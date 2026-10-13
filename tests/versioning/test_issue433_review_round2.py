"""Issue #433 lens round-2 fixes: an unreadable stored carry is never
overwritten, the finalize carry persists only once the loop starts, the
write side is capped (newest kept), read values are finite and positive,
and unchanged sets are not re-written."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from d33d.statement_extraction import StatedFeature


class _Conn:
    """Stand-in for the versions repository: ``get_project`` and the
    ``set_carried_stated_features`` accessor; a write goes through to ``row``
    so a reload reads what was persisted."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.writes: list[Any] = []

    def get_project(self, _project_id: int) -> dict[str, Any]:
        return dict(self.row)

    def set_carried_stated_features(self, _project_id: int, value: Any) -> None:
        self.writes.append(value)
        self.row["carried_stated_features"] = value


# ---------------------------------------------------------------------------
# 1. Unreadable stored carry is never overwritten.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("corrupt", ["{not json", '{"noun": "hole"}', "42"])
def test_corrupt_stored_carry_is_left_unchanged_and_warned(
    corrupt: str, caplog: pytest.LogCaptureFixture
) -> None:
    from d33d.stated_carry import carry_stated_features_for_turn

    conn = _Conn({"carried_stated_features": corrupt})
    with caplog.at_level(logging.WARNING, logger="d33d.stated_carry"):
        merged = carry_stated_features_for_turn(conn, 11, "a 5 mm hole")

    assert [(f.noun, f.value) for f in merged] == [("hole", 5.0)]
    assert conn.writes == []
    assert conn.row["carried_stated_features"] == corrupt
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, caplog.text
    assert "11" in warnings[0].getMessage()
    assert "carried_stated_features" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# 2. Finalize persists only once the loop starts (a gate that raises 502
#    before the loop leaves the carried column untouched).
# ---------------------------------------------------------------------------


def test_finalize_rejected_before_loop_leaves_carried_column_unchanged(
    app_with_versions: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from d33d import design_source
    from tests.versioning.helpers import create_project, run_async
    from tests.versioning.test_design_loop_finalize import _StubResult

    before = json.dumps([{"noun": "groove", "role": None, "value": 2.0}])

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("design source unavailable")

    monkeypatch.setattr(design_source, "current_version_source", _boom)

    def _loop(app: Any, **kwargs: Any) -> Any:
        return _StubResult("pass", {"hole_diameter": 5.0})

    async def _call(client: Any) -> tuple[int, dict[str, Any]]:
        pid = (await create_project(client))["id"]
        app_with_versions.state.conn.raw.execute(
            "UPDATE projects SET carried_stated_features = ? WHERE id = ?",
            (before, pid),
        )
        app_with_versions.state.conn.commit()
        app_with_versions.state.run_design_loop = _loop
        r = await client.post(
            f"/api/projects/{pid}/finalize",
            json={"name": "the bracket", "message": "add a 5 mm hole"},
        )
        return r.status_code, app_with_versions.state.conn.get_project(pid)

    status, row = run_async(app_with_versions, _call)
    assert status == 502
    assert row["carried_stated_features"] == before


# ---------------------------------------------------------------------------
# 3. Write-side cap keeps the newest features.
# ---------------------------------------------------------------------------


def test_write_side_cap_keeps_newest_feature_across_reload() -> None:
    from d33d.stated_carry import (
        MAX_CARRIED_FEATURES,
        carried_stated_features,
        carry_stated_features_for_turn,
    )

    stored = [{"noun": "hole", "role": None, "value": float(i + 1)} for i in range(40)]
    conn = _Conn({"carried_stated_features": json.dumps(stored)})
    carry_stated_features_for_turn(conn, 4, "a 3 mm groove")

    assert len(conn.writes) == 1
    persisted = json.loads(conn.writes[0])
    assert len(persisted) == MAX_CARRIED_FEATURES
    assert persisted[-1] == {"noun": "groove", "role": None, "value": 3.0}
    reloaded = carried_stated_features(conn.get_project(4))
    assert reloaded[-1] == StatedFeature(noun="groove", role=None, value=3.0)


# ---------------------------------------------------------------------------
# 4. Finite, positive values on read.
# ---------------------------------------------------------------------------


def test_non_finite_and_non_positive_carried_values_are_dropped() -> None:
    from d33d.stated_carry import carried_stated_features

    raw = (
        '[{"noun": "hole", "role": null, "value": NaN},'
        ' {"noun": "hole", "role": null, "value": Infinity},'
        ' {"noun": "hole", "role": null, "value": 0},'
        ' {"noun": "hole", "role": null, "value": -4.0},'
        ' {"noun": "groove", "role": null, "value": 2.5}]'
    )
    out = carried_stated_features({"carried_stated_features": raw})
    assert out == [StatedFeature(noun="groove", role=None, value=2.5)]


# ---------------------------------------------------------------------------
# 5. Unchanged merged set is not written.
# ---------------------------------------------------------------------------


def test_unchanged_set_is_not_written() -> None:
    from d33d.stated_carry import carry_stated_features_for_turn

    stored = [{"noun": "hole", "role": None, "value": 5.0}]
    conn = _Conn({"carried_stated_features": json.dumps(stored)})
    merged = carry_stated_features_for_turn(conn, 9, "a 5 mm hole")

    assert [(f.noun, f.value) for f in merged] == [("hole", 5.0)]
    assert conn.writes == []
