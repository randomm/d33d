// Failure class 4: difference() operand inversion
// The intent is to subtract the small cylinder from the large cylinder
// (creating a tube), but the operands are inverted, subtracting the large
// from the small (producing nothing or an inverted shape).
difference() {
  cylinder(h=5, d=4);
  cylinder(h=10, d=20);
}
