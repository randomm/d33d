# BOSL2 Cheatsheet

BOSL2 is far less represented in LLM training data than raw OpenSCAD, so
models hallucinate its API. This file is the single reference the design
loop (ticket #4) injects into the LLM's context. **Every module signature
below is verified against the vendored BOSL2 source at tag `v2.0.755`**
(`include <BOSL2/std.scad>`). Do not invent signatures.

## Rounding

- `cuboid(size, [chamfer=0], [rounding=0], [edges=EDGES_ALL])` — 3D box with optional per-edge chamfer/rounding; `rounding` is the radius on `edges` (a list of edge indices or "ALL"). The workhorse for rounded mechanical parts.
- `cuboid([size], [p1, p2], ...)` — alternative constructor: two corner points instead of a size vector.
- `round3d([r], [or], [ir], [size=1000])` — rounds arbitrary 3D objects (children); `r` rounds all concave+convex corners, `or` convex only, `ir` concave only. Very slow — last resort for non-primitive shapes.
- `offset3d(r, [size=1000])` — 3D convex offset of children (inset with negative `r`); the building block behind `round3d`.
- `rounded_prism(bottom, top, [joint_bot=0], [joint_top=0], [joint_sides=0], [k_bot=0.5], [k_top=0.5], [k_sides=0.5])` — prisms between two polygons with rounded joins on bottom, top and side edges.
- `smooth_path(path, [tangents], [size=0], [relsize=0.1], [method="edges"], [splinesteps=10], [uniform=0], [closed=false])` — 2D path smoothing (function form); round 2D paths with tangent-length controls before extruding.
- `path_join(paths, [joint=0], [k=0.5], [relocate=true], [closed=false])` — join 2D paths with a rounded joint.

## Masks (selective edge rounding / chamfering)

- `chamfer_edge_mask(l, [chamfer=1], [excess=0.1], [h])` — 2D mask that chamfers a length-`l` edge; apply to a profile to cut a flat bevel on specific edges.
- `rounding_edge_mask(l, r, ang=90, r1, r2, ...)` — 2D mask that rounds a length-`l` edge to radius `r`; `r1`/`r2` for asymmetric radii at each end.
- `rounding_angled_edge_mask(h, r, r1, r2, [ang=90])` — mask for rounding an angled (angled-end) edge.
- `rounding_angled_corner_mask(r, ang=90, d)` — mask for rounding an angled corner.
- `teardrop_edge_mask(l, r, [angle=45], [excess=0.1])` — 2D mask that rounds one end of an edge into a teardrop.
- `mask2d_roundover(r, [inset=0], [mask_angle=90], ...)` — 2D mask producing a roundover profile for extrusion (function form for profile data, module form for shape).
- `mask2d_chamfer(edge, [angle=90], [inset=0], ...)` — 2D chamfer profile mask.
- `mask2d_teardrop(r, [angle=45], ...)` — 2D teardrop profile mask.
- `mask2d_cove(r, [inset=0], [mask_angle=90], [bulge=1], ...)` — 2D concave cove profile mask.
- `mask2d_smooth([mask_angle=90], [cut=false], [joint=0], [height], [k=0.5], ...)` — 2D smooth (S-curve) profile mask.
- `face_profile(faces=[], r, [d=2], [excess=0.01])` — 3D face-rounding profile for extrude-based rounding.
- `edge_profile(edges=EDGES_ALL, except=[], [excess=0.01])` — 3D edge-rounding profile for extrude-based rounding.
- `corner_profile(corners=CORNERS_ALL, except=[], r, [d=2], [axis="Z"])` — 3D corner-rounding profile for extrude-based rounding.
- `bent_cutout_mask(r, [thickness=0], path, [radius])` — bent cutout profile mask for grooves.
- `convex_offset_extrude(profile, height, ...)` — extrude a 2D offset profile into a 3D convex offset solid.

## Attach / Align anchoring

- `attachable([anchor=CENTER], [spin=0], [orient=UP], [cp], ...)` — wrap a child so it exposes a named attachment point for later `attach`.
- `attach([parent=1], [child=1], [overlap=0], [align=DEF], [spin=0], [norot=false], [inset=0], [shiftout=0], [inside=false])` — place the child's anchor on the parent's anchor; `parent`/`child` accept indices or tag names. The primary anchoring primitive.
- `attach_part(name, ind=0)` — refer to a specific part inside a multi-part parent.
- `position(at, from)` — translate the current object so `from` (a point or tag) lands on `at`.
- `orient([anchor=CENTER], [spin=0])` — rotate the current object to match an anchor's orientation.
- `align([anchor=CENTER], [align=CENTER], [inside=false], [inset=0], [shiftout=0], [overlap=0])` — align the current object to an anchor; `align` is a vector of "center"/"min"/"max" per axis.
- `move(v=[0,0,0], p)` — translate by a 3-vector `v` (also `xmove`, `ymove`, `zmove` for single-axis moves).
- `left(x)` / `right(x)` / `fwd(y)` / `back(y)` / `up(z)` / `down(z)` — single-axis relative moves.
- `rot(a=0, v, cp)` — rotate by `a` degrees about axis `v` around point `cp`.
- `xrot(a, p, cp)` / `yrot(a, p, cp)` / `zrot(a, p, cp)` — single-axis rotations.
- `diff(remove="remove", keep="keep")` — tagged difference (BOSL2's replacement for `difference()` in attach-able designs).
- `intersect(intersect="intersect", keep="keep")` — tagged intersection.
- `hide(tags)` / `show_only(tags)` / `show_all()` — tag-based visibility control for debugging.

## Distributors

- `move_copies(a=[[0,0,0]])` — place a child at each position in vector list `a` (the simplest distributor).
- `xcopies(spacing, n, l, sp)` — place `n` copies along X from `l` to `sp` at `spacing` intervals.
- `ycopies(...)` / `zcopies(...)` — same, along Y and Z.
- `line_copies(spacing, n, l, p1, p2)` — place `n` copies along the line from `p1` to `p2`.
- `line_of(spacing, n, l, p1, p2)` — shorthand alias of `line_copies`.
- `grid_copies(spacing, n, size, stagger=false, inside=undef, axes="xy")` — place copies on a 2D grid (also `grid2d`).
- `rot_copies(rots=[], v, cp=[0,0,0], n, sa=0, offset=0, delta=[0,0,0], subrot=true)` — place `n` copies at equal angular steps about axis `v` around center `cp`.
- `xrot_copies(rots, cp, n, sa=0, r, d, subrot=true)` / `yrot_copies(...)` / `zrot_copies(...)` — single-axis rotational copies.
- `arc_copies(n=6, r, rx, ry, d, dx, dy, sa=0, ea=360, rot=true)` — place `n` copies on a circular arc from `sa` to `ea` degrees.
- `arc_of(n, r, rx, ry, d, dx, dy, sa, ea, rot)` — shorthand alias of `arc_copies`.
- `sphere_copies(n=100, r=undef, d=undef, cone_ang=90, scale=[1,1,1], perp=true)` — place `n` copies on a sphere (Fibonacci distribution).
- `ovoid_spread(...)` — place copies on an ovoid (egg-shaped) surface.
- `path_spread(path, n, spacing, sp=undef, rotate_children=true, closed)` — place copies along an arbitrary 2D/3D path (also `path_copies`).
- `path_join(paths, joint=0, k=0.5, relocate=true, closed=false)` — join paths before distributing along them.

## Fasteners (print-fit via `oversize` / `hole_oversize`)

- `screw(spec, head, drive, thread, drive_size, shaft_oversize, head_oversize, origin, thread_len, tolerance, head_size, ...)` — parametric screw; `spec` is a string like `"M6"` or a struct; `shaft_oversize`/`head_oversize` add the print-fit allowance to the shaft/head.
- `screw_hole(spec, head, thread, oversize, hole_oversize, head_oversize, ...)` — the matching hole for a screw; `oversize` is the print-fit clearance (mm to add to the hole diameter).
- `shoulder_screw(s, d, length, head, thread_len, tolerance, head_size, drive, drive_size, thread, ...)` — shoulder screw with a defined thread length.
- `screw_head(screw_info, details=false, counterbore=0, flat_height, teardrop=false, slop=0)` — a screw head alone (for custom assembly).
- `nut(spec, shape, thickness, nutwidth, thread, tolerance, hole_oversize, ...)` — parametric hex/flat/square nut; `hole_oversize` is the print-fit clearance on the threaded hole.
- `nut_trap_side(trap_width, spec, shape, thickness, nutwidth, anchor=BOT, ...)` / `nut_trap_inline(length, spec, ...)` — nut traps to keep a nut captive in a printed part.
- `metric_bolt(headtype="socket", size=3, l=12, shank=0, pitch=undef, coarse=true, ...)` — metric bolt; `size` is the nominal diameter in mm (M3 = 3), `shank` is the unthreaded portion.
- `metric_nut(size, ...)` — metric hex nut; `size` is the nominal diameter in mm.
- `generic_screw(...)` — generic (non-metric) screw; use `screw()` with a struct spec for full control.
- `thread_specification(screw_spec, tolerance=undef, internal=false)` — function returning the thread geometry for a spec, with the tolerance applied.

## Threading (with `tolerance` / `oversize` for print fit)

- `threaded_rod(d, l, pitch, left_handed=false, bevel, starts=1, internal=false, ...)` — external thread of diameter `d`, length `l`, pitch `pitch`; `internal=true` for an internal (hole) thread.
- `threaded_nut(d, l, pitch, left_handed=false, ...)` — matching internal thread (nut body).
- `trapezoidal_threaded_rod(...)` / `trapezoidal_threaded_nut(...)` — trapezoidal-profile threads.
- `acme_threaded_rod(...)` / `acme_threaded_nut(...)` — Acme-profile threads.
- `garden_hose_threaded_rod(...)` — garden-hose (NPT-like) thread.
- `npt_threaded_rod(...)` — NPT thread.
- `bspp_threaded_rod(...)` — BSP (BSPP) thread.
- `buttress_threaded_rod(...)` / `buttress_threaded_nut(...)` — buttress thread.
- `square_threaded_rod(...)` / `square_threaded_nut(...)` — square-profile thread.
- `ball_screw_rod(...)` — ball-screw lead screw.
- `generic_threaded_rod(...)` / `generic_threaded_nut(...)` — fully parameterized generic thread (profile, tolerance, lead-in shape, etc.).

## Partitions

- `partition_mask(l, h, cutsize, cutpath="jigsaw", gap=0, cutpath_centered=true, ...)` — 2D mask that splits a shape along a cut line; returns the two halves as separate objects.
- `partition_cut_mask(l=100, h=100, cutsize=10, cutpath="jigsaw", gap=0, ...)` — the matching cut profile for the partitioned halves.
- `partition(size=100, spread=10, cutsize=10, cutpath="jigsaw", gap=0, ...)` — convenience wrapper that produces a full partitioned shape in one call.
- `half_of(v=UP, cp, s=100, planar=false, cut_path, cut_angle=0, offset=0, ...)` — keep only the half of a shape on one side of a plane through `cp` with normal `v`.
- `left_half(...)` / `right_half(...)` / `front_half(...)` / `back_half(...)` / `bottom_half(...)` / `top_half(...)` — pre-oriented versions of `half_of` for each axis side.

## Core 3D shapes (shapes3d)

- `cuboid(size, [chamfer], [rounding], [edges], [anchor], [spin], [orient])` — rounded/chamfered 3D box (see Rounding above; the rounding-capable box).
- `cube(size=1, center, anchor, spin=0, orient=UP)` — plain 3D cube with anchor/spin/orient.
- `wedge(size=[1,1,1], center, anchor, spin=0, orient=UP)` — right-triangle wedge.
- `prismoid(...)` / `regular_prism(n, ...)` — prisms and regular polyhedra.
- `cylinder(h, r1, r2, center, r, d, d1, d2, anchor, spin=0, orient=UP)` — cylinder/cone with anchor/spin/orient (BOSL2's version of the builtin).
- `cyl(...)` / `xcyl(...)` / `ycyl(...)` / `zcyl(...)` — axis-aligned cylinder shorthands.
- `sphere(r, d, anchor=CENTER, spin=0, orient=UP)` — sphere with anchor/spin/orient.
- `spheroid(r, style="aligned", d, circum=false, dual=false, anchor, spin, orient)` — ellipsoid.
- `torus(...)` / `teardrop(...)` / `onion(...)` — parametric torus, teardrop, and onion shapes.
- `text3d(text, h, size, font, spacing=1.0, direction="ltr", ...)` — extruded 3D text.
- `interior_fillet(l=1.0, r, ang=90, overlap=0.01, d, length, h, height, anchor, spin, orient)` — interior fillet solid.
- `fillet(l, r, ang, r1, r2, excess=0.01, ...)` — fillet solid (exterior or interior).
- `plot3d(f, x, y, zclip, zspan, base=1, anchor="origin", orient=UP, spin=0, atype="hull", cp="box", convexity=4, style="default")` — plot a 3D function as a solid.
