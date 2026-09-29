#!/bin/sh
# test_docker_prune_d33d.sh — POSIX-sh tests for scripts/docker-prune-d33d.sh
# (issues #280 / #304). Drives the script with a stubbed `docker` on PATH so
# the test never touches a real daemon.
#
# Run from anywhere:
#   sh tests/scripts/test_docker_prune_d33d.sh

set -u

# --- locate the script under test --------------------------------------------
TEST_DIR=$(dirname -- "$0")
TEST_DIR=$(cd "$TEST_DIR" && pwd)
SCRIPT_DIR=$(cd "$TEST_DIR/../.." && pwd)
SCRIPT="$SCRIPT_DIR/scripts/docker-prune-d33d.sh"

if [ ! -f "$SCRIPT" ]; then
    echo "FATAL: $SCRIPT not found" >&2
    exit 1
fi

# --- scratch + stub environment ----------------------------------------------
WORKROOT=$(mktemp -d "${TMPDIR:-/tmp}/docker-prune-test.XXXXXX")
STUBBIN="$WORKROOT/bin"
mkdir -p "$STUBBIN"
trap 'rm -rf "$WORKROOT"' EXIT

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

# make_docker_stub — replaces the stubbed docker on PATH.
# Writes the inspect lookup table to $WORKROOT/inspect.data.
# $1 = exited names (newline-separated, empty = none)
# $2 = created names
# $3 = paused names
# $4 = running names
# $5 = dangling volumes (newline-separated)
# $6 = inspect JSON responses (newline-separated, one per line,
#      each line: <container-name>\t<json>)
make_docker_stub() {
    vols_json=$1

    # Write the inspect lookup table.
    printf '%s' "$vols_json" > "$WORKROOT/inspect.data"

    cat > "$STUBBIN/docker" <<'STUB_EOF'
#!/bin/sh
echo "docker $*" >> "$WORKROOT/docker.calls"
ps_mode=""
for a in "$@"; do
    case "$a" in
        status=exited)  ps_mode=exited ;;
        status=created) ps_mode=created ;;
        status=paused)  ps_mode=paused ;;
    esac
done
if [ "${1:-}" = "ps" ] && [ -z "$ps_mode" ]; then
    ps_mode=running
fi
case "${1:-}" in
    ps)
        case "$ps_mode" in
            exited)  printf '%s' "$PS_EXITED" ;;
            created) printf '%s' "$PS_CREATED" ;;
            paused)  printf '%s' "$PS_PAUSED" ;;
            running) printf '%s' "$PS_RUNNING" ;;
        esac
        ;;
    volume)
        printf '%s' "$VOL_DANGLING"
        ;;
    inspect)
        last=""
        for a in "$@"; do last="$a"; done
        result=$(grep -F "$last" "$WORKROOT/inspect.data" 2>/dev/null)
        if [ -n "$result" ]; then
            printf '%s' "${result#*	}"
            exit 0
        fi
        exit 1
        ;;
esac
exit 0
STUB_EOF
    # The stub reads PS_*/VOL_* from env vars set by the caller.
    chmod +x "$STUBBIN/docker"
}

# run_prune <args...> — runs the script with the stubbed docker on PATH.
# PS_EXITED, PS_CREATED, PS_PAUSED, PS_RUNNING, VOL_DANGLING must be set
# as shell variables (they are exported to the stub via the environment).
run_prune() {
    PATH="$STUBBIN:$PATH" \
    PS_EXITED="$PS_EXITED" \
    PS_CREATED="$PS_CREATED" \
    PS_PAUSED="$PS_PAUSED" \
    PS_RUNNING="$PS_RUNNING" \
    VOL_DANGLING="$VOL_DANGLING" \
    WORKROOT="$WORKROOT" \
    sh "$SCRIPT" "$@"
}

OUTFILE="$WORKROOT/out.txt"

# Helper to set all five stub vars at once.
# set_stub <exited> <created> <paused> <running> <dangling> <inspect_lines>
set_stub() {
    PS_EXITED=$1
    PS_CREATED=$2
    PS_PAUSED=$3
    PS_RUNNING=$4
    VOL_DANGLING=$5
    vols_json=$6
    make_docker_stub "$vols_json"
}

# =============================================================================
# 1. exact-8-hex positive: dangling volume registry-0123abcd (lowercase)
#    is in scope and appears in the dry-run output.
# =============================================================================
set_stub "" "" "" "" "registry-0123abcd" ""
run_prune > "$OUTFILE" 2>&1
check_rc "exact-8-hex positive volume in dry-run" 0 "$?"
check_grep "registry-0123abcd listed" "$OUTFILE" "registry-0123abcd"

# =============================================================================
# 2. exact-8-hex positive: dangling volume registry-DEADBEEF (uppercase)
#    is in scope
# =============================================================================
set_stub "" "" "" "" "registry-DEADBEEF" ""
run_prune > "$OUTFILE" 2>&1
check_rc "exact-8-hex uppercase volume in dry-run" 0 "$?"
check_grep "registry-DEADBEEF listed" "$OUTFILE" "registry-DEADBEEF"

# =============================================================================
# 3. exact-8-hex negative: registry-abcdef0 (7 hex) is NOT in scope
# =============================================================================
set_stub "" "" "" "" "registry-abcdef0" ""
run_prune > "$OUTFILE" 2>&1
check_rc "7-hex negative exit 0" 0 "$?"
check_not_grep "registry-abcdef0 (7 hex) not listed" "$OUTFILE" "registry-abcdef0"

# =============================================================================
# 4. exact-8-hex negative: registry-abcdef012 (9 hex) is NOT in scope
# =============================================================================
set_stub "" "" "" "" "registry-abcdef012" ""
run_prune > "$OUTFILE" 2>&1
check_rc "9-hex negative exit 0" 0 "$?"
check_not_grep "registry-abcdef012 (9 hex) not listed" "$OUTFILE" "registry-abcdef012"

# =============================================================================
# 5. exact-8-hex negative: registry-data is NOT in scope
# =============================================================================
set_stub "" "" "" "" "registry-data" ""
run_prune > "$OUTFILE" 2>&1
check_rc "registry-data negative exit 0" 0 "$?"
check_not_grep "registry-data not listed" "$OUTFILE" "registry-data"

# =============================================================================
# 6. exact-8-hex negative: registry-cache is NOT in scope
# =============================================================================
set_stub "" "" "" "" "registry-cache" ""
run_prune > "$OUTFILE" 2>&1
check_rc "registry-cache negative exit 0" 0 "$?"
check_not_grep "registry-cache not listed" "$OUTFILE" "registry-cache"

# =============================================================================
# 7. exact-8-hex negative: registry-abc is NOT in scope
# =============================================================================
set_stub "" "" "" "" "registry-abc" ""
run_prune > "$OUTFILE" 2>&1
check_rc "registry-abc negative exit 0" 0 "$?"
check_not_grep "registry-abc not listed" "$OUTFILE" "registry-abc"

# =============================================================================
# 8. d33d-* volume is in scope (broad arm, unchanged)
# =============================================================================
set_stub "" "" "" "" "d33d-render-render-xyz" ""
run_prune > "$OUTFILE" 2>&1
check_rc "d33d-render-render-xyz in scope" 0 "$?"
check_grep "d33d-render-render-xyz listed" "$OUTFILE" "d33d-render-render-xyz"

# =============================================================================
# 9. exact-8-hex positive: render-0123abcd (lowercase) container is in scope
# =============================================================================
set_stub "render-0123abcd" "" "" "" "" ""
run_prune > "$OUTFILE" 2>&1
check_rc "render-0123abcd container in scope" 0 "$?"
check_grep "render-0123abcd container listed" "$OUTFILE" "render-0123abcd"

# =============================================================================
# 10. exact-8-hex negative: render-abcdef012 (9 hex) container NOT in scope
# =============================================================================
set_stub "render-abcdef012" "" "" "" "" ""
run_prune > "$OUTFILE" 2>&1
check_rc "render-abcdef012 (9 hex) container exit 0" 0 "$?"
check_not_grep "render-abcdef012 not listed" "$OUTFILE" "render-abcdef012"

# =============================================================================
# 11. registry-get-0123abcd container is in scope
# =============================================================================
set_stub "registry-get-0123abcd" "" "" "" "" ""
run_prune > "$OUTFILE" 2>&1
check_rc "registry-get-0123abcd container in scope" 0 "$?"
check_grep "registry-get-0123abcd listed" "$OUTFILE" "registry-get-0123abcd"

# =============================================================================
# 12. registry-put-deadbeef container is in scope
# =============================================================================
set_stub "registry-put-deadbeef" "" "" "" "" ""
run_prune > "$OUTFILE" 2>&1
check_rc "registry-put-deadbeef container in scope" 0 "$?"
check_grep "registry-put-deadbeef listed" "$OUTFILE" "registry-put-deadbeef"

# =============================================================================
# 13. dry-run orphan prediction: a volume mounted only by a to-be-removed
#     container is listed.
# =============================================================================
set_stub "render-0123abcd" "" "" "" "" \
    "render-0123abcd	[{\"Type\":\"volume\",\"Name\":\"d33d-orphan\",\"Source\":\"/s\",\"Destination\":\"/d\"}]"
run_prune > "$OUTFILE" 2>&1
check_rc "orphan prediction exit 0" 0 "$?"
check_grep "d33d-orphan predicted" "$OUTFILE" "d33d-orphan"

# =============================================================================
# 14. dry-run dedupe: a volume appearing in both the dangling list and the
#     orphan set is listed exactly once.
# =============================================================================
set_stub "render-0123abcd" "" "" "" "registry-0123abcd" \
    "render-0123abcd	[{\"Type\":\"volume\",\"Name\":\"registry-0123abcd\",\"Source\":\"/s\",\"Destination\":\"/d\"}]"
run_prune > "$OUTFILE" 2>&1
check_rc "dedupe exit 0" 0 "$?"
count=$(grep -c "registry-0123abcd" "$OUTFILE")
if [ "$count" -eq 1 ]; then
    pass "registry-0123abcd listed exactly once (deduped)"
else
    fail "registry-0123abcd expected 1 listing, got $count"
fi

# =============================================================================
# 15. dry-run: a volume still mounted by a running (surviving) container is
#     NOT predicted.
# =============================================================================
set_stub "render-0123abcd" "" "" "render-running01" "" \
    "render-0123abcd	[{\"Type\":\"volume\",\"Name\":\"d33d-shared\",\"Source\":\"/s\",\"Destination\":\"/d\"}]
render-running01	[{\"Type\":\"volume\",\"Name\":\"d33d-shared\",\"Source\":\"/s2\",\"Destination\":\"/d2\"}]"
run_prune > "$OUTFILE" 2>&1
check_rc "shared-with-survivor exit 0" 0 "$?"
check_not_grep "d33d-shared not predicted (still mounted by running container)" "$OUTFILE" "d33d-shared"

# =============================================================================
# 16. real (--yes) run does NOT do orphan prediction — no inspect calls
# =============================================================================
set_stub "render-0123abcd" "" "" "" "" \
    "render-0123abcd	[{\"Type\":\"volume\",\"Name\":\"d33d-orphan\",\"Source\":\"/s\",\"Destination\":\"/d\"}]"
rm -f "$WORKROOT/docker.calls"
run_prune --yes > "$OUTFILE" 2>&1
check_rc "real run exit 0" 0 "$?"
if [ -f "$WORKROOT/docker.calls" ]; then
    check_not_grep "no inspect in real run" "$WORKROOT/docker.calls" "inspect"
else
    pass "no docker.calls file (no inspect in real run)"
fi

# =============================================================================
# 17. real run: orphan volume not in dangling list is NOT removed
# =============================================================================
set_stub "render-0123abcd" "" "" "" "" \
    "render-0123abcd	[{\"Type\":\"volume\",\"Name\":\"d33d-orphan\",\"Source\":\"/s\",\"Destination\":\"/d\"}]"
rm -f "$WORKROOT/docker.calls"
run_prune --yes > "$OUTFILE" 2>&1
check_rc "real run no orphan removal exit 0" 0 "$?"
check_not_grep "d33d-orphan not removed in real run" "$OUTFILE" "d33d-orphan"

# =============================================================================
# summary
# =============================================================================
echo
echo "PASS=$PASS FAIL=$FAIL"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
