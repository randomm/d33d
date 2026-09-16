"""Unit tests for the .scad top-level module call-site parser (issue #7).

Parses .scad SOURCE (never CSG/STL output — that path is a confirmed dead
end, see the module docstring on ``d33d.module_registry``) to enumerate
the top-level module CALL-SITES that a region pick resolves to. Module
*definitions* (``module foo() {...}``) are never call-sites; only bare
invocations (``foo();``) at the top level of the file count.

No Docker required — pure text parsing.
"""

from __future__ import annotations

import time

from d33d.module_registry import CallSite, isolate_call_site, parse_call_sites


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


def test_ordinal_suffix_never_collides_with_a_real_module_name() -> None:
    """Regression: adversarial-review round 2. A design with both a
    ``leg()`` module AND a distinct ``leg_2()`` module previously
    produced two call-sites with the SAME registry_name ("leg_2") — the
    second ``leg()`` call's auto-generated ordinal suffix collided with
    the literal ``leg_2`` module name, silently overwriting one mesh with
    the other in the assembled registry. Ordinal suffixes must skip past
    any literal module identifier declared or called elsewhere in the
    source."""
    source = (
        "module leg() { cylinder(h=40, r=3); }\n"
        "module leg_2() { cylinder(h=20, r=2); }\n"
        "leg();\n"
        "leg();\n"
        "leg_2();\n"
    )
    sites = parse_call_sites(source)
    registry_names = [s.registry_name for s in sites]
    assert len(registry_names) == len(set(registry_names)), (
        f"registry names must be pairwise distinct, got {registry_names}"
    )
    # The literal leg_2() call must keep its own natural name.
    leg_2_call = sites[-1]
    assert leg_2_call.name == "leg_2"
    assert leg_2_call.registry_name == "leg_2"
    # The second leg() call must NOT have been assigned "leg_2" (that name
    # belongs to the real leg_2() module's own call-site above).
    second_leg_call = sites[1]
    assert second_leg_call.name == "leg"
    assert second_leg_call.registry_name != "leg_2"


def test_call_site_records_column_offset_for_positional_isolation() -> None:
    source = "module leg(){cube([1,1,1]);}\nleg(); leg();\n"
    sites = parse_call_sites(source)
    assert len(sites) == 2
    assert sites[0].col == 0
    assert sites[1].col == source.split("\n")[1].index("leg();", 1)


def test_empty_source_returns_empty_list() -> None:
    assert parse_call_sites("") == []


def test_ordinal_disambiguation_is_not_quadratic_in_adversarial_collision_source() -> None:
    """Regression: adversarial-review round 2. A source built from N
    calls to the same module name PLUS N sacrificial ``module m_k(){}``
    definitions that force every auto-generated ordinal to collide made
    the ordinal-disambiguation ``while`` loop rescan from scratch per
    call-site, an O(N^2) blowup: ~4000 colliding call-sites took over a
    second of pure CPU in this parser alone, comfortably reachable within
    the HTTP route's 1MB body cap (MAX_CALL_SITES rejects the count only
    AFTER parse_call_sites has already returned). Disambiguation must be
    amortized O(1) per call-site regardless of how many literal names
    collide, so parsing itself stays cheap for any body-size-bounded
    input."""
    n = 4000
    defs = "".join(f"module m_{k}(){{cube([1,1,1]);}}\n" for k in range(2, n + 2))
    source = "module m(){cube([1,1,1]);}\n" + defs + "m();\n" * n

    start = time.perf_counter()
    sites = parse_call_sites(source)
    elapsed = time.perf_counter() - start

    assert len(sites) == n
    registry_names = [s.registry_name for s in sites]
    assert len(registry_names) == len(set(registry_names))
    # Generous bound: linear-time parsing of this ~150KB source should
    # take a small fraction of a second; the quadratic version took over
    # 1.2s for this exact input on the same hardware.
    assert elapsed < 0.5, f"parse_call_sites took {elapsed:.3f}s — quadratic regression"


def test_call_site_is_frozen_dataclass_with_expected_fields() -> None:
    site = CallSite(name="base", registry_name="base", line=1)
    assert site.name == "base"
    assert site.registry_name == "base"
    assert site.line == 1
    assert site.col == 0


def test_two_calls_to_same_module_on_one_line_isolate_distinct_occurrences() -> None:
    """Regression: adversarial-review round 2. Two calls to the SAME
    module on one source line (``leg(); leg();``) previously produced
    byte-IDENTICAL isolated sources for both call-sites, because
    ``isolate_call_site`` masked only the first unmodified occurrence of
    the name on the line regardless of which site was being isolated —
    the second call's own geometry was never actually rendered; a
    duplicate of the first was silently substituted under its
    registry_name. Positional (``site.col``) targeting must isolate each
    occurrence independently."""
    source = "module leg(){cube([1,1,1]);}\nleg(); leg();\n"
    sites = parse_call_sites(source)
    assert len(sites) == 2

    first_isolated = isolate_call_site(source, sites[0])
    second_isolated = isolate_call_site(source, sites[1])

    assert first_isolated != second_isolated
    assert first_isolated.split("\n")[1] == "!leg(); leg();"
    assert second_isolated.split("\n")[1] == "leg(); !leg();"
