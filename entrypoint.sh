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

# Parse params.json if present — defines become -Dname=value pairs
# (params.json is optional; the caller writes it only when defines are set)
#
# The AUTHORITATIVE validation of params.json — real JSON parsing, the
# defines-value size cap, and the rejection of control characters in keys
# and values — lives in the Python caller (d33d.render_worker.parse_defines),
# which runs BEFORE docker run. A malformed or out-of-bounds params.json
# aborts the render on the host, so the entrypoint never sees one.
#
# This block is a second, deliberately TRIVIAL gate: it keeps the entrypoint
# safe even for a caller that skips validation. python3 and jq are NOT in the
# base image (openscad/openscad:trixie), so the gate is a one-line-per-pair
# awk check — it extracts the "defines" object (the first top-level key whose
# value is an object) and walks its "key": "value" pairs, requiring every
# string to be a well-formed single-line JSON string and every value to stay
# under a fixed length. A value can only carry a literal tab if it is
# escaped (\\t), which the one-line shape forbids, so no tab can ever reach
# the -D flag line. Any violation (malformed JSON, a non-string or oversized
# value, an unclosed object) aborts the entrypoint before any openscad
# invocation runs, since a malformed params.json is a caller bug, not a
# partial-render failure covered by the STL/PNG step contract.
DEFINES_ARGS=()
if [ -f "${WORKDIR}/params.json" ]; then
    defines_tsv="$(mktemp)"
    if ! awk '
        BEGIN { DEF = 4096; found = 0 }
        { lines[NR] = $0 }
        END {
            text = ""
            for (i = 1; i <= NR; i++) text = text lines[i] "\n"
            rest = text
            while (match(rest, /"defines"[[:space:]]*:/) > 0) {
                after = substr(rest, RSTART + RLENGTH)
                if (after ~ /^[[:space:]]*\{/) {
                    found = 1
                    body = substr(after, index(after, "{") + 1)
                    break
                }
                rest = substr(rest, RSTART + RLENGTH)
            }
            if (!found) {
                print "[entrypoint] params.json: no \"defines\" object (must be absent or an object)" > "/dev/stderr"
                exit 1
            }
            rest = body
            while (1) {
                # Skip any leading comma or whitespace between pairs
                while (match(rest, /^[[:space:]]*,[[:space:]]*/) > 0) {
                    rest = substr(rest, RLENGTH + 1)
                }
                # If next is the closing brace, we are done
                if (rest ~ /^\}/) break
                m = match(rest, /^[[:space:]]*"[^"]*"[[:space:]]*:/)
                if (m == 0) break
                # Extract the key: first " ... second " in key_match
                key_match = substr(rest, m, RLENGTH)
                q1 = index(key_match, "\"")
                if (q1 == 0) { key = "" } else {
                    after_q1 = substr(key_match, q1 + 1)
                    q2 = index(after_q1, "\"")
                    if (q2 == 0) { key = "" } else {
                        key = substr(after_q1, 1, q2 - 1)
                    }
                }
                rest = substr(rest, m + RLENGTH)
                if (!match(rest, /^[[:space:]]*"[^\"]*"/)) {
                    print "[entrypoint] params.json: defines value is not a well-formed single-line string" > "/dev/stderr"
                    exit 1
                }
                s = substr(rest, RSTART + 1, RLENGTH - 1)
                # s starts at the opening quote of the value string
                if (length(s) - 2 > DEF) {
                    print "[entrypoint] params.json: defines value exceeds " DEF " characters" > "/dev/stderr"
                    exit 1
                }
                print key "=" substr(s, 2, length(s) - 2)
                rest = substr(rest, RSTART + RLENGTH)
            }
            if (rest !~ /^[[:space:]]*\}[[:space:]]*([,}\]])/) {
                print "[entrypoint] params.json: defines object is not closed — malformed JSON" > "/dev/stderr"
                exit 1
            }
        }
    ' "${WORKDIR}/params.json" > "${defines_tsv}"; then
        echo "[entrypoint] Malformed params.json — aborting before render" >&2
        rm -f "${defines_tsv}"
        exit 1
    fi
    while IFS= read -r pair; do
        [ -z "${pair}" ] && continue
        DEFINES_ARGS+=("-D${pair}")
    done < "${defines_tsv}"
    rm -f "${defines_tsv}"
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
# FAILED_STEPS additionally names *which* of the seven steps failed — the
# stderr line per step already existed, but the aggregate max_exit alone
# loses which specific view(s)/CSG failed, forcing a re-read of render.log
# to find out. This is purely additive: it does not change max_exit, the
# STL-abort rule, or the final exit code.
max_exit=0
declare -a FAILED_STEPS=()

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
[ "${csg_exit}" -ne 0 ] && { echo "[entrypoint] CSG export failed (exit ${csg_exit}) — continuing" >&2; max_exit=${csg_exit}; FAILED_STEPS+=("csg:${csg_exit}"); }

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
    [ "${png_exit}" -ne 0 ] && { echo "[entrypoint] PNG ${name} failed (exit ${png_exit}) — continuing" >&2; max_exit=${png_exit}; FAILED_STEPS+=("${name}:${png_exit}"); }
done

# List outputs for the caller to verify
ls -la "${STL_FILE}" "${CSG_FILE}" "${WORKDIR}"/view_*.png 2>&1 >&2 || true

# A non-aborting step failed: report the highest exit code so the caller can
# classify (the caller owns the classification table; the entrypoint only
# signals failure and lets the harvest happen). Exit non-zero if any step failed.
# FAILED_STEPS names every step that failed (e.g. "view_04_top:1") so the
# caller can see which artifact(s) to expect missing/invalid without parsing
# render.log — max_exit and the exit code are unchanged by this diagnostic.
if [ "${max_exit}" -ne 0 ]; then
    failed_list="${FAILED_STEPS[*]}"
    echo "[entrypoint] Completed with failures (max exit ${max_exit}); failed steps: ${failed_list}" >&2
    exit "${max_exit}"
fi

echo "[entrypoint] All 8 steps complete." >&2
exit 0
