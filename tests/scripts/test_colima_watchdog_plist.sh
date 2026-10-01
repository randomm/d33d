#!/bin/sh
# test_colima_watchdog_plist.sh — validates scripts/com.d33d.colima-watchdog.plist
# (issue #326). The template must carry an EnvironmentVariables block whose
# PATH includes /opt/homebrew/bin and /usr/local/bin (launchd's default PATH
# lacks both), and the rendered plist (after __HOME__ substitution) must be
# well-formed — checked by `plutil -lint` when available, else grep-based
# structure checks.
#
# Run from anywhere:
#   sh tests/scripts/test_colima_watchdog_plist.sh

set -u

# --- locate the plist under test --------------------------------------------
TEST_DIR=$(dirname -- "$0")
TEST_DIR=$(cd "$TEST_DIR" && pwd)
SCRIPT_DIR=$(cd "$TEST_DIR/../.." && pwd)
PLIST="$SCRIPT_DIR/scripts/com.d33d.colima-watchdog.plist"

if [ ! -f "$PLIST" ]; then
    echo "FATAL: $PLIST not found" >&2
    exit 1
fi

# --- scratch + counters -------------------------------------------------------
WORKROOT=$(mktemp -d "${TMPDIR:-/tmp}/colima-watchdog-plist.XXXXXX")
RENDERED="$WORKROOT/rendered.plist"
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

# --- render the template with __HOME__ substituted ---------------------------
sed "s|__HOME__|$HOME|g" "$PLIST" > "$RENDERED"

# =============================================================================
# 1. rendered: no __HOME__ left
# =============================================================================
if grep -q "__HOME__" "$RENDERED" 2>/dev/null; then
    fail "rendered plist still contains __HOME__"
else
    pass "rendered plist has no __HOME__ left"
fi

# =============================================================================
# 2. rendered: ProgramArguments point at the substituted home path
# =============================================================================
check_grep "rendered path uses real home" "$RENDERED" "$HOME/Library/Scripts/colima-watchdog.sh"

# =============================================================================
# 3. template: EnvironmentVariables block present
# =============================================================================
check_grep "template has EnvironmentVariables key" "$PLIST" "<key>EnvironmentVariables</key>"
check_grep "template has PATH key" "$PLIST" "<key>PATH</key>"

# =============================================================================
# 4. template: PATH contains /opt/homebrew/bin and /usr/local/bin
# =============================================================================
check_grep "PATH includes /opt/homebrew/bin" "$PLIST" "/opt/homebrew/bin"
check_grep "PATH includes /usr/local/bin" "$PLIST" "/usr/local/bin"

# The full documented value, single line.
check_grep "PATH value is the documented string" "$PLIST" \
    "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# =============================================================================
# 5. rendered plist is valid: plutil -lint when available, else structural
#    grep fallback (balanced dict keys, plist wrapper, xml header)
# =============================================================================
if command -v plutil >/dev/null 2>&1; then
    if plutil -lint "$RENDERED" >/dev/null 2>&1; then
        pass "plutil -lint accepts the rendered plist"
    else
        fail "plutil -lint rejected the rendered plist"
    fi
else
    check_grep "fallback: xml header" "$RENDERED" '<?xml version="1.0" encoding="UTF-8"?>'
    check_grep "fallback: plist open tag" "$RENDERED" "<plist version=\"1.0\">"
    check_grep "fallback: plist close tag" "$RENDERED" "</plist>"
    check_grep "fallback: dict open tag" "$RENDERED" "<dict>"
    check_grep "fallback: dict close tag" "$RENDERED" "</dict>"
fi

# =============================================================================
# summary
# =============================================================================
echo
echo "PASS=$PASS FAIL=$FAIL"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
