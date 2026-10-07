"""Corrupt-JSON degrade tests for ``VersionService._row_to_version`` (issue #389)
and ``part_envelope_with_bbox``'s v1 bbox decode (issue #389).

A corrupt (unparseable) JSON column in a stored version row must degrade to
the honest absent value (``{}`` for ``params``, ``None`` for the others)
with a ``logger.warning`` naming the column — never a 500. The v1 bbox
decode in ``part_envelope_with_bbox`` must likewise degrade (not raise) and
now emits the same warning.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _conn_with_corrupt_columns(tmp_path: Path, corrupt_column: str, corrupt_value: str):
    """An in-memory connection with one version row whose given JSON column
    holds a corrupt (unparseable) blob; every other JSON column holds valid
    JSON. Returns the connection, project id, and version id."""
    import d33d.db as db_mod
    from d33d.versions import migrate as _migrate

    conn = db_mod.Connection(":memory:")
    _migrate(conn)
    pid = conn.create_project(name="p", git_repo_path=str(tmp_path / "repo"))

    bbox_json = json.dumps({"x": 10.0, "y": 20.0, "z": 30.0})
    stated_json = json.dumps({"W": 60.0})
    meta_json = json.dumps({"w": {"label": "width"}})
    confirmed_json = json.dumps({"w": 60.0})
    fork_json = json.dumps([pid, 1])
    params_json = "{}"

    values = {
        "params": params_json,
        "forked_from": fork_json,
        "bbox": bbox_json,
        "stated_dims": stated_json,
        "param_meta": meta_json,
        "confirmed_params": confirmed_json,
    }
    values[corrupt_column] = corrupt_value

    conn.execute(
        "INSERT INTO versions (project_id, params, name, forked_from, bbox,"
        " stated_dims, param_meta, confirmed_params)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pid,
            values["params"],
            "v1",
            values["forked_from"],
            values["bbox"],
            values["stated_dims"],
            values["param_meta"],
            values["confirmed_params"],
        ),
    )
    conn.commit()
    vid = conn.raw.execute(
        "SELECT id FROM versions WHERE project_id = ? ORDER BY id ASC LIMIT 1",
        (pid,),
    ).fetchone()[0]
    return conn, pid, vid


@pytest.mark.parametrize("column", ["params", "bbox", "stated_dims", "param_meta", "confirmed_params", "forked_from"])
def test_row_to_version_corrupt_column_degrades_no_raise(
    tmp_path: Path, column: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupt JSON blob in any of the 6 JSON columns degrades to the
    honest absent value (``{}`` for params, ``None`` for the others) with a
    warning naming the column — never an exception (issue #389)."""
    from d33d.versions import VersionService

    conn, pid, vid = _conn_with_corrupt_columns(tmp_path, column, "{not valid json")
    svc = VersionService(conn)
    with caplog.at_level("WARNING"):
        v = svc.get_version(pid, vid)
    assert v is not None

    expected = {
        "params": {},
        "bbox": None,
        "stated_dims": None,
        "param_meta": None,
        "confirmed_params": None,
        "forked_from": None,
    }
    # The corrupt column degrades to its honest default.
    assert v[column] == expected[column], (column, v[column])
    # The other 5 columns decode normally (not affected by the corrupt one).
    for other, valid in {
        "params": json.loads("{}"),
        "bbox": {"x": 10.0, "y": 20.0, "z": 30.0},
        "stated_dims": {"W": 60.0},
        "param_meta": {"w": {"label": "width"}},
        "confirmed_params": {"w": 60.0},
        "forked_from": (pid, 1),
    }.items():
        if other != column:
            assert v[other] == valid, (other, v[other])

    # Exactly one warning, naming the corrupt column; the blob is never logged.
    warns = [r for r in caplog.records if column in r.getMessage()]
    assert len(warns) == 1, [r.getMessage() for r in caplog.records]
    assert "{not valid json" not in warns[0].getMessage()


def test_row_to_version_null_columns_stay_null(tmp_path: Path) -> None:
    """A row whose JSON columns are NULL (the honest absent value) decodes
    to the same defaults as a corrupt row — NULL and corrupt both degrade;
    the NULL path must NOT warn (it is not a decode failure)."""
    import d33d.db as db_mod
    from d33d.versions import VersionService
    from d33d.versions import migrate as _migrate

    conn = db_mod.Connection(":memory:")
    _migrate(conn)
    pid = conn.create_project(name="p", git_repo_path=str(tmp_path / "repo"))
    conn.execute(
        "INSERT INTO versions (project_id, params, name) VALUES (?, ?, ?)",
        (pid, "{}", "v1"),
    )
    conn.commit()
    svc = VersionService(conn)
    v = svc.list_versions(pid)[0]
    assert v["params"] == {}
    assert v["bbox"] is None
    assert v["stated_dims"] is None
    assert v["param_meta"] is None
    assert v["confirmed_params"] is None
    assert v["forked_from"] is None


def test_row_to_version_list_versions_corrupt_column(tmp_path: Path) -> None:
    """The route-facing ``list_versions`` (not just ``get_version``) also
    degrades a corrupt column without raising (issue #389)."""
    from d33d.versions import VersionService

    conn, pid, _ = _conn_with_corrupt_columns(tmp_path, "bbox", "{corrupt")
    svc = VersionService(conn)
    versions = svc.list_versions(pid)
    assert len(versions) == 1
    assert versions[0]["bbox"] is None
    assert versions[0]["params"] == {}


def test_part_envelope_with_bbox_corrupt_v1_bbox_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A v1 row whose bbox column holds a corrupt blob degrades
    ``bbox_mm`` to ``None`` (the gate abstains) with a warning naming the
    column — never an exception (issue #389)."""
    import d33d.db as db_mod
    from d33d.part_http import part_envelope_with_bbox
    from d33d.versions import migrate as _migrate

    conn = db_mod.Connection(":memory:")
    _migrate(conn)
    pid = conn.create_project(name="p", git_repo_path=str(tmp_path / "repo"))
    conn.execute(
        "INSERT INTO versions (project_id, name, params, bbox) VALUES (?, ?, ?, ?)",
        (pid, "v1", "{}", "{not-valid-json"),
    )
    conn.execute(
        "UPDATE projects SET part_filename=?, part_format=?, part_unit=?, "
        "part_unit_status=?, part_scale=? WHERE id=?",
        ("part.stl", "stl", "mm", "settled", 1.0, pid),
    )
    conn.commit()
    row = conn.get_project(pid)

    with caplog.at_level("WARNING"):
        env = part_envelope_with_bbox(row, conn)
    assert env is not None
    assert env["bbox_mm"] is None

    warns = [r for r in caplog.records if "bbox" in r.getMessage()]
    assert len(warns) == 1, [r.getMessage() for r in caplog.records]
    assert "{not-valid-json" not in warns[0].getMessage()


def test_part_envelope_with_bbox_valid_v1_bbox_still_resolves(
    tmp_path: Path,
) -> None:
    """A v1 row with a VALID bbox still resolves ``bbox_mm`` (no regression
    from the degrade change — issue #389 must not break the happy path)."""
    import d33d.db as db_mod
    from d33d.part_http import part_envelope_with_bbox
    from d33d.versions import migrate as _migrate

    conn = db_mod.Connection(":memory:")
    _migrate(conn)
    pid = conn.create_project(name="p", git_repo_path=str(tmp_path / "repo"))
    conn.execute(
        "INSERT INTO versions (project_id, name, params, bbox) VALUES (?, ?, ?, ?)",
        (pid, "v1", "{}", json.dumps({"x": 20.0, "y": 20.0, "z": 20.0})),
    )
    conn.execute(
        "UPDATE projects SET part_filename=?, part_format=?, part_unit=?, "
        "part_unit_status=?, part_scale=? WHERE id=?",
        ("part.stl", "stl", "mm", "settled", 1.0, pid),
    )
    conn.commit()
    row = conn.get_project(pid)
    env = part_envelope_with_bbox(row, conn)
    assert env is not None
    assert env["bbox_mm"] == (20.0, 20.0, 20.0)
