#!/bin/sh
# docker-prune-d33d.sh — one-off cleanup for leaked d33d render/registry
# containers and volumes (issue #280).
#
# Defaults to DRY-RUN: it lists what it would remove without removing it.
# Pass --yes to actually delete.
#
# Scope (strict, exact patterns only — a name is in scope ONLY if it
# matches one of these; anything else is left alone):
#   Containers:
#     * render-<8 lowercase hex>  (e.g. render-0123abcd)
#     * registry-get-<8 hex>      (e.g. registry-get-0123abcd)
#     * registry-put-<8 hex>      (e.g. registry-put-0123abcd)
#   Volumes:
#     * d33d-render-render-*
#     * d33d-*
#     * registry-<8 hex>          (e.g. registry-0123abcd)
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
#   registry-<exactly 8 hex> (mirrors the registry container arms)
is_in_scope_volume() {
    case "$1" in
        d33d-render-render-*) return 0 ;;
        d33d-*) return 0 ;;
        registry-[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]) return 0 ;;
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
LISTED_VOLUMES=""
while IFS= read -r vol; do
    [ -z "$vol" ] && continue
    is_in_scope_volume "$vol" || continue
    process_one "volume" "$vol" "docker volume rm $vol"
    LISTED_VOLUMES="$LISTED_VOLUMES $vol "
done <<EOF
$(docker volume ls --filter "dangling=true" --format '{{.Name}}')
EOF

# ── Dry-run only: orphan prediction ────────────────────────────────────────
# Besides the dangling list above, the dry run also names every in-scope
# volume that the container removals in this run would ORPHAN: a volume that
# no container in the final (post-removal) set mounts (checked via
# `docker inspect` of the Mounts of every container that stays). It is never
# predicted twice (deduped against the dangling list) and never predicted
# while a surviving container still mounts it. The real (--yes) run does not
# do this: its volume removal already picks up newly-dangling volumes via the
# filter above.
if [ "$DRY_RUN" = 1 ]; then
    # The mount names of the containers this run removes (exited/created/
    # paused in-scope containers), one JSON entry per line; an inspect
    # failure (container gone mid-run) contributes nothing.
    REMOVED_MOUNTS="$(
        for state in exited created paused; do
            while IFS= read -r name; do
                [ -z "$name" ] && continue
                is_in_scope_container "$name" || continue
                docker inspect --format '{{json .Mounts}}' "$name" 2>/dev/null || true
            done <<EOF2
$(docker ps -a --filter "status=$state" --format '{{.Names}}')
EOF2
        done | sed 's/},{/{/g' | sed -n 's/.*"Type":"volume".*"Name":"\([^"]*\)".*/\1/p'
    )"
    while IFS= read -r vol; do
        [ -z "$vol" ] && continue
        is_in_scope_volume "$vol" || continue
        # Never predict a volume any container that survives this run still
        # mounts. `docker ps` (no -a) lists exactly the running containers:
        # every in-scope name in it survives, because the removals above
        # only take exited / created / paused containers.
        still_mounted=0
        while IFS= read -r other; do
            [ -z "$other" ] && continue
            mounts_json=$(docker inspect --format '{{json .Mounts}}' "$other" 2>/dev/null) || continue
            case "$mounts_json" in
                *"\"Name\":\"$vol\""*) still_mounted=1; break ;;
                *) ;;
            esac
        done <<EOF
$(docker ps --format '{{.Names}}')
EOF
        [ "$still_mounted" = 1 ] && continue
        case "$LISTED_VOLUMES" in
            *" $vol "*) continue ;;
            *) LISTED_VOLUMES="$LISTED_VOLUMES $vol "
               process_one "volume (orphaned by this run)" "$vol" "docker volume rm $vol" ;;
        esac
    done <<EOF
$REMOVED_MOUNTS
EOF
fi

echo "== done: $removed removed, $would_remove would be removed (dry-run) =="
exit 0
