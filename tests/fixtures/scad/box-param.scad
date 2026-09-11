// Parametric version of box-magic.scad using named parameters (positive target)
W = 20;
H = 25;
D = 30;

cube([W, H, D]);
translate([0, 0, D])
cylinder(h=5, d=W);
