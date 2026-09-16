// Issue #100 — the reported multi-part SCAD (live repro: "Create a 20mm
// cube with a 10mm sphere sitting next to it"). The failures.jsonl archive
// line for this request carried an empty output_scad (the archive
// pre-dated the loop's SCAD capture), so this fixture IS the reported SCAD:
// the parametric W/D/H block the design prompt mandates, the cube, and the
// sphere placed beside it — the placement translate on its own line (the
// shape the gate's placement exemption must cover) and the sphere's d=10
// inlined (the 2-digit literal the gate flags when 10 is not stated).
W = 20;
D = 20;
H = 20;
cube([W, D, H]);
translate([20, 0, 0])
sphere(d = 10);
