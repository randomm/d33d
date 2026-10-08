// Issue #419 regression fixture: the REAL v100 SCAD from the 2026-10-08 QA
// repro (tmp/review-2026-10-08/REVIEW.md §1, screenshot A-recut-noop-passed.png).
// A plate import; the user asks "make the center hole 38 mm"; the fill-and-
// recut offer is accepted; the model emits this SCAD.
//
// The BUG: `scale(1) import("part.stl")` has NO semicolon, so the following
// `difference() { ... }` becomes a CHILD of `import()` — which ignores its
// children. The render re-exports the parent identical (664 faces,
// 56315.87 mm3, delta 0) and passes "Your design is ready".
//
// The secondary bug: `position=` is not a `cylinder()` parameter — OpenSCAD
// only WARNS (exit 0, valid STL), so the `cylinder` is placed at the origin
// instead of the hole centre. The unchanged-mesh check (this issue) catches
// the primary bug; the unknown-named-argument check catches the secondary.
//
// The fixture is the exact shape the loop must fail — the candidate's
// rendered mesh EQUALS the parent's (the parent's own model.stl is the
// candidate's rendered mesh, byte-for-byte, in the test).
scale(1) import("part.stl")
difference() {
  cylinder(d=3.99429, h=100, center=true);
  cylinder(d=38, h=100, center=true, position=[10, 10, 0]);
}
