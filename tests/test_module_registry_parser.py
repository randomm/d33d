"""Unit tests for the .scad top-level module call-site parser (issue #7).

Parses .scad SOURCE (never CSG/STL output — that path is a confirmed dead
end, see the module docstring on ``d33d.module_registry``) to enumerate
the top-level module CALL-SITES that a lasso selection resolves to. Module
*definitions* (``module foo() {...}``) are never call-sites; only bare
invocations (``foo();``) at the top level of the file count.

No Docker required — pure text parsing.
"""

from __future__ import annotations

from d33d.module_registry import CallSite, parse_call_sites


def test_simple_flat_call_sites() -> None:
    source = """
module base() { cube([20,20,20]); }
module post(h=30) { cylinder(h=h, r=5); }
module cap() { sphere(r=8); }

base();
post(h=25);
cap();
"""
    sites = parse_call_sites(source)
    assert [s.name for s in sites] == ["base", "post", "cap"]


def test_definitions_are_never_call_sites() -> None:
    """A module definition header must never be mistaken for a call."""
    source = """
module base() { cube([1,1,1]); }
"""
    sites = parse_call_sites(source)
    assert sites == []


def test_parameterised_call_site_with_named_and_positional_args() -> None:
    source = """
module post(h=30, r=5) { cylinder(h=h, r=r); }
post(25, r=8);
"""
    sites = parse_call_sites(source)
    assert [s.name for s in sites] == ["post"]


def test_nested_call_inside_a_module_body_is_not_top_level() -> None:
    """A call written INSIDE another module's body/braces is nested, not a
    top-level call-site — only calls at brace-depth 0 in the file count."""
    source = """
module inner() { cube([5,5,5]); }
module outer() {
    inner();
    translate([10,0,0]) inner();
}
outer();
"""
    sites = parse_call_sites(source)
    assert [s.name for s in sites] == ["outer"]


def test_call_site_wrapped_in_a_transform_still_counts_at_top_level() -> None:
    """``translate(...) foo();`` at brace-depth 0 is still a top-level
    call-site for ``foo`` — the modifier/transform prefix does not nest
    the call inside braces."""
    source = """
module leg() { cylinder(h=40, r=3); }
translate([10, 0, 0]) leg();
translate([-10, 0, 0]) rotate([0,0,45]) leg();
"""
    sites = parse_call_sites(source)
    assert [s.name for s in sites] == ["leg", "leg"]


def test_bang_modifier_call_site_still_parses_by_name() -> None:
    """A call already carrying a ``!``/``%``/``#``/``*`` modifier in the
    source is still enumerated by its bare module name — the isolation
    modifier the orchestrator injects is a SEPARATE render-time concern,
    not something the parser needs to special-case away."""
    source = """
module base() { cube([1,1,1]); }
!base();
"""
    sites = parse_call_sites(source)
    assert [s.name for s in sites] == ["base"]


def test_commented_call_sites_are_ignored_line_comment() -> None:
    source = """
module base() { cube([1,1,1]); }
// base();
base();
"""
    sites = parse_call_sites(source)
    assert [s.name for s in sites] == ["base"]


def test_commented_call_sites_are_ignored_block_comment() -> None:
    source = """
module base() { cube([1,1,1]); }
/* base();
   post(); */
base();
"""
    sites = parse_call_sites(source)
    assert [s.name for s in sites] == ["base"]


def test_no_modules_edge_case_returns_empty_list() -> None:
    source = """
cube([20,20,20]);
translate([5,5,5]) sphere(r=3);
"""
    sites = parse_call_sites(source)
    assert sites == []


def test_builtin_primitives_are_never_treated_as_module_call_sites() -> None:
    """cube/sphere/cylinder/etc. are built-in primitives, not user-defined
    modules — a registry entry for them would be meaningless (nothing to
    isolate that isn't already isolate-able by other means) and they must
    never appear in the parsed call-site list."""
    source = """
module widget() { cube([1,1,1]); }
cube([2,2,2]);
sphere(r=1);
cylinder(h=1, r=1);
widget();
"""
    sites = parse_call_sites(source)
    assert [s.name for s in sites] == ["widget"]


def test_duplicate_call_sites_get_distinct_ordinal_suffix() -> None:
    """The same module called twice at the top level must resolve to two
    DISTINCT registry names (a lasso pick must be unambiguous) — pinned as
    ``name`` (first) then ``name_2``, ``name_3``, ... for subsequent
    calls, never silently collapsed or overwritten."""
    source = """
module leg() { cylinder(h=40, r=3); }
translate([10, 0, 0]) leg();
translate([-10, 0, 0]) leg();
translate([0, 10, 0]) leg();
"""
    sites = parse_call_sites(source)
    assert [s.registry_name for s in sites] == ["leg", "leg_2", "leg_3"]
    assert [s.name for s in sites] == ["leg", "leg", "leg"]


def test_call_site_records_source_line_number() -> None:
    source = "module base() { cube([1,1,1]); }\nbase();\n"
    sites = parse_call_sites(source)
    assert len(sites) == 1
    assert sites[0].line == 2


def test_empty_source_returns_empty_list() -> None:
    assert parse_call_sites("") == []


def test_call_site_is_frozen_dataclass_with_expected_fields() -> None:
    site = CallSite(name="base", registry_name="base", line=1)
    assert site.name == "base"
    assert site.registry_name == "base"
    assert site.line == 1
