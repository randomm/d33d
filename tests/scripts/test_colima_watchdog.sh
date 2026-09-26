#!/bin/sh
# test_colima_watchdog.sh — local shell test for scripts/colima-watchdog.sh
#
# Run from the repository root (or anywhere; the script locates itself):
#   sh tests/scripts/test_colima_watchdog.sh
#
# NOT wired into CI or scripts/test (per issue #281). Drives the watchdog's
# --dry-run mode with stubbed `docker` / `colima` / `ssh` commands on PATH so
# the test never touches a real VM.

set -u

# --- locate repo root and the script under test ------------------------------

TEST_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SCRIPT_DIR=${WATCHDOG_DIR:-$(CDPATH= cd -- "$TEST_DIR/../../scripts" && pwd)}
WATCHDOG="$SCRIPT_DIR/colima-watchdog.sh"

if [ ! -f "$WATCHDOG" ]; then
    echo "FATAL: $WATCHDOG not found" >&2
    exit 1
fi

# --- scratch + stub environment ----------------------------------------------

WORKROOT=$(mktemp -d "${TMPDIR:-/tmp}/colima-watchdog-test.XXXXXX")
STUBBIN="$WORKROOT/bin"
FAKEHOME="$WORKROOT/home"
mkdir -p "$STUBBIN" "$FAKEHOME"

# Trap for clean-up of the scratch tree.
trap 'rm -rf "$WORKROOT"' EXIT

# --- test harness -------------------------------------------------------------

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

# --- stub factory -------------------------------------------------------------
# make_stubs <docker_rc> <docker_info_text> <status_rc> <status_out> <containers_rc> <containers_out>
# The docker/colima stubs read their args and branch:
#   docker info                      -> DOCKER_RC / DOCKER_INFO_TEXT
#   colima status                    -> STATUS_RC / STATUS_OUT
#   colima ssh -- docker ps -q       -> CONTAINERS_RC / CONTAINERS_OUT
#   colima ssh -n                    -> prints nothing, exit 0 (for ControlPath probe)
#   colima stop|start                -> appended to "$WORKROOT/actions.log"
#   ssh (re-forward via ControlMaster)-> appended to "$WORKROOT/actions.log", exit 0
# A stub run counter increments a per-stub counter file so the test can
# assert how many times each stub was invoked.

make_stubs() {
    D_RC=$1
    D_INFO=$2
    S_RC=$3
    S_OUT=$4
    C_RC=$5
    C_OUT=$6

    cat > "$STUBBIN/docker" <<EOF
#!/bin/sh
echo "docker \$*" >> "$WORKROOT/docker.calls"
if [ "\$1" = "info" ]; then
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
        printf '%s\n' "$S_OUT"
        exit $S_RC
        ;;
    ssh)
        # detect "colima ssh -- docker ps -q" vs "colima ssh -n" vs other
        if [ "\$2" = "-n" ]; then
            exit 0
        fi
        if [ "\$2" = "--" ] && [ "\$3" = "docker" ] && [ "\$4" = "ps" ]; then
            printf '%s\n' "$C_OUT"
            exit $C_RC
        fi
        # any other colima ssh invocation (e.g. for status) — treat as success
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
    rm -rf "$FAKEHOME/Library"
    mkdir -p "$FAKEHOME/Library"
}

# run_watchdog <args...> — runs the watchdog with stubs on PATH and HOME faked
# to $FAKEHOME so the log lands in $FAKEHOME/Library/Logs/...
run_watchdog() {
    PATH="$STUBBIN:$PATH" \
    HOME="$FAKEHOME" \
    sh "$WATCHDOG" "$@"
}

# log file under test
LOGFILE="$FAKEHOME/Library/Logs/d33d-colima-watchdog.log"

# =============================================================================
# 1. healthy: docker info succeeds -> rc 0, no heal action, log has healthy marker
# =============================================================================
reset_state
make_stubs 0 "Server: healthy" 0 "Running" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "healthy exit 0" 0 "$rc"
check_not_grep "healthy no restart action" "$WORKROOT/actions.log" "restart" 2>/dev/null || true
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "restart" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "healthy should not trigger any restart/re-forward action"
    else
        pass "healthy no restart action in actions.log"
    fi
else
    pass "healthy no restart action (no actions.log written)"
fi
if [ -f "$LOGFILE" ]; then
    check_grep "healthy log marker" "$LOGFILE" "healthy"
fi

# =============================================================================
# 2. forward dead + status running + no containers + ControlPath present
#    -> dry-run prints "re-forward", does not restart, exits 0
# =============================================================================
reset_state
# ControlPath probe: the script runs `ssh -O check -S <path>` or reads the
# ssh_config; with the `ssh` stub on PATH, `ssh -O check` returns 0 (stub).
# To model ControlPath presence we also seed a fake colima ssh_config file.
mkdir -p "$FAKEHOME/.colima"
cat > "$FAKEHOME/.colima/ssh_config" <<'EOF'
Host colima
    ControlPath /Users/fake/.colima/_lima/colima/ssh.sock
    UserKnownHostsFile /Users/fake/.lima/default/ssh/known_hosts
EOF
make_stubs 1 "ERROR: cannot connect to Docker daemon" 0 "Running" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "dead-forward healed exit 0" 0 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "re-forward action logged" "$LOGFILE" "re-forward"
fi
# no colima stop/start should have been attempted
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "colima stop" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "re-forward branch must not restart colima"
    else
        pass "re-forward branch did not restart colima"
    fi
else
    pass "re-forward branch did not restart colima (no actions.log)"
fi

# =============================================================================
# 3. forward dead + status running + no containers + NO ControlPath
#    -> dry-run prints "restart", exits 0
# =============================================================================
reset_state
# No ssh_config file at all -> no ControlPath available
rm -rf "$FAKEHOME/.colima"
make_stubs 1 "ERROR: cannot connect to Docker daemon" 0 "Running" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "restart-fallback healed exit 0" 0 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "restart action logged" "$LOGFILE" "restart"
fi

# =============================================================================
# 4. forward dead + status running + containers RUNNING
#    -> script does NOT restart, logs WARNING, exits 2
# =============================================================================
reset_state
mkdir -p "$FAKEHOME/.colima"
cat > "$FAKEHOME/.colima/ssh_config" <<'EOF'
Host colima
    ControlPath /Users/fake/.colima/_lima/colima/ssh.sock
    UserKnownHostsFile /Users/fake/.lima/default/ssh/known_hosts
EOF
make_stubs 1 "ERROR: cannot connect" 0 "Running" 0 "abc123"
run_watchdog --dry-run
rc=$?
check_rc "containers-running skip exit 2" 2 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "containers-running WARNING logged" "$LOGFILE" "WARNING"
fi
# no action should have been taken
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "colima stop" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "containers-running branch must not restart colima"
    else
        pass "containers-running branch did not restart colima"
    fi
else
    pass "containers-running branch did not restart colima (no actions.log)"
fi

# =============================================================================
# 5. forward dead + status running + docker ps probe FAILS (non-zero)
#    -> conservative: no restart, log WARNING, exit non-zero (2)
# =============================================================================
reset_state
mkdir -p "$FAKEHOME/.colima"
cat > "$FAKEHOME/.colima/ssh_config" <<'EOF'
Host colima
    ControlPath /Users/fake/.colima/_lima/colima/ssh.sock
EOF
make_stubs 1 "ERROR: cannot connect" 0 "Running" 1 ""
run_watchdog --dry-run
rc=$?
check_rc "docker-ps probe failure -> exit 2 (conservative)" 2 "$rc"

# =============================================================================
# 6. forward dead + colima status says NOT running
#    -> distinct path, exit 1 (not healed), no restart in dry-run
#    (the branch for "VM down" is defined by the script; in dry-run it logs
#    the intended start and exits 1 because we cannot actually start in a test)
#    OR: the script may attempt colima start (idempotent, not stop+start) and
#    then docker info still fails -> exit 1.
#    Both are acceptable; we assert exit 1 and that NO `colima stop` was called.
# =============================================================================
reset_state
rm -rf "$FAKEHOME/.colima"
make_stubs 1 "ERROR: cannot connect" 1 "Instance colima is not running" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "vm-not-running exit 1" 1 "$rc"
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "colima stop" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "vm-not-running branch must not run colima stop"
    else
        pass "vm-not-running branch did not call colima stop"
    fi
else
    pass "vm-not-running branch did not call colima stop (no actions.log)"
fi

# =============================================================================
# 7. docker info fails, colima status also fails (non-zero)
#    -> cannot determine VM state; conservative: exit 1, no restart
# =============================================================================
reset_state
rm -rf "$FAKEHOME/.colima"
make_stubs 1 "ERROR: cannot connect" 1 "error: instance not found" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "status probe failure -> exit 1 (not healed)" 1 "$rc"

# =============================================================================
# 8. healthy: dry-run still exits 0 and does not take any action
# =============================================================================
reset_state
make_stubs 0 "Server: healthy" 0 "Running" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "dry-run healthy exit 0" 0 "$rc"

# =============================================================================
# 9. log file is written under $FAKEHOME/Library/Logs (mkdir -p is implicit)
# =============================================================================
reset_state
make_stubs 1 "ERROR: cannot connect" 0 "Running" 0 "abc123"
run_watchdog --dry-run
# containers-running: log must exist and contain WARNING
if [ -f "$LOGFILE" ]; then
    pass "log file exists after containers-running case"
    check_grep "log contains WARNING" "$LOGFILE" "WARNING"
else
    fail "log file not found at $LOGFILE after containers-running case"
fi

# =============================================================================
# 10. dry-run: no `colima stop` / `colima start` should appear in actions.log
#      even in the restart-fallback branch (dry-run only prints intent)
# =============================================================================
reset_state
rm -rf "$FAKEHOME/.colima"
make_stubs 1 "ERROR: cannot connect" 0 "Running" 0 ""
run_watchdog --dry-run
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "colima stop" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "dry-run restart-fallback must not actually call colima stop"
    else
        pass "dry-run restart-fallback did not call colima stop"
    fi
    if grep -q "colima start" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "dry-run restart-fallback must not actually call colima start"
    else
        pass "dry-run restart-fallback did not call colima start"
    fi
else
    pass "dry-run restart-fallback: no actions.log (no real actions taken)"
fi

# =============================================================================
# 11. non-dry-run (real mode): docker info fails, no ControlPath, no containers
#     -> script SHOULD call colima stop + colima start; both stubbed.
#     After heal, docker info succeeds (stub always returns D_RC=1 on first
#     call... but stub is stateless). Since stubs are stateless, the heal
#     verification check will still fail -> exit 1. That is expected: the
#     important assertions are that colima stop AND colima start were called.
# =============================================================================
reset_state
rm -rf "$FAKEHOME/.colima"
make_stubs 1 "ERROR: cannot connect" 0 "Running" 0 ""
# run WITHOUT --dry-run
PATH="$STUBBIN:$PATH" HOME="$FAKEHOME" sh "$WATCHDOG"
rc=$?
# stubs never recover, so the verify step fails -> exit 1 (not healed)
check_rc "real-mode heal attempted but stubs never recover -> exit 1" 1 "$rc"
if [ -f "$WORKROOT/actions.log" ]; then
    check_grep "real-mode called colima stop" "$WORKROOT/actions.log" "colima stop"
    check_grep "real-mode called colima start" "$WORKROOT/actions.log" "colima start"
else
    fail "real-mode heal: actions.log not found — colima stop/start not called"
fi

# =============================================================================
# 12. non-dry-run: docker info fails, containers running -> exit 2, no stop
# =============================================================================
reset_state
mkdir -p "$FAKEHOME/.colima"
cat > "$FAKEHOME/.colima/ssh_config" <<'EOF'
Host colima
    ControlPath /Users/fake/.colima/_lima/colima/ssh.sock
EOF
make_stubs 1 "ERROR: cannot connect" 0 "Running" 0 "abc123"
PATH="$STUBBIN:$PATH" HOME="$FAKEHOME" sh "$WATCHDOG"
rc=$?
check_rc "real-mode containers-running exit 2" 2 "$rc"
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "colima stop" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "real-mode containers-running must not call colima stop"
    else
        pass "real-mode containers-running did not call colima stop"
    fi
else
    pass "real-mode containers-running: no actions.log (no real actions)"
fi

# =============================================================================
# 13. non-dry-run: docker info fails, ControlPath present, no containers
#     -> re-forward (ssh stub) is called; no colima stop/start
# =============================================================================
reset_state
mkdir -p "$FAKEHOME/.colima"
cat > "$FAKEHOME/.colima/ssh_config" <<'EOF'
Host colima
    ControlPath /Users/fake/.colima/_lima/colima/ssh.sock
    UserKnownHostsFile /Users/fake/.lima/default/ssh/known_hosts
EOF
make_stubs 1 "ERROR: cannot connect" 0 "Running" 0 ""
PATH="$STUBBIN:$PATH" HOME="$FAKEHOME" sh "$WATCHDOG"
rc=$?
# re-forward via ssh stub succeeds (exit 0), but the verify docker info call
# still fails (stub is stateless) -> exit 1 (not healed after re-forward)
# The key assertion: ssh WAS called (re-forward was attempted first).
# We do NOT assert that colima stop was NOT called, because the script may
# reasonably fall through to restart if re-forward didn't fix docker info.
check_rc "real-mode re-forward attempted but stubs never recover -> exit 1" 1 "$rc"
if [ -f "$WORKROOT/actions.log" ]; then
    check_grep "re-forward used ssh" "$WORKROOT/actions.log" "ssh"
else
    fail "real-mode re-forward: actions.log not found"
fi

# =============================================================================
# 14. --dry-run flag: log file is written (not suppressed in dry-run)
# =============================================================================
reset_state
make_stubs 0 "Server: healthy" 0 "Running" 0 ""
run_watchdog --dry-run
if [ -f "$LOGFILE" ]; then
    pass "dry-run writes log entry"
else
    fail "dry-run did not write log file"
fi

# =============================================================================
# 15. unknown flag: script exits non-zero (not 0 or 2)
# =============================================================================
reset_state
make_stubs 0 "Server: healthy" 0 "Running" 0 ""
run_watchdog --bogus-flag
rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
    pass "unknown flag exits non-zero (rc=$rc)"
else
    fail "unknown flag: expected non-zero exit, got $rc"
fi

# =============================================================================
# 16. docker info succeeds but colima status also checked (idempotent healthy)
#     Ensure the script does NOT log a WARNING or trigger any heal when healthy
# =============================================================================
reset_state
rm -f "$LOGFILE"
make_stubs 0 "Server: healthy" 0 "Running" 0 ""
run_watchdog --dry-run
if [ -f "$LOGFILE" ]; then
    if grep -q "WARNING" "$LOGFILE" 2>/dev/null; then
        fail "healthy run must not log a WARNING"
    else
        pass "healthy run logged no WARNING"
    fi
else
    pass "healthy run: log file optional (or no WARNING)"
fi

# =============================================================================
# summary
# =============================================================================
echo ""
echo "=== $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
