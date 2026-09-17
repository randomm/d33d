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
#   The first six tuple elements (translate + rotate) are fixed constants that
#   pin *which* face each view sees:
#
#   View 0 (front): 0,0,0,0,0,0,<dist>    — camera on -Z axis, no rotation
#   View 1 (back):  0,0,0,0,180,0,<dist>  — 180° about Y
#   View 2 (left):  0,0,0,0,90,0,<dist>   — 90° about Y  (red slab x=0 appears on the left of image)
#   View 3 (right): 0,0,0,0,-90,0,<dist>  — -90° about Y (red slab x=0 appears on the right of image)
#   View 4 (top):   0,0,0,90,0,0,<dist>   — 90° about X  (looking down from +Z)
#   View 5 (iso):   0,0,0,0,45,45,<dist>  — 45° about Y then 45° about Z (isometric corner view)
#
#   The 7th element (dist) is **not** a fixed constant. After the STL export
#   (step 1) the entrypoint parses the ASCII STL's bounding box with
#   ``awk`` (present in the base image) and computes:
#
#     max_extent = max(x_max−x_min, y_max−y_min, z_max−z_min)
#     dist       = CAM_DIST_FACTOR × max_extent          (5 axis-aligned views)
#     dist_iso   = CAM_DIST_FACTOR × √2 × max_extent     (isometric view)
#
#   CAM_DIST_FACTOR is 3.0 (mirrored from d33d.render_worker.CAM_DIST_FACTOR).
#   The √2 iso multiplier is calibrated for cube-shaped models (a cube's
#   45°-rotated silhouette is 2D-diagonal-limited: S·√2, zero z-extent).
#   For a genuine iso corner view of a full-extent box the projected
#   width is S·√3, which at CAM_DIST_FACTOR=3.0 still fits the 800×800
#   frame with ~5% margin (3.0/√3 ≈ 1.732 > 2.52 exact-fit ratio) —
#   the iso factor is the tightest case in the design.
#
#   The fit is a pure function of the bounding box — no timestamps, no
#   randomised seeds, no wall-clock — so two renders of the same source
#   produce byte-identical view PNGs (issue #111 acceptance gate).
#
#   The STL is first verified to be ASCII (its first line must start
#   with ``solid``) — a binary or partial STL is detected up front and
#   the render ABORTS (a partial STL can carry bogus coordinates that
#   would silently under-size the fit; the legacy 40.0/55.0 fallback
#   would re-overflow the very bug issue #111 fixes, and a fallback that
#   only warns on stderr would leave the host unaware the fit was
#   skipped). The caller classifies a non-zero exit with a present STL
#   as artifact_error.
#
#   The 7-element tuple order is (tx, ty, tz, rx, ry, rz, dist).
#   The spec's VIEWS constant in d33d/render_worker.py pins the rotation
#   semantics; the dist element is substituted at render time from the
#   per-model bounding box.

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

# ── Bounding-box extraction (zero extra openscad invocations) ──────────────
# Parse the ASCII STL written by step 1 to get the model's bounding box.
# OpenSCAD's STL export produces ASCII STL ("solid ... vertex x y z ... endsolid");
# the first line is verified below to be ``solid`` before any parse.
# The awk parser reads only the "vertex" lines (not the "facet normal" lines,
# which carry unit vectors, not model coordinates).
#
# awk (mawk) is present in the base image (openscad/openscad:trixie);
# python3 and jq are NOT.
#
# The result is a single number: max_extent = max(x_max−x_min, y_max−y_min,
# z_max−z_min) in mm. A binary, truncated, or otherwise malformed STL cannot
# yield a trusted bounding box — a partial file can carry valid "vertex" lines
# that under-state the true extent, and the legacy 40.0/55.0 fallback would
# re-overflow the 800×800 frame (the exact bug issue #111 fixes). Such an
# STL therefore ABORTS the render: the STL artifact is still present on the
# volume, so the caller's classification table lands this in artifact_error
# ("STL present but a later step failed"), and the non-zero exit code carries
# the failure signal to the host — no silent stderr-only WARNING.
# A legitimately empty model (zero vertices) is detected the same way — it
# aborts as artifact_error rather than shipping an unframeable render; the
# caller's empty_model row still covers the normal "all eight artifacts
# present, STL degenerate" shape.
CAM_DIST_FACTOR=3.0
CAM_DIST_ISO_FACTOR=$(awk -v f="${CAM_DIST_FACTOR}" 'BEGIN { printf "%.10f", f * 2.0^0.5 }')

stl_first_line=$(awk 'NR == 1 { print; exit }' "${STL_FILE}")

bbox_status=0
bbox_detail=""
if [ "${stl_first_line}" != "" ] && ! printf '%s' "${stl_first_line}" | grep -q '^solid[[:space:]]'; then
    bbox_status=1
    bbox_detail="STL is not ASCII (first line is not 'solid')"
else
    max_extent=$(awk '
# Each "vertex" line carries one model coordinate. The "facet normal" lines
# carry unit vectors (not model coords) and must be excluded.
/^ *vertex[[:space:]]/ {
    x = $2 + 0
    y = $3 + 0
    z = $4 + 0
    if (x < minx) minx = x
    if (y < miny) miny = y
    if (z < minz) minz = z
    if (x > maxx) maxx = x
    if (y > maxy) maxy = y
    if (z > maxz) maxz = z
    found = 1
}
BEGIN {
    minx=1e30; miny=1e30; minz=1e30
    maxx=-1e30; maxy=-1e30; maxz=-1e30
    found=0
}
END {
    if (!found) { print "0"; exit }
    ex = maxx - minx
    ey = maxy - miny
    ez = maxz - minz
    m = ex; if (ey > m) m = ey; if (ez > m) m = ez
    if (m < 0) m = 0
    printf "%.10f", m
}' "${STL_FILE}")
    if [ -z "${max_extent}" ] || [ "${max_extent}" = "0" ] || [ "${max_extent}" = "0.0000000000" ]; then
        bbox_status=1
        bbox_detail="no vertex lines parsed (empty or truncated STL)"
    fi
fi

if [ "${bbox_status}" -ne 0 ]; then
    echo "[entrypoint] ERROR: bounding-box fit aborted — ${bbox_detail}" >&2
    echo "[entrypoint] ERROR: bounding-box fit aborted — ${bbox_detail}" >>"${LOG_FILE}"
    exit 1
fi

CAM_DIST_AA=$(awk -v m="${max_extent}" -v f="${CAM_DIST_FACTOR}" 'BEGIN { printf "%.10f", m * f }')
CAM_DIST_ISO=$(awk -v m="${max_extent}" -v f="${CAM_DIST_ISO_FACTOR}" 'BEGIN { printf "%.10f", m * f }')
echo "[entrypoint] max_extent=${max_extent}mm  dist_aa=${CAM_DIST_AA}  dist_iso=${CAM_DIST_ISO}" >&2

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

# Camera tuples: (tx, ty, tz, rx, ry, rz, dist).
# The first six elements are fixed (they pin which face each view sees);
# the dist element is substituted from the per-model bounding box computed
# above (issue #111). The values below use a placeholder "0" for dist —
# the loop at the bottom of the file substitutes CAM_DIST_AA / CAM_DIST_ISO
# before each openscad invocation.
declare -a VIEW_CAMERAS=(
    "0,0,0,0,0,0,0"     # front:  camera on -Z, no rotation
    "0,0,0,0,180,0,0"   # back:   180° about Y
    "0,0,0,0,90,0,0"    # left:   90° about Y
    "0,0,0,0,-90,0,0"   # right:  -90° about Y
    "0,0,0,90,0,0,0"    # top:    90° about X (looking down from +Z)
    "0,0,0,0,45,45,0"   # iso:    45° about Y + 45° about Z (isometric)
)

for i in 0 1 2 3 4 5; do
    step=$(( i + 3 ))
    name="${VIEW_NAMES[$i]}"
    # Substitute the per-model distance into the camera tuple.
    # All views use CAM_DIST_AA except the iso view (index 5), which uses
    # CAM_DIST_ISO (the 45° rotation projects a larger silhouette).
    if [ "${i}" -eq 5 ]; then
        dist="${CAM_DIST_ISO}"
    else
        dist="${CAM_DIST_AA}"
    fi
    # Strip the placeholder ",0" (last two chars) from the camera tuple
    # and append the computed per-model distance.
    cam="${VIEW_CAMERAS[$i]%,0},${dist}"
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
