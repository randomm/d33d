"""Unit tests for d33d.db — the shared SQLite schema (issue #3, task-d scope).

Tables owned by this workstream:
  - projects             (project metadata; the git repo path is on the file system)
  - transcripts          (one row per message; per-message model logging)
  - provider_credentials (Fernet ciphertext; the list endpoint returns names only)
  - request_logs         (observability; prompt_hash is the A10 diff key)

Invariants pinned here:
  - the connection is row-factory-aware (sqlite3.Row -> dict access);
  - WAL mode so the FastAPI writer and any read-only replica do not lock;
  - FK enforcement is ON and the schema is idempotent (open, close, reopen
    on the same file and the tables still exist);
  - prompt_hash round-trips into request_logs verbatim and is indexable;
  - provider_credentials rows round-trip ciphertext as BLOB and names as TEXT.

All tests are fast (no Docker, no network) and use ``:memory:`` or a tmp_path
SQLite file. The `slow` marker is NOT used — CI without Docker must pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from d33d import db

TABLES = {
    "projects",
    "transcripts",
    "provider_credentials",
    "request_logs",
}


@pytest.fixture
def conn() -> db.Connection:
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def file_db(tmp_path: Path) -> db.Connection:
    c = db.connect(tmp_path / "d33d.sqlite3")
    yield c
    c.close()


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"] for r in rows}


def test_connect_creates_all_four_tables(conn: db.Connection) -> None:
    assert TABLES <= _tables(conn)


def test_connect_is_idempotent_on_a_persisted_file(file_db: db.Connection) -> None:
    # Reopen the same file: tables must still exist (idempotent schema).
    p = file_db.path
    file_db.close()
    c2 = db.connect(p)
    try:
        assert TABLES <= _tables(c2)
    finally:
        c2.close()


def test_foreign_keys_are_enforced(conn: db.Connection) -> None:
    on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert on == 1


def test_wal_mode_is_active(file_db: db.Connection) -> None:
    mode = file_db.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_projects_round_trip(conn: db.Connection) -> None:
    pid = conn.create_project(name="Hinge bracket", tags=["hinge"], notes="40x20x3")
    row = conn.get_project(pid)
    assert row is not None
    assert row["name"] == "Hinge bracket"
    assert row["git_repo_path"] is not None
    # Tags round-trip as JSON list.
    assert row["tags"] == ["hinge"]
    assert row["notes"] == "40x20x3"
    assert row["source_photo_path"] is None


def test_projects_update(conn: db.Connection) -> None:
    pid = conn.create_project(name="A")
    conn.update_project(pid, name="A2", tags=["x", "y"], notes="n")
    row = conn.get_project(pid)
    assert row["name"] == "A2"
    assert row["tags"] == ["x", "y"]


def test_projects_delete(conn: db.Connection) -> None:
    pid = conn.create_project(name="A")
    conn.delete_project(pid)
    assert conn.get_project(pid) is None


def test_list_projects_orders_by_created(conn: db.Connection) -> None:
    p1 = conn.create_project(name="first")
    p2 = conn.create_project(name="second")
    listed = conn.list_projects()
    assert [r["id"] for r in listed] == [p1, p2]


def test_transcripts_append_and_read_in_order(conn: db.Connection) -> None:
    pid = conn.create_project(name="P")
    m1 = conn.append_message(
        project_id=pid, role="user", content="hello", model_alias="design-primary"
    )
    m2 = conn.append_message(
        project_id=pid,
        role="assistant",
        content="hi",
        model_alias="design-primary",
        model_id="RedHatAI/Qwen3.8-27B-INT4",
        provider="trailopeners",
    )
    msgs = conn.get_transcript(project_id=pid)
    assert [m["id"] for m in msgs] == [m1, m2]
    # Per-message model logging (acceptance #5 groundwork).
    assert msgs[0]["model_alias"] == "design-primary"
    assert msgs[1]["model_id"] == "RedHatAI/Qwen3.8-27B-INT4"
    assert msgs[1]["provider"] == "trailopeners"


def test_transcript_content_is_byte_identical_round_trip(
    conn: db.Connection,
) -> None:
    """The stored transcript is the canonical schema; the DB must not
    rewrite it. This pins acceptance #4's stored-side of the contract —
    sanitisation happens on the OUTGOING request, never on the stored row."""
    pid = conn.create_project(name="P")
    raw = "Design a   dovetail   hinge bracket"
    conn.append_message(project_id=pid, role="user", content=raw)
    row = conn.get_transcript(project_id=pid)[0]
    assert row["content"] == raw


def test_transcripts_require_existing_project(conn: db.Connection) -> None:
    import sqlite3 as _sqlite3

    with pytest.raises(_sqlite3.IntegrityError):
        conn.append_message(project_id=99999, role="user", content="x")


def test_provider_credentials_round_trip_and_names_only(
    conn: db.Connection,
) -> None:
    conn.put_credential(
        provider_id="trailopeners",
        model_alias="design-primary",
        key_ciphertext=b"fernet-ciphertext-bytes",
    )
    # Names-only view (what the list endpoint returns): no ciphertext.
    names = conn.list_credentials()
    assert len(names) == 1
    assert names[0]["provider_id"] == "trailopeners"
    assert names[0]["model_alias"] == "design-primary"
    # The names-only view must NOT expose the ciphertext column.
    assert "key_ciphertext" not in names[0]
    # No public ciphertext accessor exists on the DB layer (HIGH #2):
    # the ciphertext is owned by d33d.security.credentials.CredentialStore,
    # not by Connection — an HTTP handler reaching for this class cannot
    # leak key material via repr/logging/pickle of a Connection row.
    assert not hasattr(conn, "get_credential")


def test_request_logs_round_trip_with_prompt_hash(conn: db.Connection) -> None:
    pid = conn.create_project(name="P")
    rid = conn.log_request(
        project_id=pid,
        model_alias="design-primary",
        model_id="RedHatAI/Qwen3.8-27B-INT4",
        provider="trailopeners",
        role="design",
        status="ok",
        prompt_tokens=412,
        completion_tokens=180,
        latency_ms=1234,
        prompt_hash="a" * 64,
    )
    row = conn.get_request_log(rid)
    assert row["model_alias"] == "design-primary"
    assert row["model_id"] == "RedHatAI/Qwen3.8-27B-INT4"
    assert row["provider"] == "trailopeners"
    assert row["role"] == "design"
    assert row["status"] == "ok"
    assert row["prompt_tokens"] == 412
    assert row["completion_tokens"] == 180
    assert row["latency_ms"] == 1234
    assert row["prompt_hash"] == "a" * 64


def test_request_logs_prompt_hash_is_indexable_and_queryable(
    conn: db.Connection,
) -> None:
    """Acceptance #5: two rows on the same prompt hash (different models)
    can be diffed purely from the logs."""
    pid = conn.create_project(name="P")
    h = "ab" * 32
    conn.log_request(
        project_id=pid,
        model_alias="design-primary",
        model_id="RedHatAI/Qwen3.8-27B-INT4",
        provider="trailopeners",
        role="design",
        status="ok",
        prompt_tokens=412,
        completion_tokens=180,
        latency_ms=1234,
        prompt_hash=h,
    )
    conn.log_request(
        project_id=pid,
        model_alias="design-backup",
        model_id="Qwen/Qwen2.5-32B-Instruct",
        provider="paid-azure",
        role="design",
        status="ok",
        prompt_tokens=398,
        completion_tokens=175,
        latency_ms=2001,
        prompt_hash=h,
    )
    matches = conn.requests_by_prompt_hash(h)
    assert len(matches) == 2
    aliases = {m["model_alias"] for m in matches}
    assert aliases == {"design-primary", "design-backup"}


def test_request_logs_require_existing_project(conn: db.Connection) -> None:
    import sqlite3 as _sqlite3

    with pytest.raises(_sqlite3.IntegrityError):
        conn.log_request(
            project_id=99999,
            model_alias="a",
            model_id="b",
            provider="c",
            role="design",
            status="ok",
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=1,
            prompt_hash="d" * 64,
        )


def test_row_factory_is_dict_like(conn: db.Connection) -> None:
    row = conn.execute("SELECT 1 AS one").fetchone()
    assert row["one"] == 1
    # sqlite3.Row supports dict-style access via keys() and dict() conversion.
    assert list(row.keys()) == ["one"]
    assert dict(row) == {"one": 1}
