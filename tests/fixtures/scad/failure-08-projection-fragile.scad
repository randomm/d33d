// Failure class 8: projection()/offset() CGAL fragility
// projection() and offset() are CGAL-dependent and can be fragile with
// certain geometry. This fixture applies offset() to a shape that may
// trigger a CGAL error.
offset(delta=1, r=1)
square([10, 10], center=true);
