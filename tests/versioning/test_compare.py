"""Compare tests (issue #8, test-surface: test_compare.py).

Compare is the prioritized surface (the single most-missed feature across
every competitor surveyed). Covers:
- given two version IDs the compare endpoint returns both param sets plus a
  computed diff table (added/removed/changed keys);
- the diff is symmetric (compare(A,B) == compare(B,A) with sides swapped);
- comparing a version to itself yields an empty diff;
- the compare response does NOT include 3D geometry — only param diffs and
  the shared-rotation contract (identical units/axis convention for the two
  client viewports).
"""

from __future__ import annotations

from tests.versioning.helpers import create_project, run_async


async def _make_versions(client, pid):
    """Paired-variant fixture: two versions that share 4 of 6 parameters
    and differ in 2 (one numeric, one categorical/boolean) — plus a third
    version that removes a key (to exercise 'removed')."""
    v_a_params = {"W": 20, "H": 25, "D": 30, "wall": True, "slot": 5, "brim": "round"}
    v_b_params = {"W": 24, "H": 25, "D": 30, "wall": False, "slot": 5, "brim": "round"}
    v_c_params = {"W": 24, "H": 25, "D": 30, "wall": False, "slot": 5}  # brim removed
    r = await client.post(
        f"/api/projects/{pid}/versions",
        json={"params": v_a_params, "name": "variant A"},
    )
    va = r.json()
    r = await client.post(
        f"/api/projects/{pid}/versions",
        json={"params": v_b_params, "name": "variant B"},
    )
    vb = r.json()
    r = await client.post(
        f"/api/projects/{pid}/versions",
        json={"params": v_c_params, "name": "variant C"},
    )
    vc = r.json()
    return va, vb, vc


def test_compare_returns_param_sets_and_diff_table(app_with_versions):
    """The compare endpoint returns BOTH full param sets plus the computed
    diff table (added/removed/changed)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        va, vb, _ = await _make_versions(client, pid)
        r = await client.get(
            f"/api/projects/{pid}/versions/compare",
            params={"a": va["id"], "b": vb["id"]},
        )
        return va, vb, r

    va, vb, r = run_async(app_with_versions, _call)
    assert r.status_code == 200, r.text
    body = r.json()

    # Both full param sets present.
    assert body["a"]["params"] == va["params"]
    assert body["b"]["params"] == vb["params"]

    # The diff table: one numeric change (W) and one categorical/bool
    # change (wall). No added/removed between A and B (same key set).
    diff = body["diff"]
    assert diff["added"] == []
    assert diff["removed"] == []
    assert sorted(diff["changed"]) == ["W", "wall"]
    assert diff["count"] == 2


def test_compare_diff_is_symmetric(app_with_versions):
    """compare(A,B) == compare(B,A) with sides swapped (the diff itself is
    mirrored: added <-> removed, changed unchanged)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        va, _, vc = await _make_versions(client, pid)
        r1 = await client.get(
            f"/api/projects/{pid}/versions/compare",
            params={"a": va["id"], "b": vc["id"]},
        )
        r2 = await client.get(
            f"/api/projects/{pid}/versions/compare",
            params={"a": vc["id"], "b": va["id"]},
        )
        return r1.json(), r2.json()

    ab, ba = run_async(app_with_versions, _call)
    # A→C: 'brim' is removed. C→A: 'brim' is added.
    assert ab["diff"]["removed"] == ["brim"]
    assert ab["diff"]["added"] == []
    assert ba["diff"]["added"] == ["brim"]
    assert ba["diff"]["removed"] == []
    # Changed keys are the same set, count identical.
    assert ab["diff"]["changed"] == ba["diff"]["changed"]
    assert ab["diff"]["count"] == ba["diff"]["count"]


def test_compare_with_itself_yields_empty_diff(app_with_versions):
    """Comparing a version to itself: zero added, zero removed, zero
    changed."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        va, _, _ = await _make_versions(client, pid)
        r = await client.get(
            f"/api/projects/{pid}/versions/compare",
            params={"a": va["id"], "b": va["id"]},
        )
        return r

    r = run_async(app_with_versions, _call)
    assert r.status_code == 200
    diff = r.json()["diff"]
    assert diff == {"added": [], "removed": [], "changed": [], "count": 0}


def test_compare_response_has_no_geometry(app_with_versions):
    """The compare response carries ONLY param diffs + thumbnail references
    + the shared-rotation contract — never the 3D geometry (no STL/3MF
    payload, no vertex/face data)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        va, vb, _ = await _make_versions(client, pid)
        r = await client.get(
            f"/api/projects/{pid}/versions/compare",
            params={"a": va["id"], "b": vb["id"]},
        )
        return r

    r = run_async(app_with_versions, _call)
    body = r.json()
    # No geometry keys anywhere in the response.
    text = r.text.lower()
    for forbidden in ("stl", "3mf", "vertices", "faces", "geometry", "mesh", "binary"):
        assert forbidden not in text, f"{forbidden!r} leaked into compare response"
    # The shared-rotation contract IS present (identical units/axis
    # convention — what lets the two viewports share a rotation state).
    sr = body["shared_rotation"]
    assert sr["units"] == "mm"
    assert sr["axis_convention"] == "z-up"
    assert sr["identical_convention"] is True


def test_compare_unknown_version_is_404(app_with_versions):
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        return await client.get(
            f"/api/projects/{pid}/versions/compare", params={"a": 1, "b": 999}
        )

    r = run_async(app_with_versions, _call)
    assert r.status_code == 404
