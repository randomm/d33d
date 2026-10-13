"""Issue #433 lens round-1 fixes: one carry persist/merge helper with loud
failures, message-only finalize bodies, the single role vocabulary, and
validated carried feature rows."""

from __future__ import annotations

import json
import logging
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from d33d.statement_extraction import StatedFeature

_WRITE_SQL = "UPDATE projects SET carried_stated_features"


class _Raw:
    def __init__(self, fail_write: bool = False, fail_read: bool = False) -> None:
        self.fail_write = fail_write
        self.executed: list[str] = []

    def execute(self, sql: str, *args: Any) -> None:
        self.executed.append(sql)
        if self.fail_write and sql.startswith(_WRITE_SQL):
            raise sqlite3.OperationalError("disk I/O error")


class _Conn:
    """A stand-in for the db wrapper: ``get_project`` / ``raw`` / ``commit``."""

    def __init__(
        self,
        row: dict[str, Any] | None = None,
        *,
        fail_read: bool = False,
        fail_write: bool = False,
    ) -> None:
        self.row = row
        self.fail_read = fail_read
        self.raw = _Raw(fail_write=fail_write)

    def get_project(self, _project_id: int) -> dict[str, Any] | None:
        if self.fail_read:
            raise sqlite3.OperationalError("database is locked")
        return self.row

    def commit(self) -> None:
        return None


def _carried_row(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"carried_stated_features": json.dumps(items)}


# ---------------------------------------------------------------------------
# 1. One persist helper, one merge helper, loud failures.
# ---------------------------------------------------------------------------


def test_failed_write_logs_warning_naming_project_and_turn_keeps_merged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed write is a WARNING (with traceback) naming the project and
    the operation; the turn still runs and gets the merged features."""
    from d33d.stated_carry import carry_stated_features_for_turn

    conn = _Conn(_carried_row([{"noun": "groove", "role": None, "value": 2.0}]), fail_write=True)
    with caplog.at_level(logging.WARNING, logger="d33d.stated_carry"):
        merged = carry_stated_features_for_turn(conn, 42, "a 5 mm hole")

    assert {(f.noun, f.value) for f in merged} == {("groove", 2.0), ("hole", 5.0)}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, caplog.text
    assert "42" in warnings[0].getMessage()
    assert "carried_stated_features" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None


def test_failed_read_keeps_features_stated_in_the_current_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed READ must not drop the features the current message states;
    the stored set is not overwritten with a guess either (no write)."""
    from d33d.stated_carry import carry_stated_features_for_turn

    conn = _Conn(fail_read=True)
    with caplog.at_level(logging.WARNING, logger="d33d.stated_carry"):
        merged = carry_stated_features_for_turn(conn, 7, "a 5 mm hole")

    assert [(f.noun, f.value) for f in merged] == [("hole", 5.0)]
    assert not any(_WRITE_SQL in s for s in conn.raw.executed)
    assert any(r.levelno == logging.WARNING and "7" in r.getMessage() for r in caplog.records)


def test_successful_turn_persists_merged_set() -> None:
    from d33d.stated_carry import carry_stated_features_for_turn

    conn = _Conn(_carried_row([{"noun": "groove", "role": None, "value": 2.0}]))
    merged = carry_stated_features_for_turn(conn, 3, "the hole is 6 mm")

    assert {(f.noun, f.value) for f in merged} == {("groove", 2.0), ("hole", 6.0)}
    assert any(s.startswith(_WRITE_SQL) for s in conn.raw.executed)


def test_finalize_message_only_body_carries_feature(app_with_versions: Any) -> None:
    """(LOW bug) A finalize body with ``message`` and no ``request`` attribute
    must still feed its message into the feature carry (the pre-fix
    ``hasattr`` guard read ``""`` and dropped the statement)."""
    import d33d.versions_routes as routes_mod
    from tests.versioning.helpers import create_project, run_async

    class _Req:
        app = app_with_versions

    body = SimpleNamespace(
        params=None,
        name=None,
        message="add a 5 mm hole",
        photo=None,
        stated_dims=None,
        chat_history=(),
    )
    assert not hasattr(body, "request")

    async def _call(client: Any) -> list[Any]:
        pid = (await create_project(client))["id"]
        kwargs = routes_mod._finalize_loop_kwargs(_Req(), pid, body)
        return list(kwargs["stated_features"])

    feats = run_async(app_with_versions, _call)
    assert [(f.noun, f.value) for f in feats] == [("hole", 5.0)]


def test_finalize_write_failure_logs_warning_and_turn_runs(
    app_with_versions: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The finalize seam: a failed carry WRITE is a WARNING naming the project
    and the loop still receives the merged features."""
    import d33d.versions_routes as routes_mod
    from tests.versioning.helpers import create_project, run_async

    class _FailingRaw:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def execute(self, sql: str, *args: Any) -> Any:
            if sql.startswith(_WRITE_SQL):
                raise sqlite3.OperationalError("disk I/O error")
            return self._inner.execute(sql, *args)

    class _FailingWrites:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.raw = _FailingRaw(inner.raw)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    class _Req:
        app = app_with_versions

    body = routes_mod.FinalizeBody(params=None, name=None, message="add a 5 mm hole")

    async def _call(client: Any) -> tuple[int, list[Any]]:
        pid = (await create_project(client))["id"]
        monkeypatch.setattr(
            app_with_versions.state, "conn", _FailingWrites(app_with_versions.state.conn)
        )
        with caplog.at_level(logging.WARNING, logger="d33d.stated_carry"):
            kwargs = routes_mod._finalize_loop_kwargs(_Req(), pid, body)
        return pid, list(kwargs["stated_features"])

    pid, feats = run_async(app_with_versions, _call)
    assert [(f.noun, f.value) for f in feats] == [("hole", 5.0)]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(pid) in r.getMessage() and r.exc_info for r in warnings), caplog.text


# ---------------------------------------------------------------------------
# 3. One role vocabulary.
# ---------------------------------------------------------------------------


def test_every_extractable_role_word_has_a_matcher_entry() -> None:
    from d33d import design_state
    from d33d.axis_lexicon import FEATURE_ROLE_TOKENS
    from d33d.statement_extraction import _FEATURE_ROLE_WORDS, _SIZE_SLOT_ROLES

    assert set(_FEATURE_ROLE_WORDS) == set(FEATURE_ROLE_TOKENS)
    for role in _FEATURE_ROLE_WORDS:
        assert role in design_state._FEATURE_ROLE_TOKENS, role
    assert _SIZE_SLOT_ROLES == {r for r, toks in FEATURE_ROLE_TOKENS.items() if toks == design_state._SIZE_TOKENS}


# ---------------------------------------------------------------------------
# 5. Validate carried values.
# ---------------------------------------------------------------------------


def test_tampered_carried_row_is_filtered() -> None:
    from d33d.stated_carry import carried_stated_features

    row = _carried_row(
        [
            {"noun": "hole", "role": "diameter", "value": 5.0},
            {"noun": "DROP TABLE", "role": None, "value": 3.0},
            {"noun": "hole", "role": "bogus", "value": 4.0},
            {"noun": "groove", "role": None, "value": 2.0},
        ]
    )
    out = carried_stated_features(row)
    assert out == [
        StatedFeature(noun="hole", role="diameter", value=5.0),
        StatedFeature(noun="groove", role=None, value=2.0),
    ]


def test_carried_features_capped_at_32() -> None:
    from d33d.stated_carry import carried_stated_features

    row = _carried_row([{"noun": "hole", "role": None, "value": float(i + 1)} for i in range(40)])
    assert len(carried_stated_features(row)) == 32
