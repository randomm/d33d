// BOSL2 model using a confirmed module from the cheatsheet (rounding on a cuboid)
include <BOSL2/bosl2.scad>

W = 20;
H = 25;
D = 30;

rounding(r=2)
cube([W, H, D], center=true);
