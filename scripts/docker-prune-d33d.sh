#!/bin/sh
# docker-prune-d33d.sh — one-off cleanup for leaked d33d render/registry
# containers and volumes (issue #280).
#
# Defaults to DRY-RUN: it lists what it would remove without removing it.
# Pass --yes to actually delete.
#
# Scope (strict, exact patterns only — never touches other projects):
#   Containers:
#     * render-<8 lowercase hex>  (e.g. render-0123abcd)
#     * registry-get-<8 hex>      (e.g. registry-get-0123abcd)
#     * registry-put-<8 hex>      (e.g. registry-put-0123abcd)
#   Volumes:
#     * d33d-render-render-*
#     * d33d-*
#     * registry-<hex>
#
#   docker-prune-d33d.sh          # dry-run (default)
#   docker-prune-d33d.sh --yes    # actually delete
#
# POSIX sh. No bash-isms. No external deps beyond the docker CLI.

set -u

DRY_RUN=1
for arg in "$@"; do
    case "$arg" in
        -y|--yes) DRY_RUN=0 ;;
        -h|--help)
            echo "usage: docker-prune-d33d.sh [--yes]"
            exit 0
            ;;
        *)
            echo "docker-prune-d33d.sh: unknown argument: $arg" >&2
            echo "usage: docker-prune-d33d.sh [--yes]" >&2
            exit 2
            ;;
    esac
done

if ! command -v docker >/dev/null 2>&1; then
    echo "docker-prune-d33d.sh: docker CLI not found on PATH" >&2
    exit 1
fi

# Exact container-name patterns (POSIX case):
#   render-<8 lowercase hex>
#   registry-get-<8 hex>
#   registry-put-<8 hex>
is_in_scope_container() {
    case "$1" in
        render-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) return 0 ;;
        registry-get-[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]) return 0 ;;
        registry-put-[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]) return 0 ;;
        *) return 1 ;;
    esac
}

# Exact volume-name patterns (POSIX case):
#   d33d-render-render-*
#   d33d-*
#   registry-<hex+>
is_in_scope_volume() {
    case "$1" in
        d33d-render-render-*) return 0 ;;
        d33d-*) return 0 ;;
        registry-[0-9a-fA-F]*) return 0 ;;
        *) return 1 ;;
    esac
}

removed=0
would_remove=0

# Shared dry-run / remove / WARN block for a single name.
# $1 = label (e.g. "container", "volume"), $2 = name, $3 = remove command
process_one() {
    label=$1
    name=$2
    rm_cmd=$3
    if [ "$DRY_RUN" = 1 ]; then
        echo "  would remove $label: $name"
        would_remove=$((would_remove + 1))
    else
        if $rm_cmd >/dev/null 2>&1; then
            echo "  removed $label: $name"
            removed=$((removed + 1))
        else
            echo "  WARN: could not remove $label: $name" >&2
        fi
    fi
}

echo "== d33d docker prune (dry-run: $( [ "$DRY_RUN" = 1 ] && echo yes || echo no ) ) =="

# ── Containers ─────────────────────────────────────────────────────────────
# Exited (not running) in-scope containers.
while IFS= read -r name; do
    [ -z "$name" ] && continue
    is_in_scope_container "$name" || continue
    process_one "container" "$name" "docker rm -f $name"
done <<EOF
$(docker ps -a --filter "status=exited" --format '{{.Names}}')
EOF

# Also catch paused / created (never-started) in-scope containers.
for state in created paused; do
    while IFS= read -r name; do
        [ -z "$name" ] && continue
        is_in_scope_container "$name" || continue
        process_one "container ($state)" "$name" "docker rm -f $name"
    done <<EOF
$(docker ps -a --filter "status=$state" --format '{{.Names}}')
EOF
done

# ── Volumes ────────────────────────────────────────────────────────────────
# Dangling in-scope volumes.
while IFS= read -r vol; do
    [ -z "$vol" ] && continue
    is_in_scope_volume "$vol" || continue
    process_one "volume" "$vol" "docker volume rm $vol"
done <<EOF
$(docker volume ls --filter "dangling=true" --format '{{.Name}}')
EOF

echo "== done: $removed removed, $would_remove would be removed (dry-run) =="
exit 0
