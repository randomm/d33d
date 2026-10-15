"""Issue #448: harden the stated-feature carry at its two weak points.

1. The chat call site (``d33d.chat_loop.run_design_loop``): the comment
   claimed the carry "never fails the 202", but the call was unguarded — the
   claim held only because ``d33d.stated_carry`` guards its own read, extract
   and write paths. A non-dict projects row raises ``AttributeError`` out of
   ``_parse_stored`` (the ``row.get`` sits outside the try in ``_read_stored``)
   and that WOULD fail the 202. The call is now wrapped in a local
   ``try/except Exception`` that logs a WARNING naming the project and falls
   back to the honest empty set.

2. The corrupt-row WARNING (``d33d.stated_carry._read_stored``): a corrupt
   ``carried_stated_features`` row stays corrupt while the user states no
   feature, so the same WARNING fired on every turn. It now fires once per
   project per server process, and is forgotten when the REPLACED path heals
   the row so a second corruption is reported again.

All fast: stub loop, no LLM, no Docker, no real model call.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from d33d.statement_extraction import StatedFeature
from tests.versioning.helpers import create_project, run_async


@pytest.fixture(autouse=True)
def _fresh_corrupt_carry_dedupe() -> Any:
    """The once-per-project dedupe is process-global, so a test that reuses a
    project id (or a fixture DB that recycles id 1) would see the second
    WARNING suppressed and the caplog assertion become order-dependent.
    Each test starts from an empty set and leaves one behind."""
    from d33d import stated_carry

    stated_carry._CORRUPT_CARRY_LOGGED.clear()
    yield
    stated_carry._CORRUPT_CARRY_LOGGED.clear()


class _Conn:
    """Stand-in for the versions repository: ``get_project`` +
    ``set_carried_stated_features``, with the stored row kept in ``row`` so a
    reload reads what was persisted."""

    def __init__(self, row: Any = None) -> None:
        self.row = row
        self.writes: list[Any] = []

    def get_project(self, _project_id: int) -> Any:
        return self.row

    def set_carried_stated_features(self, _project_id: int, value: Any) -> None:
        self.writes.append(value)
        self.row = {"carried_stated_features": value}


def _warnings(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and needle in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# 1. The chat call site: a raising carry never fails the 202.
# ---------------------------------------------------------------------------


def test_chat_carry_raising_logs_warning_and_returns_202(
    app_with_versions: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An exception raised from inside ``carry_stated_features_for_turn`` does
    not fail the chat request: 202, a WARNING naming the project (with no
    message text), and the turn continues with no stated features."""
    import d33d.chat_loop as chat_loop_mod
    from tests.versioning.test_design_loop_finalize import _StubResult

    captured: dict[str, Any] = {}

    async def _loop(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _StubResult("pass", {"W": 10})

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("carry exploded")

    async def _call(client: Any) -> tuple[int, str]:
        pid = (await create_project(client))["id"]
        app_with_versions.state.run_design_loop = _loop
        monkeypatch.setattr(chat_loop_mod, "carry_stated_features_for_turn", _boom)
        with caplog.at_level(logging.WARNING, logger="d33d.chat_loop"):
            r = await client.post(
                f"/api/projects/{pid}/chat", json={"message": "add a 5 mm hole"}
            )
            source = app_with_versions.state.event_sources.get(pid)
            async for event, _data in source:
                if event in ("done", "error"):
                    break
        return r.status_code, str(pid)

    status, pid_text = run_async(app_with_versions, _call)
    assert status == 202
    # The turn still reached the loop — with the honest empty set.
    assert captured["stated_features"] == []
    warnings = _warnings(caplog, "carried_stated_features")
    assert len(warnings) == 1, caplog.text
    assert pid_text in warnings[0]


def test_chat_carry_failing_on_a_non_dict_row_returns_202(
    app_with_versions: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The concrete unguarded path: a non-dict projects row makes
    ``_parse_stored`` raise ``AttributeError`` on ``row.get`` — outside every
    try inside ``stated_carry``. Before the call-site guard this failed the
    202. A non-dict row also trips the ``carried_stated_dims`` write guard
    (``d33d.chat_loop``'s own best-effort block), so the stub loop's kwargs
    are what prove the turn continued."""
    import d33d.chat_loop as chat_loop_mod
    from tests.versioning.test_design_loop_finalize import _StubResult

    captured: dict[str, Any] = {}

    async def _loop(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _StubResult("pass", {"W": 10})

    async def _call(client: Any) -> tuple[int, list[str]]:
        pid = (await create_project(client))["id"]
        app_with_versions.state.run_design_loop = _loop
        monkeypatch.setattr(
            app_with_versions.state.versions, "get_project", lambda _pid: "not-a-row"
        )
        with caplog.at_level(logging.WARNING, logger="d33d.chat_loop"):
            r = await client.post(
                f"/api/projects/{pid}/chat", json={"message": "hello there"}
            )
            source = app_with_versions.state.event_sources.get(pid)
            async for event, _data in source:
                if event in ("done", "error"):
                    break
        return r.status_code, _warnings(caplog, "carried_stated_features")

    status, warnings = run_async(app_with_versions, _call)
    assert status == 202
    assert len(warnings) == 1, caplog.text
    # The turn continued, with the honest empty set (no carried features).
    assert captured["stated_features"] == []
    assert chat_loop_mod  # the module whose logger the WARNING went to


# ---------------------------------------------------------------------------
# 2. The corrupt-row WARNING: once per project per process.
# ---------------------------------------------------------------------------


def test_corrupt_row_warns_once_across_no_feature_turns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Several turns that state no feature against a corrupt row log the
    UNREADABLE WARNING once, not once per turn."""
    from d33d.stated_carry import carry_stated_features_for_turn

    conn = _Conn({"carried_stated_features": "{not json"})
    with caplog.at_level(logging.WARNING, logger="d33d.stated_carry"):
        for _ in range(4):
            merged = carry_stated_features_for_turn(conn, 21, "make it taller")

    assert merged == []
    assert conn.writes == []
    assert len(_warnings(caplog, "UNREADABLE")) == 1, caplog.text


def test_corrupt_row_warning_not_suppressed_across_projects(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The dedupe is keyed by project id: two corrupt projects are two
    warnings, not one."""
    from d33d.stated_carry import carry_stated_features_for_turn

    with caplog.at_level(logging.WARNING, logger="d33d.stated_carry"):
        carry_stated_features_for_turn(_Conn({"carried_stated_features": "{not json"}), 31, "hi")
        carry_stated_features_for_turn(_Conn({"carried_stated_features": "{not json"}), 32, "hi")

    warnings = _warnings(caplog, "UNREADABLE")
    assert len(warnings) == 2, caplog.text
    assert any("31" in m for m in warnings)
    assert any("32" in m for m in warnings)


def test_corrupt_row_warns_again_after_the_row_is_replaced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The REPLACED path (a turn that states a feature) heals the row, so the
    project is forgotten by the dedupe — a row that corrupts a second time is
    reported again rather than silenced for the life of the process."""
    from d33d.stated_carry import carry_stated_features_for_turn

    conn = _Conn({"carried_stated_features": "{not json"})
    with caplog.at_level(logging.WARNING, logger="d33d.stated_carry"):
        for _ in range(2):
            carry_stated_features_for_turn(conn, 41, "make it taller")
        # A stated feature replaces the corrupt row (and clears the dedupe).
        carry_stated_features_for_turn(conn, 41, "a 5 mm hole")
        # Corrupt again — reported a second time.
        conn.row = {"carried_stated_features": "{not json"}
        for _ in range(2):
            carry_stated_features_for_turn(conn, 41, "make it taller")

    warnings = _warnings(caplog, "UNREADABLE")
    assert len(warnings) == 2, caplog.text
    assert len(_warnings(caplog, "REPLACED")) == 1, caplog.text


def test_read_failed_warning_still_fires_every_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The dedupe is scoped to the corrupt-row WARNING only: a READ that
    raises is a live fault (transient lock, dead connection) and stays loud on
    every turn."""
    import sqlite3

    from d33d.stated_carry import carry_stated_features_for_turn

    class _FailingRead:
        def __init__(self) -> None:
            self.writes: list[Any] = []

        def get_project(self, _project_id: int) -> Any:
            raise sqlite3.OperationalError("database is locked")

        def set_carried_stated_features(self, _project_id: int, value: Any) -> None:
            self.writes.append(value)

    conn = _FailingRead()
    with caplog.at_level(logging.WARNING, logger="d33d.stated_carry"):
        for _ in range(3):
            merged = carry_stated_features_for_turn(conn, 51, "a 5 mm hole")

    assert merged == [StatedFeature(noun="hole", role=None, value=5.0)]
    assert conn.writes == []
    assert len(_warnings(caplog, "READ failed")) == 3, caplog.text
