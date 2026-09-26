#!/bin/sh
# wd_test_helper.sh — shared harness for the colima-watchdog test files
# (test_colima_watchdog_heal.sh and test_colima_watchdog_safety.sh).
# Sourced, not executed. See test_colima_watchdog_heal.sh for an entry point.
#
# NOT wired into CI or scripts/test (per issue #281). The harness drives the
# watchdog with stubbed `docker` / `colima` / `ssh` commands on PATH so the
# test never touches a real VM.

set -u

# --- locate the script under test --------------------------------------------
# The helper is sourced by the test files, which set WD_TEST_DIR to their own
# directory ($0 inside the helper is the invoking shell, not the helper).

CDPATH=
TEST_DIR=$(cd -- "${WD_TEST_DIR:-.}" && pwd)
CDPATH=
SCRIPT_DIR=${WATCHDOG_DIR:-$TEST_DIR/../../scripts}
CDPATH=
SCRIPT_DIR=$(cd -- "$SCRIPT_DIR" && pwd)
WATCHDOG="$SCRIPT_DIR/colima-watchdog.sh"

if [ ! -f "$WATCHDOG" ]; then
    echo "FATAL: $WATCHDOG not found" >&2
    exit 1
fi

# --- scratch + stub environment ----------------------------------------------

WORKROOT=$(mktemp -d "${TMPDIR:-/tmp}/colima-watchdog-test.XXXXXX")
STUBBIN="$WORKROOT/bin"
FAKEHOME="$WORKROOT/home"
LOCKDIR="$WORKROOT/lock.d33d-colima-watchdog.lock"
mkdir -p "$STUBBIN" "$FAKEHOME"

# Trap for clean-up of the scratch tree (and any stray socket under /tmp
# used by make_control_path).
trap 'rm -rf "$WORKROOT" /tmp/d33d-wd-test.sock' EXIT

# --- counters + result helpers ------------------------------------------------

PASS=0
FAIL=0

pass() {
    PASS=$((PASS + 1))
    echo "ok   - $1"
}

fail() {
    FAIL=$((FAIL + 1))
    echo "FAIL - $1"
}

# check_rc <name> <expected_rc> <observed_rc>
check_rc() {
    name=$1
    expected=$2
    observed=$3
    if [ "$observed" -eq "$expected" ]; then
        pass "$name (rc=$observed)"
    else
        fail "$name (expected rc=$expected, got rc=$observed)"
    fi
}

# check_grep <name> <file> <pattern>
check_grep() {
    name=$1
    file=$2
    pattern=$3
    if grep -q -- "$pattern" "$file" 2>/dev/null; then
        pass "$name (found: $pattern)"
    else
        fail "$name (missing: $pattern in $file)"
    fi
}

# check_not_grep <name> <file> <pattern>
check_not_grep() {
    name=$1
    file=$2
    pattern=$3
    if grep -q -- "$pattern" "$file" 2>/dev/null; then
        fail "$name (unexpected: $pattern in $file)"
    else
        pass "$name (absent: $pattern)"
    fi
}

# summary — print totals and exit non-zero on any failure.
summary() {
    echo
    echo "PASS=$PASS FAIL=$FAIL"
    if [ "$FAIL" -gt 0 ]; then
        exit 1
    fi
}

# --- stub factory -------------------------------------------------------------
# make_stubs <docker_rc> <docker_info_text> [docker_ok=0|1] <status_rc> <status_out> <containers_rc> <containers_out>
#   docker info (before any heal action) -> DOCKER_RC / DOCKER_INFO_TEXT
#   docker info (after a heal action)    -> 0 if D_OK=1, else DOCKER_RC/TEXT
#   colima status                    -> STATUS_RC / STATUS_OUT (on stderr)
#   colima ssh -- docker ps -q       -> CONTAINERS_RC / CONTAINERS_OUT
#   colima ssh -n                    -> exit 0, prints nothing
#   colima stop|start                -> appended to "$WORKROOT/actions.log"
#   ssh (re-forward via ControlMaster) -> appended to "$WORKROOT/actions.log"

make_stubs() {
    D_RC=$1
    D_INFO=$2
    D_OK=$3
    S_RC=$4
    S_OUT=$5
    C_RC=$6
    C_OUT=$7

    cat > "$STUBBIN/docker" <<EOF
#!/bin/sh
echo "docker \$*" >> "$WORKROOT/docker.calls"
if [ "\$1" = "info" ]; then
    if [ -f "$WORKROOT/actions.log" ] && [ "$D_OK" = "1" ]; then
        exit 0
    fi
    printf '%s\n' "$D_INFO"
    exit $D_RC
fi
printf '%s\n' "$D_INFO"
exit 0
EOF

    cat > "$STUBBIN/colima" <<EOF
#!/bin/sh
echo "colima \$*" >> "$WORKROOT/colima.calls"
case "\$1" in
    status)
        # Real colima writes its status message to STDERR (logrus format:
        # time=... level=info msg="colima is running ..."). The stub must
        # mirror this so the test exercises the same code path as the real
        # tool.
        printf '%s\n' "$S_OUT" >&2
        exit $S_RC
        ;;
    ssh)
        if [ "\$2" = "-n" ]; then
            exit 0
        fi
        if [ "\$2" = "--" ] && [ "\$3" = "docker" ] && [ "\$4" = "ps" ]; then
            printf '%s\n' "$C_OUT"
            exit $C_RC
        fi
        exit 0
        ;;
    stop)
        echo "colima stop" >> "$WORKROOT/actions.log"
        exit 0
        ;;
    start)
        echo "colima start" >> "$WORKROOT/actions.log"
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
EOF

    cat > "$STUBBIN/ssh" <<EOF
#!/bin/sh
echo "ssh \$*" >> "$WORKROOT/actions.log"
exit 0
EOF

    chmod +x "$STUBBIN/docker" "$STUBBIN/colima" "$STUBBIN/ssh"
}

# reset between cases: wipe call logs and per-test state
reset_state() {
    rm -f "$WORKROOT/actions.log" "$WORKROOT/docker.calls" "$WORKROOT/colima.calls"
    rm -rf "$FAKEHOME/Library" "$FAKEHOME/.colima"
    mkdir -p "$FAKEHOME/Library"
    rm -rf "$LOCKDIR"
}

# make_control_path — write a fake ~/.colima/ssh_config with a ControlPath
# under the fake home, AND a real unix socket at that path so the watchdog
# takes the re-forward branch (without the socket it correctly falls back
# to restart). macOS caps AF_UNIX paths at 104 bytes and the test home is
# already deep, so the socket is bound on a short /tmp path and mv'd in.
make_control_path() {
    mkdir -p "$FAKEHOME/.colima/_lima/colima"
    cat > "$FAKEHOME/.colima/ssh_config" <<'EOF'
Host colima
    ControlPath ~/.colima/_lima/colima/ssh.sock
    UserKnownHostsFile ~/.lima/default/ssh/known_hosts
EOF
    if python3 -c 'import socket; s=socket.socket(socket.AF_UNIX); s.bind("/tmp/d33d-wd-test.sock")' 2>/dev/null; then
        mv /tmp/d33d-wd-test.sock "$FAKEHOME/.colima/_lima/colima/ssh.sock" 2>/dev/null || rm -f /tmp/d33d-wd-test.sock
    fi
}

# run_watchdog <args...> — runs the watchdog with stubs on PATH and HOME
# faked to $FAKEHOME so the log lands in $FAKEHOME/Library/Logs/....
# D33D_WD_RETRY_COUNT/SLEEP keep the post-heal verify loop instant, and
# D33D_WD_LOCKDIR points the lock fence at a per-run scratch dir so runs
# never share (or trip over) a lock left by a previous instance.
run_watchdog() {
    PATH="$STUBBIN:$PATH" \
    HOME="$FAKEHOME" \
    D33D_WD_LOCKDIR="$LOCKDIR" \
    D33D_WD_RETRY_COUNT=1 \
    D33D_WD_RETRY_SLEEP=0 \
    sh "$WATCHDOG" "$@"
}

# log file under test
LOGFILE="$FAKEHOME/Library/Logs/d33d-colima-watchdog.log"

# Realistic colima status output (matches real colima 0.10.1 logrus format
# on stderr: time=... level=info msg="colima is running using ...").
STATUS_RUNNING="colima is running using macOS Virtualization.Framework"
