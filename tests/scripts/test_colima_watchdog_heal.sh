#!/bin/sh
# test_colima_watchdog_heal.sh — core + heal-path cases for
# scripts/colima-watchdog.sh (issue #281). Safety/lock/lifecycle cases
# live in test_colima_watchdog_safety.sh.
#
# Run from the repository root (or anywhere; the helper locates itself):
#   sh tests/scripts/test_colima_watchdog_heal.sh
#
# NOT wired into CI or scripts/test (per issue #281). Drives the watchdog
# with stubbed `docker` / `colima` / `ssh` commands on PATH so the test
# never touches a real VM.

WD_TEST_DIR=$(dirname -- "$0")
WD_TEST_DIR=$(cd "$WD_TEST_DIR" && pwd)
WD_TEST_DIR=$WD_TEST_DIR . "$(dirname -- "$0")/wd_test_helper.sh"

# =============================================================================
# 1. healthy: docker info succeeds -> rc 0, no heal action, no log file
# =============================================================================
reset_state
make_stubs 0 "Server: healthy" 0 0 "$STATUS_RUNNING" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "healthy exit 0" 0 "$rc"
# A healthy tick exits 0 before writing any log (no heal needed).
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "restart" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "healthy should not trigger any restart/re-forward action"
    else
        pass "healthy no restart action"
    fi
else
    pass "healthy no restart action (no actions.log written)"
fi
if [ -f "$LOGFILE" ]; then
    check_not_grep "healthy log has no WARNING" "$LOGFILE" "WARN"
fi

# =============================================================================
# 2. forward dead + status running + no containers + ControlPath present
#    -> re-forward intended, no restart, exit 0
# =============================================================================
reset_state
make_control_path
make_stubs 1 "ERROR: cannot connect to Docker daemon" 0 0 "$STATUS_RUNNING" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "dead-forward healed exit 0" 0 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "re-forward action logged" "$LOGFILE" "re-forward"
fi
if [ -f "$WORKROOT/actions.log" ]; then
    if grep -q "colima stop" "$WORKROOT/actions.log" 2>/dev/null; then
        fail "dead-forward (ControlPath present) must not restart"
    else
        pass "dead-forward (ControlPath present) did not restart"
    fi
fi

# =============================================================================
# 3. forward dead + status running + no containers + NO ControlPath
#    -> restart intended, exit 0
# =============================================================================
reset_state
# No ssh_config file at all -> no ControlPath available
make_stubs 1 "ERROR: cannot connect to Docker daemon" 0 0 "$STATUS_RUNNING" 0 ""
run_watchdog --dry-run
rc=$?
check_rc "restart-fallback healed exit 0" 0 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "restart action logged" "$LOGFILE" "restart"
fi

# =============================================================================
# 11. real mode: docker info fails, no ControlPath, no containers
#     -> colima stop AND colima start called; docker never recovers -> exit 1
# =============================================================================
reset_state
cat > "$STUBBIN/docker" <<EOF
#!/bin/sh
echo "docker \$*" >> "$WORKROOT/docker.calls"
exit 1
EOF
cat > "$STUBBIN/colima" <<EOF
#!/bin/sh
echo "colima \$*" >> "$WORKROOT/colima.calls"
case "\$1" in
    status)
        printf '%s\n' "colima is running using macOS Virtualization.Framework" >&2
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
run_watchdog
rc=$?
check_rc "real-mode heal attempted but stubs never recover -> exit 1" 1 "$rc"
if [ -f "$WORKROOT/actions.log" ]; then
    check_grep "colima stop was called" "$WORKROOT/actions.log" "colima stop"
    check_grep "colima start was called" "$WORKROOT/actions.log" "colima start"
else
    fail "real-mode heal: actions.log not found"
fi

# =============================================================================
# 11b. real mode: colima stop fails -> "not healed", exit 1, start not called
# =============================================================================
reset_state
cat > "$STUBBIN/docker" <<EOF
#!/bin/sh
echo "docker \$*" >> "$WORKROOT/docker.calls"
exit 1
EOF
cat > "$STUBBIN/colima" <<EOF
#!/bin/sh
echo "colima \$*" >> "$WORKROOT/colima.calls"
case "\$1" in
    status)
        printf '%s\n' "colima is running using macOS Virtualization.Framework" >&2
        exit 0
        ;;
    stop)
        exit 42
        ;;
    start)
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
EOF
run_watchdog
rc=$?
check_rc "colima stop failure -> exit 1 (not healed)" 1 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "colima stop rc logged" "$LOGFILE" "colima stop failed (rc=42)"
fi
if [ -f "$WORKROOT/actions.log" ]; then
    check_not_grep "colima start not attempted after stop failure" "$WORKROOT/actions.log" "colima start"
fi

# =============================================================================
# 11c. real mode: colima stop succeeds, colima start fails -> exit 1 (not
#      healed), start failure logged with its rc
# =============================================================================
reset_state
cat > "$STUBBIN/docker" <<EOF
#!/bin/sh
echo "docker \$*" >> "$WORKROOT/docker.calls"
exit 1
EOF
cat > "$STUBBIN/colima" <<EOF
#!/bin/sh
echo "colima \$*" >> "$WORKROOT/colima.calls"
case "\$1" in
    status)
        printf '%s\n' "colima is running using macOS Virtualization.Framework" >&2
        exit 0
        ;;
    stop)
        exit 0
        ;;
    start)
        exit 7
        ;;
    *)
        exit 0
        ;;
esac
EOF
run_watchdog
rc=$?
check_rc "colima start failure -> exit 1 (not healed)" 1 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "colima start rc logged" "$LOGFILE" "colima start failed (rc=7)"
    check_grep "not healed outcome logged" "$LOGFILE" "not healed"
fi

# =============================================================================
# 11d. real mode: heal command fails but docker is already reachable on the
#      first verify -> exit 0 (recovered). Proves exit 0 is gated on
#      docker_info_check, not on the heal command's return status.
# =============================================================================
reset_state
cat > "$STUBBIN/docker" <<EOF
#!/bin/sh
echo "docker \$*" >> "$WORKROOT/docker.calls"
exit 0
EOF
cat > "$STUBBIN/colima" <<EOF
#!/bin/sh
echo "colima \$*" >> "$WORKROOT/colima.calls"
case "\$1" in
    status)
        printf '%s\n' "colima is running using macOS Virtualization.Framework" >&2
        exit 0
        ;;
    stop)
        exit 0
        ;;
    start)
        exit 7
        ;;
    *)
        exit 0
        ;;
esac
EOF
run_watchdog
rc=$?
check_rc "heal step fails but docker already reachable -> exit 0" 0 "$rc"
if [ -f "$LOGFILE" ]; then
    check_grep "colima start rc logged" "$LOGFILE" "colima start failed (rc=7)"
    check_grep "recovery logged" "$LOGFILE" "restart succeeded; docker reachable"
fi

# =============================================================================
# 13. real mode: ControlPath present, no containers -> re-forward (ssh)
#     attempted first; the parsed ControlPath rides the -S arg; stubs never
#     recover -> escalate to restart -> exit 1
# =============================================================================
reset_state
make_control_path
make_stubs 1 "ERROR: cannot connect" 0 0 "$STATUS_RUNNING" 0 ""
run_watchdog
rc=$?
check_rc "real-mode re-forward attempted, stubs never recover -> exit 1" 1 "$rc"
if [ -f "$WORKROOT/actions.log" ]; then
    check_grep "re-forward used ssh" "$WORKROOT/actions.log" "ssh"
    check_grep "re-forward used the parsed ControlPath" "$WORKROOT/actions.log" "ssh -S .*/_lima/colima/ssh.sock"
else
    fail "real-mode re-forward: actions.log not found"
fi

# =============================================================================
# 19. env override with garbage -> exit 64 (usage/env error), no heal
# =============================================================================
reset_state
make_stubs 0 "Server: healthy" 0 0 "$STATUS_RUNNING" 0 ""
PATH="$STUBBIN:$PATH" \
HOME="$FAKEHOME" \
D33D_WD_LOCKDIR="$LOCKDIR" \
D33D_WD_RETRY_COUNT=abc \
sh "$WATCHDOG" --dry-run
rc=$?
check_rc "garbage numeric env override -> exit 64" 64 "$rc"

# =============================================================================
# 20. hung docker info -> run_with_timeout kills the child, check fails,
#     script proceeds to the VM-down path (status not running, dry-run),
#     and does not hang. Bounded by the 1 s DOCKER_TIMEOUT override.
# =============================================================================
reset_state
cat > "$STUBBIN/docker" <<'EOF'
#!/bin/sh
echo "docker $*" >> "$WORKROOT/docker.calls"
sleep 30
EOF
cat > "$STUBBIN/colima" <<'EOF'
#!/bin/sh
echo "colima $*" >> "$WORKROOT/colima.calls"
case "$1" in
    status)
        printf '%s\n' "Instance colima is not running" >&2
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
EOF
start_ts=$(date +%s)
PATH="$STUBBIN:$PATH" \
HOME="$FAKEHOME" \
D33D_WD_LOCKDIR="$LOCKDIR" \
D33D_WD_DOCTIMEOUT=1 \
D33D_WD_RETRY_COUNT=1 \
D33D_WD_RETRY_SLEEP=0 \
sh "$WATCHDOG" --dry-run
rc=$?
end_ts=$(date +%s)
elapsed=$((end_ts - start_ts))
check_rc "hung docker info -> dry-run start path exit 0" 0 "$rc"
if [ "$elapsed" -gt 10 ]; then
    fail "run_with_timeout bound broken: took ${elapsed}s"
else
    pass "hung docker info bounded (${elapsed}s)"
fi

# =============================================================================
# summary
# =============================================================================
summary
