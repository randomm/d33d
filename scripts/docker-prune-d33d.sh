#!/bin/sh
# docker-prune-d33d.sh — one-off cleanup for leaked d33d render/registry
# containers and volumes (issue #280).
#
# Defaults to DRY-RUN: it lists what it would remove without removing it.
# Pass --yes to actually delete.
#
# Scope (strict, by name prefix only — never touches other projects):
#   * Exited (non-running) containers named d33d-* , render-*, registry-*
#     — the render worker's and module registry's leaked containers.
#   * d33d-* and registry-* volumes that no container is attached to
#     (dangling by d33d prefix).
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

# A container is in scope when its name matches one of the d33d project's
# name prefixes. The render worker names its containers render-* and
# d33d-render-*; the module registry names its helpers registry-get-* and
# registry-put-* and its per-run containers registry-*. d33d-* is a
# catch-all for anything else the project has named d33d.
is_in_scope_name() {
    case "$1" in
        d33d-*) return 0 ;;
        render-*) return 0 ;;
        registry-*) return 0 ;;
        *) return 1 ;;
    esac
}

# A volume is in scope for dangling removal when its name starts with
# d33d- or registry- (the render worker's d33d-render-* / d33d-* volumes
# and the module registry's registry-* volumes).
is_in_scope_volume() {
    case "$1" in
        d33d-*) return 0 ;;
        registry-*) return 0 ;;
        *) return 1 ;;
    esac
}

removed=0
would_remove=0

echo "== d33d docker prune (dry-run: $( [ "$DRY_RUN" = 1 ] && echo yes || echo no ) ) =="

# ── Containers ─────────────────────────────────────────────────────────────
# Exited (not running) in-scope containers: docker ps -a filtered to
# non-running, in-scope names. For each, `docker rm -f` (force, best-effort).
# We iterate names, not IDs, so the report is human-readable.
for name in $(docker ps -a --filter "status=exited" --format '{{.Names}}'); do
    is_in_scope_name "$name" || continue
    if [ "$DRY_RUN" = 1 ]; then
        echo "  would remove container: $name"
        would_remove=$((would_remove + 1))
    else
        if docker rm -f "$name" >/dev/null 2>&1; then
            echo "  removed container: $name"
            removed=$((removed + 1))
        else
            echo "  WARN: could not remove container: $name" >&2
        fi
    fi
done
# Also catch paused / created (never-started) in-scope containers — the
# "exited" filter above misses them and a leaked container in created/paused
# state is still a leak.
for state in created paused; do
    for name in $(docker ps -a --filter "status=$state" --format '{{.Names}}'); do
        is_in_scope_name "$name" || continue
        if [ "$DRY_RUN" = 1 ]; then
            echo "  would remove container ($state): $name"
            would_remove=$((would_remove + 1))
        else
            if docker rm -f "$name" >/dev/null 2>&1; then
                echo "  removed container ($state): $name"
                removed=$((removed + 1))
            else
                echo "  WARN: could not remove container ($state): $name" >&2
            fi
        fi
    done
done

# ── Volumes ────────────────────────────────────────────────────────────────
# Dangling by d33d prefix: a d33d-*/registry-* volume not attached to any
# container. `docker volume ls --filter dangling=true` only catches volumes
# with no *container* referencing them at all; we additionally restrict to
# in-scope prefixes so we never touch another project's dangling volume.
for vol in $(docker volume ls --filter "dangling=true" --format '{{.Name}}'); do
    is_in_scope_volume "$vol" || continue
    if [ "$DRY_RUN" = 1 ]; then
        echo "  would remove volume: $vol"
        would_remove=$((would_remove + 1))
    else
        if docker volume rm "$vol" >/dev/null 2>&1; then
            echo "  removed volume: $vol"
            removed=$((removed + 1))
        else
            echo "  WARN: could not remove volume: $vol" >&2
        fi
    fi
done

echo "== done: $removed removed, $would_remove would be removed (dry-run) =="
exit 0
