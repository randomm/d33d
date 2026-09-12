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
  -t d33d-render-worker:latest .
```

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
has shifted the origin to the object's bounding-box centre). The following
mappings were verified empirically on 2026-09-11 using a three-slab test
model (red slab at x=0, blue slab at z=0, green slab at z=20):

| View | Camera tuple | Verification result |
|------|-------------|---------------------|
| front (`view_00`) | `0,0,0,0,0,0,40` | Camera on -Z axis; red slab (x=0) visible as a full face on the left half of the image; blue slab (z=0) not visible (facing away); green slab (z=20) visible as a thin line at the top edge |
| back (`view_01`) | `0,0,0,0,180,0,40` | 180° about Y; red slab (x=0) visible as a full face on the right half; small red triangle at bottom-right corner confirms the view is from +Z |
| left (`view_02`) | `0,0,0,0,90,0,40` | 90° about Y; red slab (x=0) visible as a thin vertical strip on the left edge (edge-on view of the x=0 face); blue slab (z=0) visible as a thin horizontal strip at the bottom |
| right (`view_03`) | `0,0,0,0,-90,0,40` | -90° about Y; red slab (x=0) visible as a thin vertical strip on the right edge (edge-on from the opposite side); blue slab (z=0) visible as a thin horizontal strip at the bottom |
| top (`view_04`) | `0,0,0,90,0,0,40` | 90° about X (looking down from +Z); red slab (x=0) visible as a full square on the right side (the x=0 face is now facing up); green slab (z=20) not visible (it's the top face, facing toward the camera) |
| iso (`view_05`) | `0,0,0,0,45,45,55` | 45° about Y + 45° about Z; three coloured faces meet at a visible corner — the classic isometric corner view. The red face (x=0) is on the left, the blue face (z=0) is on the right, and the green face (z=20) is visible as a thin strip at the bottom-left edge |

These camera tuples are the single source of truth for the `VIEWS` constant
in `d33d/__init__.py` (ticket: core workstream) and for the `VIEW_CAMERAS`
array in `entrypoint.sh`. They must be updated together in the same commit
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
