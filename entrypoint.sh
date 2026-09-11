#!/bin/bash
#
# d33d render worker entrypoint
#
# Reads: /work/model.scad, /work/params.json (optional)
# Writes: /work/model.stl, /work/model.csg, /work/view_0{0..5}_*.png
#
# Eight sequential openscad invocations:
#   1. STL export
#   2. CSG export
#   3–8. Six PNG renders (front, back, left, right, top, iso)
#
# A non-zero exit on the STL invocation (step 1) aborts the remaining seven
# immediately. A non-zero exit on any of the remaining SEVEN invocations does
# NOT abort the run — the remaining invocations still execute and the entrypoint
# exits non-zero so the caller can classify (the class-5 "artifact_error" row is
# exactly this shape: STL present but a later PNG/CSG invocation failed). This is
# required because `set -e` is active: without the per-step `||` guards, a single
# failed openscad call would silently kill the whole entrypoint before the PNGs
# run. Each invocation's stderr is appended to /work/render.log.
#
# Camera tuples (verified empirically against openscad/openscad:trixie, OpenSCAD 2026.01.19):
#   The --camera flag uses: translate_x,translate_y,translate_z,rot_x,rot_y,rot_z,dist
#   With --autocenter, the origin is shifted to the object's bounding-box centre
#   before the rotation is applied.
#
#   View 0 (front): 0,0,0,0,0,0,40    — camera on -Z axis, no rotation
#   View 1 (back):  0,0,0,0,180,0,40  — 180° about Y
#   View 2 (left):  0,0,0,0,90,0,40   — 90° about Y  (red slab x=0 appears on the left of image)
#   View 3 (right): 0,0,0,0,-90,0,40  — -90° about Y (red slab x=0 appears on the right of image)
#   View 4 (top):   0,0,0,90,0,0,40   — 90° about X  (looking down from +Z)
#   View 5 (iso):   0,0,0,0,45,45,55  — 45° about Y then 45° about Z (isometric corner view)
#
# The 7-element tuple order is (tx, ty, tz, rx, ry, rz, dist).
# The spec's VIEWS constant in d33d/__init__.py pins these exact values.

set -euo pipefail

WORKDIR=/work
SCAD_FILE="${WORKDIR}/model.scad"
STL_FILE="${WORKDIR}/model.stl"
CSG_FILE="${WORKDIR}/model.csg"
LOG_FILE="${WORKDIR}/render.log"
IMG_SIZE="800,800"
PROJECTION="o"   # orthographic
RENDER_FLAG="--render"

# Parse params.json if present — defines are -Dname=value pairs
# (params.json is optional; the caller writes it only when defines are set)
DEFINES_ARGS=()
if [ -f "${WORKDIR}/params.json" ]; then
    # Use python3 if available, otherwise fall back to a simple sed-based approach.
    # python3 is NOT in the base image, so we use a minimal awk parser.
    # params.json format: {"defines": {"NAME": "value", ...}, ...}
    # The values are strings — the caller has already escaped them.
    # For this first implementation we parse with a grep/sed pipeline.
    # A more robust parser would be added in a follow-up ticket.
    while IFS= read -r line; do
        # Lines look like:   "NAME": "value"
        name=$(echo "$line" | sed 's/^ *//' | cut -d'"' -f2)
        value=$(echo "$line" | cut -d'"' -f4)
        DEFINES_ARGS+=("-D${name}=${value}")
    done < <(sed -n '/"defines"/,/^ *}/p' "${WORKDIR}/params.json" \
             | grep -E '^[[:space:]]*"[A-Za-z_][A-Za-z0-9_]*":' \
             | grep -v '"defines"' || true)
fi

# Common flags shared by all openscad invocations.
# The Manifold backend (the default in OpenSCAD 2026.01.19) is used for
# reliable colour output in CSG and PNG renders; CGAL is the older backend.
COMMON_FLAGS=(
    --autocenter
    --projection "${PROJECTION}"
    --imgsize "${IMG_SIZE}"
)

# Truncate the log file at the start of each render run
: > "${LOG_FILE}"

echo "[entrypoint] Starting render of ${SCAD_FILE}" >&2

# Track non-zero exits from the seven non-ABORTING steps (CSG + 6 PNGs).
# The STL step is the only one that aborts immediately (see below).
max_exit=0

# ── Step 1: STL export (ABORTING on failure) ────────────────────────────────
echo "[entrypoint] Step 1/8: STL export" >&2
# NOTE: under `set -e`, a failing command in a condition (||, if, while) does
# not trigger errexit, so `openscad ... || stl_exit=$?` is safe: the script
# survives the failure, records the exit code, and the `if` below decides.
stl_exit=0
openscad \
    "${COMMON_FLAGS[@]}" \
    "${DEFINES_ARGS[@]}" \
    -o "${STL_FILE}" \
    "${SCAD_FILE}" \
    2>>"${LOG_FILE}" \
    || stl_exit=$?

if [ "${stl_exit}" -ne 0 ]; then
    echo "[entrypoint] STL export failed with exit code ${stl_exit} — aborting remaining steps" >&2
    exit "${stl_exit}"
fi

# ── Step 2: CSG export (non-aborting) ───────────────────────────────────────
echo "[entrypoint] Step 2/8: CSG export" >&2
csg_exit=0
openscad \
    "${COMMON_FLAGS[@]}" \
    "${DEFINES_ARGS[@]}" \
    -o "${CSG_FILE}" \
    "${SCAD_FILE}" \
    2>>"${LOG_FILE}" \
    || csg_exit=$?
[ "${csg_exit}" -ne 0 ] && { echo "[entrypoint] CSG export failed (exit ${csg_exit}) — continuing" >&2; max_exit=${csg_exit}; }

# ── Steps 3–8: Six PNG renders (non-aborting) ───────────────────────────────
# Camera tuples: (tx, ty, tz, rx, ry, rz, dist)
# Values verified empirically against openscad/openscad:trixie (OpenSCAD 2026.01.19).
# See the comment block at the top of this file for the full derivation.

declare -a VIEW_NAMES=(
    "view_00_front"
    "view_01_back"
    "view_02_left"
    "view_03_right"
    "view_04_top"
    "view_05_iso"
)

declare -a VIEW_CAMERAS=(
    "0,0,0,0,0,0,40"     # front:  camera on -Z, no rotation
    "0,0,0,0,180,0,40"   # back:   180° about Y
    "0,0,0,0,90,0,40"    # left:   90° about Y
    "0,0,0,0,-90,0,40"   # right:  -90° about Y
    "0,0,0,90,0,0,40"    # top:    90° about X (looking down from +Z)
    "0,0,0,0,45,45,55"   # iso:    45° about Y + 45° about Z (isometric)
)

for i in 0 1 2 3 4 5; do
    step=$(( i + 3 ))
    name="${VIEW_NAMES[$i]}"
    cam="${VIEW_CAMERAS[$i]}"
    png_file="${WORKDIR}/${name}.png"

    echo "[entrypoint] Step ${step}/8: PNG ${name}" >&2
    png_exit=0
    openscad \
        "${COMMON_FLAGS[@]}" \
        "${DEFINES_ARGS[@]}" \
        ${RENDER_FLAG} \
        --camera "${cam}" \
        --colorscheme "Tomorrow Night" \
        -o "${png_file}" \
        "${SCAD_FILE}" \
        2>>"${LOG_FILE}" \
        || png_exit=$?
    [ "${png_exit}" -ne 0 ] && { echo "[entrypoint] PNG ${name} failed (exit ${png_exit}) — continuing" >&2; max_exit=${png_exit}; }
done

# List outputs for the caller to verify
ls -la "${STL_FILE}" "${CSG_FILE}" "${WORKDIR}"/view_*.png 2>&1 >&2 || true

# A non-aborting step failed: report the highest exit code so the caller can
# classify (the caller owns the classification table; the entrypoint only
# signals failure and lets the harvest happen). Exit non-zero if any step failed.
if [ "${max_exit}" -ne 0 ]; then
    echo "[entrypoint] Completed with failures (max exit ${max_exit})." >&2
    exit "${max_exit}"
fi

echo "[entrypoint] All 8 steps complete." >&2
exit 0
