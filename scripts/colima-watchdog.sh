#!/bin/sh
# colima-watchdog.sh — detect a dead host-side docker.sock forward on a
# colima/lima machine and restore it (issue #281).
#
# Intended to run every 60 s from a launchd agent (see
# scripts/com.d33d.colima-watchdog.plist and docs/colima-watchdog.md).
#
# Behaviour:
#   1. Health check: `docker info` with a wall-clock bound (~10 s).
#      Success -> exit 0, no log entry.
#   2. Check failed:
#      - colima status says NOT running: `colima start` (a stop+start on
#        an already-dead VM is meaningless).
#      - colima status says running: the forward is presumed dead.
#        Safety rule: if any container inside the VM is RUNNING (or the
#        probe itself fails), do NOT touch the VM — log a WARNING and
#        exit 2. Otherwise re-establish the `ssh -O forward` through
#        the lima ControlMaster (ControlPath read at runtime from the
#        colima ssh config); fall back to `colima stop && colima start`
#        only when re-forward is impossible.
#   3. Every heal is verified with a bounded `docker info` retry loop
#      (docker.sock readiness lags `colima start` completion).
#
# Exit codes: 0 healthy or healed, 1 not healed, 2 skipped because
# containers were running (or the container probe failed — conservative).
#
# POSIX sh only. No GNU `timeout` on stock macOS — uses gtimeout when
# available, else a background-process + kill pattern. No flock on
# stock macOS — a mkdir lock directory is the fence against overlapping
# launchd ticks during a ~1 min restart.
#
# --dry-run: logs the intended action instead of executing it.
#
# Overridable via env for the local test harness (tests/scripts/):
#   D33D_WD_HOME         fake $HOME (log dir + colima dir point here)
#   D33D_WD_DOCTIMEOUT   docker info timeout seconds (default 10)
#   D33D_WD_RETRY_COUNT  post-heal docker info retries (default 6)
#   D33D_WD_RETRY_SLEEP  seconds between retries (default 5)
#   D33D_WD_LOGFILE      log file path (default $HOME/Library/Logs/
#                        d33d-colima-watchdog.log)
#   D33D_WD_LOCKDIR      lock directory (default $TMPDIR-based)

set -u

WD_HOME="${D33D_WD_HOME:-$HOME}"
DOCKER_TIMEOUT="${D33D_WD_DOCTIMEOUT:-10}"
RETRY_COUNT="${D33D_WD_RETRY_COUNT:-6}"
RETRY_SLEEP="${D33D_WD_RETRY_SLEEP:-5}"
LOG_FILE="${D33D_WD_LOGFILE:-$WD_HOME/Library/Logs/d33d-colima-watchdog.log}"
LOCK_DIR="${D33D_WD_LOCKDIR:-${TMPDIR:-/tmp}/d33d-colima-watchdog.lock}"
DRY_RUN=0

# ControlPath is read at runtime so the script never hard-codes a home
# path. Empty when the ssh config is missing or has no ControlPath.
# The real colima ssh_config indents and double-quotes the ControlPath
# line, so the parse tolerates both (awk field match + quote strip).
CONTROL_PATH=""
if [ -r "$WD_HOME/.colima/ssh_config" ]; then
    CONTROL_PATH=$(awk 'tolower($1)=="controlpath"{print $2; exit}' \
        "$WD_HOME/.colima/ssh_config" | sed -e 's/^"//' -e 's/"$//')
fi

# ---------------------------------------------------------------- logging

# log LEVEL message...
# Log failures must never mask the real exit code.
log() {
    {
        mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null \
            && printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" \
                >>"$LOG_FILE" 2>/dev/null
    } >/dev/null 2>&1 || :
}

# ---------------------------------------------------------------- helpers

# run_with_timeout SECS CMD [ARGS...] — run CMD under a wall-clock bound.
# Exit status: the command's status if it finished in time, 124 if
# killed by the bound (matching GNU timeout).
run_with_timeout() {
    secs=$1
    shift
    if command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$secs" "$@"
        return $?
    fi
    "$@" &
    pid=$!
    i=0
    while [ "$i" -lt $((secs * 10)) ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            wait "$pid"
            return $?
        fi
        sleep 0.1 2>/dev/null || sleep 1
        i=$((i + 1))
    done
    kill "$pid" 2>/dev/null
    kill -9 "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    return 124
}

docker_info_check() {
    run_with_timeout "$DOCKER_TIMEOUT" docker info >/dev/null 2>&1
}

# wait_for_docker — bounded retry loop for post-heal verification.
wait_for_docker() {
    i=1
    while [ "$i" -le "$RETRY_COUNT" ]; do
        if docker_info_check; then
            return 0
        fi
        if [ "$i" -lt "$RETRY_COUNT" ]; then
            sleep "$RETRY_SLEEP"
        fi
        i=$((i + 1))
    done
    return 1
}

colima_status_running() {
    out=$(colima status 2>/dev/null) || return 1
    case "$out" in
    *running*) return 0 ;;
    *) return 1 ;;
    esac
}

running_container_count() {
    # Echoes the number of RUNNING containers in the VM, or the literal
    # string "error" when the probe itself failed. A failed probe is
    # treated conservatively by the caller (never restart).
    out=$(colima ssh -- docker ps -q 2>/dev/null)
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "error"
        return 0
    fi
    n=0
    for _ in $out; do
        n=$((n + 1))
    done
    echo "$n"
}

# ---------------------------------------------------------------- actions

# act_restart / act_start / act_reforward log the intended action and
# execute it (or no-op under --dry-run). All return 0; post-heal
# verification is done by the caller via wait_for_docker.

act_restart() {
    log ACTION "colima stop && colima start"
    if [ "$DRY_RUN" -eq 1 ]; then
        log RESULT "dry-run: restart not executed"
        return 0
    fi
    colima stop && colima start
}

act_start() {
    log ACTION "colima start (VM was not running)"
    if [ "$DRY_RUN" -eq 1 ]; then
        log RESULT "dry-run: start not executed"
        return 0
    fi
    colima start
}

# rebuild_forward — try to re-establish the forward through the existing
# ControlMaster. Returns 0 when the ssh -O round-trip succeeded, 1 when
# it is impossible (no ControlPath, missing master socket, ssh failed)
# and the caller should fall back to a full restart.
rebuild_forward() {
    if [ -z "$CONTROL_PATH" ]; then
        log INFO "no usable ControlPath in colima ssh config; restart required"
        return 1
    fi
    case "$CONTROL_PATH" in
    "~"*) cp="${WD_HOME}${CONTROL_PATH#\~}" ;;
    *) cp="$CONTROL_PATH" ;;
    esac
    if [ ! -S "$cp" ]; then
        log INFO "ControlPath $cp not a socket; restart required"
        return 1
    fi
    log ACTION "ssh -O forward via $cp"
    if [ "$DRY_RUN" -eq 1 ]; then
        log RESULT "dry-run: re-forward not executed"
        return 0
    fi
    if ssh -S "$cp" -O forward -f -L \
        "$WD_HOME/.colima/default/docker.sock:/var/run/docker.sock" colima \
        >/dev/null 2>&1; then
        return 0
    fi
    log INFO "ssh -O forward via $cp failed; restart required"
    return 1
}

# ---------------------------------------------------------------- main

main() {
    for arg in "$@"; do
        case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        *)
            echo "colima-watchdog: unsupported argument: $arg" >&2
            echo "usage: colima-watchdog.sh [--dry-run]" >&2
            exit 64
            ;;
        esac
    done

    # 1. Healthy? Exit 0 with no log entry.
    if docker_info_check; then
        exit 0
    fi
    log WARN "check failed: docker info did not succeed within ${DOCKER_TIMEOUT}s"

    # Fence against overlapping launchd ticks (a restart takes ~1 min,
    # the cadence is 60 s). mkdir is atomic; the loser skips this tick.
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        log WARN "another watchdog instance is active; skipping"
        exit 1
    fi
    trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT INT TERM

    if colima_status_running; then
        # Running VM, dead forward. Safety rule first.
        n=$(running_container_count)
        if [ "$n" = "error" ]; then
            log WARN "cannot probe container list (colima ssh failed); not touching VM"
            exit 2
        fi
        if [ "$n" -gt 0 ]; then
            log WARN "$n container(s) RUNNING; not restarting (safety rule)"
            exit 2
        fi
        # Cheapest heal first: re-forward via the ControlMaster.
        if rebuild_forward; then
            if [ "$DRY_RUN" -eq 1 ]; then
                exit 0
            fi
            if wait_for_docker; then
                log RESULT "re-forward succeeded; docker reachable"
                exit 0
            fi
            log WARN "re-forward attempted but docker still unreachable; escalating to restart"
        fi
        # Fallback: full stop/start.
        act_restart
        if [ "$DRY_RUN" -eq 1 ]; then
            exit 0
        fi
        if wait_for_docker; then
            log RESULT "restart succeeded; docker reachable"
            exit 0
        fi
        log ERROR "restart attempted but docker still unreachable after ${RETRY_COUNT} tries"
        exit 1
    fi

    # VM is not running: start is the recovery (no container probe —
    # nothing can be running in a down VM).
    act_start
    if [ "$DRY_RUN" -eq 1 ]; then
        exit 0
    fi
    if wait_for_docker; then
        log RESULT "start succeeded; docker reachable"
        exit 0
    fi
    log ERROR "start attempted but docker still unreachable after ${RETRY_COUNT} tries"
    exit 1
}

main "$@"
