# BOSL2 Pinning Policy

## Pinned tag and commit

The render worker image vendors BOSL2 from GitHub at build time. The tag is
supplied via the `--build-arg BOSL2_TAG` Dockerfile build-arg, which has **no
default** — a build without it fails loudly at the `ARG` / `RUN` line.

A git tag ref is mutable: upstream can retarget (move) a tag to point at a
different commit at any time, without changing the tag name. Pinning the tag
name alone is therefore not a supply-chain guarantee — a compromised or
retargeted tag ref would silently vendor different code into every render.
To close that gap, the build also requires `--build-arg BOSL2_COMMIT`, the
exact commit SHA the tag is expected to resolve to. After the shallow clone,
the Dockerfile runs `git rev-parse HEAD` inside the vendored checkout and
compares it against `BOSL2_COMMIT`; a mismatch fails the build loudly instead
of silently accepting the retargeted code.

Current pinned tag: **`v2.0.755`**
Current pinned commit: **`4e031aafe189efcf4eb0250c24d3216b6a429458`**
(verified via `git ls-remote https://github.com/BelfrySCAD/BOSL2 v2.0.755`
on 2026-09-11; `v2.0.755` is a lightweight tag pointing directly at this
commit)

Build command:

```bash
docker build --platform=linux/amd64 \
  --build-arg BOSL2_TAG=v2.0.755 \
  --build-arg BOSL2_COMMIT=4e031aafe189efcf4eb0250c24d3216b6a429458 \
  --build-arg D33D_BUILD_HASH="$(uv run python -c 'from d33d.render_worker import build_hash; print(build_hash())')" \
  -t d33d/render-worker:local .
```

This is the single canonical build command — the tag (`-t d33d/render-worker:local`)
must match `d33d.render_worker.RENDER_WORKER_IMAGE` and the
`D33D_BUILD_HASH` build-arg must stamp the `d33d/build-hash` image label
(the Dockerfile's `LABEL` reads the ARG) with the working tree's
`build_hash()`, so the server's pre-render staleness check (issue #236)
finds a matching label. A build without the hash, or with a stale label,
refuses to render until rebuilt with this command.

## Why pin to a tag (not `main` or a rolling ref)

1. **Reproducibility** — the same image build must produce the same render
   output every time. A rolling ref would silently change the BOSL2 API
   between builds, breaking the determinism contract (byte-identical STL and
   CSG across runs) and potentially breaking the six-view PNG output.

2. **Vulnerability / behaviour audit** — a specific tag is a fixed commit.
   We can diff between tags, audit the diff, and decide whether to upgrade.

3. **The LLM critique loop is trained on a fixed BOSL2 API surface** —
   the cheatsheet in `prompts/bosl2-cheatsheet.md` is generated against a
   specific tag. Upgrading the tag without updating the cheatsheet and
   re-running the evals would produce a mismatch between what the LLM is
   told and what BOSL2 actually exposes.

## Upgrade policy

Upgrading the pinned BOSL2 tag is a **breaking change** that requires:

1. A GitHub issue describing the reason for the upgrade (bug fix, security
   patch, new API needed by a ticket).
2. Verification that `prompts/bosl2-cheatsheet.md` is still accurate against
   the new tag — run the cheatsheet test
   (`tests/fast/test_bosl2_cheatsheet.py`) against the new tag before
   committing the tag bump.
3. A re-run of the slow-layer BOSL2 smoke test
   (`tests/slow/test_bosl2.py`) to confirm the image still builds and renders
   correctly with the new tag.
4. Resolution of the new tag's commit SHA (`git ls-remote <repo> <tag>`) and
   an update to both the `BOSL2_TAG` and `BOSL2_COMMIT` values in the build
   command(s) in this document — the two must always be updated together in
   the same commit, never `BOSL2_TAG` alone.
5. A re-run of the full eval suite (ticket #8) before merging.

## Base image pinning

The base image is pinned to `docker.io/openscad/openscad:trixie` — the
Debian trixie (13) variant. This tag is a rolling ref that updates as new
OpenSCAD releases are published to the trixie suite.

### Empirical CLI verification

Before using any `--camera` or `--projection` flag, the flags must be
verified against the pinned base image by running `openscad --help` inside
the image. The following flags were verified present in
`docker.io/openscad/openscad:trixie` (OpenSCAD version 2026.01.19) on
2026-09-11:

| Flag | Present | Notes |
|------|---------|-------|
| `--camera` | Yes | `=translate_x,y,z,rot_x,y,z,dist` or `=eye_x,y,z,center_x,y,z` |
| `--autocenter` | Yes | Adjusts camera to look at object's centre |
| `--projection` | Yes | `(o)rtho` or `(p)erspective` |
| `--imgsize` | Yes | `=width,height` |
| `--render` | Yes | Full geometry evaluation (not preview) |
| `--colorscheme` | Yes | `=*Cornfield \| Metallic \| … \| Tomorrow Night \| …` |
| `--backend` | Yes | `CGAL` (old/slow) or `Manifold` (new/fast, default) |
| `--D` | Yes | `-Dname=value` pre-define |

The `--colorscheme` flag is used in `entrypoint.sh` with the value
`"Tomorrow Night"` to produce a dark background that makes the model
geometry stand out clearly in the PNG renders.

### Camera tuple verification

The `--camera` flag accepts a 7-element tuple: `tx,ty,tz,rx,ry,rz,dist`.
The camera rotations are applied **about the origin** (after `--autocenter`
has shifted the origin to the object's bounding-box centre). Rotations
are applied in order: `rx` (about X), then `ry` (about Y), then `rz`
(about Z).

The following mappings were verified empirically on 2026-09-25 (issue #262)
using an asymmetric marker model inside the pinned image
(`openscad/openscad:trixie`, OpenSCAD 2026.01.19). The marker model:

- Base slab 40×40×10, z=0..10, centred at origin in XY
- Tall post at +X: x=24..32, z=0..45 (tallest feature)
- Short block at −X: x=−32..−24, z=0..22
- Block at +Y: y=22..34, z=0..16
- Notch on the −Y face of the base slab (removed volume)
- Cone on top at origin: z=10..26

Each view is verified by pixel analysis (the slow test
tests/slow/test_render_views.py::test_views_marker_model_feature_placement)
to show the face its name says under the operator convention (Z up,
front looks from −Y toward +Y, +X right in the frame):

| View | Camera tuple | Verification result |
|------|-------------|---------------------|
| front (`view_00`) | `0,0,0,90,0,0,40` | 90° about X; camera looks from −Y. The cone is at the bottom-center of the frame, the +X post on the right, the −X block on the left; the −Y notch is visible (facing the camera). |
| back (`view_01`) | `0,0,0,90,0,180,40` | 90° about X, 180° about Z; camera looks from +Y. The +Y block is in the centre; the −Y notch is hidden (facing away). |
| left (`view_02`) | `0,0,0,90,0,270,40` | 90° about X, 270° about Z; camera looks from −X. The cone is on one side of the frame, the −Y notch on the other. |
| right (`view_03`) | `0,0,0,90,0,90,40` | 90° about X, 90° about Z; camera looks from +X. The +X post is closest to the camera; the −Y notch is visible. |
| top (`view_04`) | `0,0,0,0,0,0,40` | No rotation; camera looks down from +Z. The base slab fills the frame; the cone tip is visible at the centre; the −Y notch is at the bottom of the frame, the +Y block at the top. |
| iso (`view_05`) | `0,0,0,55,0,25,55` | 55° about X + 25° about Z; camera in the front-right-top octant (−Y, +X, +Z). Verified pixel facts (per the marker test `tests/slow/test_render_views.py::test_views_marker_model_feature_placement`): the +X post is vertical (taller than wide) on the right of the frame, the cone is above the slab, the −Y notch face is visible (viewer on the −Y side), and +Z is up. Issue #269 re-verified this empirically: the original `(0,45,45)` rendered the model sideways (+Z pointing left, the 45 mm post as a horizontal bar); `(55,0,25)` — OpenSCAD's GUI default view angle — is the correct front-right-top octant. |

The original three-slab verification (2026-09-11) was **wrong**: it
labelled the rotation `(0,0,0)` as "front" (it is actually the top view),
`(90,0,0)` as "top" (it is actually the front view), `(0,180,0)` as "back"
(it is actually a side view), and `(0,90,0)` / `(0,-90,0)` as "left" /
"right" (they are actually other side views). The mislabelling was
invisible while the views were cropped (fixed in #223/#234); the
marker-model test makes the correct mapping the regression gate.

The iso rotation was additionally re-verified in #269: the original
`(0,45,45)` (45° about Y then 45° about Z) rendered the model sideways —
the 45 mm post appeared as a horizontal bar pointing left, with +Z
pointing to the upper-left of the frame. The new `(55,0,25)` (55° about
X, 0° about Y, 25° about Z) is OpenSCAD's GUI default view angle and
renders from the front-right-top octant (−Y, +X, +Z). Verified pixel facts
(per the marker test
`tests/slow/test_render_views.py::test_views_marker_model_feature_placement`):
the post is vertical on the right, the −Y notch face is visible, and +Z is
up — the rotation the per-view pixel assertions in that test verify.

These camera tuples are the single source of truth for the `VIEWS` constant
in `d33d/render_worker.py` and for the `VIEW_CAMERAS` array in
`entrypoint.sh`. They must be updated together in the same commit
if a future OpenSCAD build changes the camera rotation semantics.

### Entrypoint failure semantics (spec-critical invariant)

The entrypoint runs **eight** sequential `openscad` invocations. The failure
rule is asymmetric and load-bearing:

- **STL step (step 1) is the only aborting step.** A non-zero exit on the STL
  invocation exits the entrypoint immediately with that code — the remaining
  seven are not attempted.
- **The other seven steps (CSG + six PNGs) are non-aborting.** A non-zero exit
  on any of them is recorded (the maximum exit code is tracked) and the run
  **continues to completion**; the entrypoint then exits non-zero at the end so
  the *caller* can classify and harvest the partial artifact set.

This is required because the entrypoint runs under `set -e`. Without the
per-step `|| var=$?` guards, a single failed `openscad` call (e.g. a PNG that
fails while the STL succeeded) would be killed by `set -e` *before* the
remaining PNGs ran, silently dropping them and making the run unclassifiable.

The "STL present but a later PNG/CSG invocation failed" shape is the
spec's **class-5 `artifact_error`** — a real, reachable outcome that the
classification table (owned by the Python caller, not the entrypoint) depends
on being able to observe. The entrypoint's only job in that case is to:

1. not abort (run all eight invocations),
2. leave the partial artifacts in `/work` for the caller to harvest, and
3. exit non-zero (the max failing exit code) so the caller knows a step failed.

Do **not** "fix" write failures by relaxing `--read-only` or by removing the
`||` guards — a render that writes only to the rw `/work` volume must succeed
with `--read-only` on the rootfs, and the guards are what preserve the
class-5 contract.
