// Failure class 10: hallucinated BOSL2 module or parameter name
// The module "bosl2_rounding" does not exist in BOSL2. The correct
// module is "rounding" (without the "bosl2_" prefix).
include <BOSL2/bosl2.scad>

bosl2_rounding(r=2)
cube([10, 10, 10]);
