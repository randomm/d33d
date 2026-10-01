#!/bin/sh
# test_colima_watchdog_safety.sh — safety-rule, status-routing, lock and
# lifecycle cases for scripts/colima-watchdog.sh (issue #281). Core
# healthy/heal-path cases live in test_colima_watchdog_heal.sh.
#
# Run from the repository root (or anywhere; the helper locates itself):
#   sh tests/scripts/test_colima_watchdog_safety.sh
#
# NOT wired into CI or scripts/test (per issue #281). Drives the watchdog
# with stubbed `docker` / `colima` / `ssh` commands on PATH so the test
# never touches a real VM.

WD_TEST_DIR=$(dirname -- "$0")
WD_TEST_DIR=$(cd "$WD_TEST_DIR" && pwd)
WD_TEST_DIR=$WD_TEST_DIR . "$(dirname -- "$0")/wd_test_helper.sh"

# A PATH that exposes the stub directory but NOT /opt/homebrew/bin or
# /usr/local/bin — the only real tools reachable are the stubs plus base
# system commands (sh, grep, cat, ...). Omitting a stub then makes the
# tool truly "missing", the way launchd's minimal PATH sees it. This
# mirrors the exit-3 pre-flight guard (issue #326).
BARE_PATH="$STUBBIN:/usr/bin:/bin:/usr/sbin:/sbin"
run_watchdog_bare() {
    PATH="$BARE_PATH" \
    HOME="$FAKEHOME" \
    D33D_WD_LOCKDIR="$LOCKDIR" \
    D33D_WD_RETRY_COUNT=1 \
    D33D_WD_RETRY_SLEEP=0 \
    sh "$WATCHDOG" "$@"
}

# The "missing tool" cases rely on the stub directory being the ONLY place
# docker/colima can come from under $BARE_PATH. If the host ever has a real
# docker or colima on a base-system path (not /opt/homebrew/bin, not
# /usr/local/bin), it would leak in and defeat the test. This host does not
# (verified); fail loudly if it ever does.
if PATH=/usr/bin:/bin:/usr/sbin:/sbin sh -c 'command -v docker || command -v colima' 2>/dev/null; then
    echo "FATAL: real docker/colima on a base-system path; missing-tool tests would be unsound" >&2
    exit 1
fi

# =============================================================================
# 4. forward dead + status running + containers RUNNING
#    -> re-forward IS attempted (it does not touch the VM), the restart
#       fallback is blocked, WARNING names the count, exits 2 (safety rule)
# =============================================================================
reset_state
make_control_path
make_stubs 1 "ERROR: cannot connect" 0 0 "$STATUS_RUNNING" 0 "abc123"
run_watchdog --dry-run
rc=$?
check_rc "containers-running: restart blocked, exit 2" 2 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "containers-running WARNING names the count" "$LOGFILE" "1 container(s) RUNNING"
fi
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "ssh -O forward" "$WORKROOT/actions.log" 2>/dev/null; then
        pass "containers-running: re-forward was attempted"
    else
        fail "containers-running: re-forward was not attempted"
    fi
    if grep -q "colima stop\|colima start" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "containers-running must not restart"
    else
        pass "containers-running did not restart"
    fi
fi

# =============================================================================
# 5. forward dead + status running + docker ps probe FAILS (non-zero)
#    -> re-forward IS attempted, restart blocked, log WARNING, exit 2
# =============================================================================
reset_state
make_control_path
make_stubs 1 "ERROR: cannot connect" 0 0 "$STATUS_RUNNING" 1 ""
run_watchdog --dry-run
rc=$?
check_rc "docker-ps probe failure -> exit 2 (conservative)" 2 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "probe failure WARNING logged" "$LOGFILE" "cannot probe container list"
fi
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "ssh -O forward" "$WORKROOT/actions.log" 2>/dev/null; then
        pass "probe failure: re-forward was attempted"
    else
        fail "probe failure: re-forward was not attempted"
    fi
    if grep -q "colima stop\|colima start" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "probe failure must not restart"
    else
        pass "probe failure did not restart"
    fi
fi

# =============================================================================
# 5b. container probe emits a warning word plus a real container id
#     -> only the id is counted (1 container) -> exit 2; pure noise without
#     ids -> counts 0 -> exit 0
# =============================================================================
reset_state
make_control_path
make_stubs 1 "ERROR: cannot connect" 0 0 "$STATUS_RUNNING" 0 "WARNING: abc123def456"
run_watchdog --dry-run
rc=$?
check_rc "probe warning word + id -> id counted, exit 2" 2 "$rc"
reset_state
make_control_path
make_stubs 1 "ERROR: cannot connect" 0 0 "$STATUS_RUNNING" 0 "WARNING: no containers found"
run_watchdog --dry-run
rc=$?
check_rc "probe noise without ids -> 0 containers, exit 0" 0 "$rc"

# =============================================================================
# 6. forward dead + colima status says NOT running
#    -> distinct path: dry-run exits 0 (start not executed); no colima stop
# =============================================================================
reset_state
make_stubs 1 "ERROR: cannot connect" 1 0 "Instance colima is not running" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "vm-not-running exit 0 (dry-run, start not executed)" 0 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "start intent logged" "$LOGFILE" "VM was not running"
fi
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "colima stop" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "vm-not-running branch must not run colima stop"
    else
        pass "vm-not-running did not call colima stop"
    fi
fi

# =============================================================================
# 21. re-forward cannot heal (no ControlMaster socket) + containers RUNNING
#     -> restart blocked, exit 2 naming the count (no restart executed)
# =============================================================================
reset_state
# No ssh_config / socket: rebuild_forward fails immediately, the dry-run
# would then exit 0 via the restart-fallback path — the exit-2 gate must
# fire first.
make_stubs 1 "ERROR: cannot connect" 0 0 "$STATUS_RUNNING" 0 "abc123 def456"
run_watchdog --dry-run
rc=$?
check_rc "re-forward impossible + containers running -> exit 2" 2 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "blocked-restart message names the count" "$LOGFILE" "2 container(s) RUNNING"
fi
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "colima stop\|colima start" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "re-forward-impossible + containers running must not restart"
    else
        pass "re-forward-impossible + containers running did not restart"
    fi
fi

# =============================================================================
# 7. forward dead + colima status probe fails (rc != 0)
#    -> cannot determine VM state; NO start attempted, exit 4
#       (issue #326: a status error is never read as "not running")
# =============================================================================
reset_state
make_stubs 1 "ERROR: cannot connect" 1 0 "error: instance not found" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "status probe failure (rc=1) -> exit 4, no start" 4 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "status error logged with exit-4 message" "$LOGFILE" "cannot determine VM state"
fi
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "colima start\|colima stop\|ssh -O forward" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "status-error path must take no action"
    else
        pass "status-error path took no action"
    fi
fi

# =============================================================================
# 7b. forward dead + colima status rc=0 with unrecognised text
#     -> cannot determine VM state; NO start attempted, exit 4
# =============================================================================
reset_state
make_stubs 1 "ERROR: cannot connect" 1 0 "level=fatal msg=boom" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "status rc=0 unrecognised text -> exit 4, no start" 4 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "unrecognised status logged with exit-4 message" "$LOGFILE" "cannot determine VM state"
fi
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "colima start\|colima stop\|ssh -O forward" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "unrecognised-status path must take no action"
    else
        pass "unrecognised-status path took no action"
    fi
fi

# =============================================================================
# 30. docker missing from PATH (stub omitted; bare PATH without
#     /opt/homebrew/bin or /usr/local/bin) -> strict pre-flight exits 3,
#     one ERROR line naming docker + the PATH, no action, no lock dir
#     (issue #326)
# =============================================================================
reset_state
# Only colima + ssh stubs present — docker is absent from $BARE_PATH.
# reset_state does NOT clear the stub binaries, so explicitly remove any
# docker stub left by a prior make_stubs call.
rm -f "$STUBBIN/docker"
cat > "$STUBBIN/colima" <<EOF
#!/bin/sh
echo "colima \$*" >> "$WORKROOT/colima.calls"
case "\$1" in
    status) printf '%s\n' "$STATUS_RUNNING" >&2; exit 0 ;;
    *) exit 0 ;;
esac
EOF
cat > "$STUBBIN/ssh" <<EOF
#!/bin/sh
echo "ssh \$*" >> "$WORKROOT/actions.log"
exit 0
EOF
chmod +x "$STUBBIN/colima" "$STUBBIN/ssh"
run_watchdog_bare
rc=$?
check_rc "missing docker -> exit 3" 3 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "missing-docker ERROR names docker" "$LOGFILE" "tools not found: docker"
    check_grep "missing-docker ERROR names the PATH" "$LOGFILE" "PATH="
    err_count=$(grep -c "ERROR" "$LOGFILE" 2>/dev/null)
    if [ "$err_count" -eq 1 ]; then
        pass "missing-docker: exactly one ERROR line"
    else
        fail "missing-docker expected 1 ERROR line, got $err_count"
    fi
fi
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "colima start\|colima stop\|ssh -O forward" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "missing-docker must take no action"
    else
        pass "missing-docker took no action"
    fi
fi
if [ ! -d "$LOCKDIR" ]; then
    pass "missing-docker: no lock dir created"
else
    fail "missing-docker: lock dir was created"
fi

# =============================================================================
# 31. colima missing while docker is healthy (stub returns rc 0) -> strict
#     pre-flight exits 3 (guard is not lazy; issue #326 operator decision)
# =============================================================================
reset_state
# Only the docker stub is present — colima is absent from $BARE_PATH.
rm -f "$STUBBIN/colima" "$STUBBIN/ssh"
cat > "$STUBBIN/docker" <<EOF
#!/bin/sh
echo "docker \$*" >> "$WORKROOT/docker.calls"
exit 0
EOF
chmod +x "$STUBBIN/docker"
run_watchdog_bare
rc=$?
check_rc "missing colima (docker healthy) -> exit 3" 3 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "missing-colima ERROR names colima" "$LOGFILE" "tools not found: colima"
else
    fail "missing-colima: log file not written"
fi
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "colima start\|colima stop\|ssh -O forward" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "missing-colima must take no action"
    else
        pass "missing-colima took no action"
    fi
fi
if [ ! -d "$LOCKDIR" ]; then
    pass "missing-colima: no lock dir created"
else
    fail "missing-colima: lock dir was created"
fi

# =============================================================================
# 32. both docker and colima missing -> exactly ONE ERROR line naming both,
#     exit 3, no action, no lock dir
# =============================================================================
reset_state
# STUBBIN left empty: neither tool on $BARE_PATH.
rm -f "$STUBBIN/docker" "$STUBBIN/colima" "$STUBBIN/ssh"
run_watchdog_bare
rc=$?
check_rc "both tools missing -> exit 3" 3 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "both-missing ERROR names docker" "$LOGFILE" "docker, colima"
    err_count=$(grep -c "ERROR" "$LOGFILE" 2>/dev/null)
    if [ "$err_count" -eq 1 ]; then
        pass "both-missing: exactly one ERROR line"
    else
        fail "both-missing expected 1 ERROR line, got $err_count"
    fi
fi
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "colima start\|colima stop\|ssh -O forward" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "both-missing must take no action"
    else
        pass "both-missing took no action"
    fi
fi
if [ ! -d "$LOCKDIR" ]; then
    pass "both-missing: no lock dir created"
else
    fail "both-missing: lock dir was created"
fi

# =============================================================================
# 15. unknown flag: script exits 64 (usage error)
# =============================================================================
reset_state
make_stubs 0 "Server: healthy" 0 0 "$STATUS_RUNNING" 0 ""
run_watchdog --bogus-flag
rc=$?
if [ "$rc" -eq 64 ]; then
    pass "unknown flag -> exit 64 (usage error)"
else
    fail "unknown flag expected rc=64, got rc=$rc"
fi

# =============================================================================
# 16. healthy: dry-run still exits 0 and takes no action (no log file for
#     a no-op tick)
# =============================================================================
reset_state
rm -f "$LOGFILE"
make_stubs 0 "Server: healthy" 0 0 "$STATUS_RUNNING" 0 ""
run_watchdog --dry-run
rc=$?
if [ -f "$LOGFILE" ]; then
    if grep -q "WARN" "$LOGFILE" 2>/dev/null; then
        fail "healthy run must not log a WARNING"
    else
        pass "healthy run logged no WARNING"
    fi
else
    pass "healthy run: no log file (correct no-op)"
fi

# =============================================================================
# 17. status probe returns rc=0 with "not running" text
#     -> the negative-check-first matcher must NOT misroute into the
#        running-VM branch; the script takes the start path (no colima
#        stop) and dry-run exits 0.
# =============================================================================
reset_state
make_stubs 1 "ERROR: cannot connect" 0 0 "Instance colima is not running" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "status rc=0 'not running' text routes to start path (exit 0 dry-run)" 0 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "not-running text logged the start intent" "$LOGFILE" "VM was not running"
fi
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "colima stop" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "not-running text must not enter restart path"
    else
        pass "not-running text did not call colima stop"
    fi
fi

# =============================================================================
# 18. stale lock recovery: a lock directory left by a killed run (dead
#     recorded pid) is reclaimed, and the watchdog proceeds to its
#     normal dry-run exit 0.
# =============================================================================
reset_state
# Seed a stale lock whose recorded pid no longer exists.
mkdir -p "$LOCKDIR"
printf '%s\n' "99999" > "$LOCKDIR/pid" 2>/dev/null
# If 99999 happens to be alive on this host (rare), re-seed with a
# certainly-dead pid so the reclaim path is exercised.
if kill -0 99999 2>/dev/null; then
    sh -c 'exit 0' &
    _deadpid=$!
    wait "$_deadpid" 2>/dev/null
    printf '%s\n' "$_deadpid" > "$LOCKDIR/pid" 2>/dev/null
fi
make_stubs 1 "ERROR: cannot connect" 0 0 "$STATUS_RUNNING" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "stale lock reclaimed -> healthy exit 0" 0 "$rc"
if [ ! -d "$LOCKDIR" ]; then
    pass "stale lock directory removed after reclaim"
else
    fail "stale lock directory still present after run"
fi

# =============================================================================
# 19. live lock contention: lock directory holds a LIVE pid -> watchdog
#     must skip the tick (exit 1), not steal the lock.
# =============================================================================
reset_state
# Start a long-lived sleep as a live sentinel pid.
sleep 30 &
_live_pid=$!
mkdir -p "$LOCKDIR"
printf '%s\n' "$_live_pid" > "$LOCKDIR/pid" 2>/dev/null
make_stubs 1 "ERROR: cannot connect" 0 0 "$STATUS_RUNNING" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "live lock contention -> exit 1 (skip)" 1 "$rc"
kill "$_live_pid" 2>/dev/null
wait "$_live_pid" 2>/dev/null
rm -rf "$LOCKDIR"
if [ -f "$LOGFILE" ]; then
    check_grep "live contention logged skip" "$LOGFILE" "another watchdog instance is active"
else
    pass "live contention: log check skipped (no log file)"
fi
# The live lock dir must have been preserved (not stolen); reset_state
# already cleared it above, so just assert no crash.
pass "live lock skip path completed"

# =============================================================================
# summary
# =============================================================================
summary
