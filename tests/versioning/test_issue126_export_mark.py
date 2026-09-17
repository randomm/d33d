"""Issue #126 — the persistent "exported" mark on a version.

The mark is the valuable half of the export's designed ending: a maker three
days later asks which version they actually printed, and the answer has to
come from the SERVER, not from client state. The decisive contract is the
reload: a NEW client session over the same app/DB (the "come back to the
browser" simulation) reads ``exported_at`` off the version timeline, because
the event was recorded server-side, not held in the React tree.

Semantics pinned here (decided in this ticket, recorded per the ticket's own
"decide and say which" instruction):

- the mark belongs to the SPECIFIC version exported (not necessarily the
  latest — the export route takes a version id);
- re-exporting the same version makes the LATEST export win: ``exported_at``
  updates to the later time (there is no counter, no first-export timestamp);
- a failed or cancelled download never reaches the route, so nothing here
  can mark a version whose 3MF the user never received — the SPA is the only
  caller and it calls after a successful download (the client-side half is
  pinned by the web suite).
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from tests.versioning.helpers import create_project, create_version, run_async


def _column_names(conn, table: str) -> set[str]:
    rows = conn.raw.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


# ---------------------------------------------------------------------------
# schema / migration
# ---------------------------------------------------------------------------


def test_migrate_adds_exported_at_to_pre_existing_versions_table(tmp_path):
    """A database created before this ticket (versions table without the
    column) migrates idempotently and gains ``exported_at`` nullable —
    no backfill, because an unexported version is NULL, never a
    fabricated timestamp (issue #91's precedent: an absent fact abstains).
    """
    from d33d import db as db_mod
    from d33d.versions import migrate

    scratch = db_mod.Connection(tmp_path / "pre126.sqlite3")
    migrate(scratch)  # fresh DDL: has the column
    # Recreate the table WITHOUT the column (the pre-ticket shape).
    raw = scratch.raw
    raw.execute("ALTER TABLE versions RENAME TO versions_old")
    raw.execute(
        """
        CREATE TABLE versions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            params      TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            created_by_message TEXT NOT NULL DEFAULT '',
            parent      INTEGER,
            restored_from INTEGER,
            forked_from TEXT,
            pinned      INTEGER NOT NULL DEFAULT 0,
            archived    INTEGER NOT NULL DEFAULT 0,
            thumbnail   TEXT,
            bbox        TEXT,
            created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """
    )
    raw.execute("DROP TABLE versions_old")
    raw.commit()
    assert "exported_at" not in _column_names(scratch, "versions")
    migrate(scratch)  # must add the column, must not raise
    assert "exported_at" in _column_names(scratch, "versions")
    # Idempotent on the migrated shape.
    migrate(scratch)


# ---------------------------------------------------------------------------
# the record route
# ---------------------------------------------------------------------------


def test_export_event_is_recorded_on_the_specific_version(app_with_versions):
    """POST .../versions/{id}/export marks THAT version — an older one, not
    the latest — and the timeline carries the mark (the field the SPA's
    filmstrip renders)."""

    async def _call(client):
        pid = (await create_project(client))["id"]
        v1 = await create_version(client, pid, {"W": 20}, name="first")
        v2 = await create_version(client, pid, {"W": 30}, name="second")
        r = await client.post(f"/api/projects/{pid}/versions/{v1['id']}/export")
        assert r.status_code == 200, r.text
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return v1["id"], v2["id"], timeline

    v1, v2, timeline = run_async(app_with_versions, _call)
    by_id = {v["id"]: v for v in timeline}
    assert by_id[v1]["exported_at"] is not None
    assert "T" in by_id[v1]["exported_at"]  # an ISO timestamp, not a marker
    # The LATEST version is NOT marked — the mark belongs to the version
    # that was actually exported.
    assert by_id[v2]["exported_at"] is None


def test_export_of_unknown_version_404s(app_with_versions):
    async def _call(client):
        pid = (await create_project(client))["id"]
        await create_version(client, pid, {"W": 20}, name="first")
        r = await client.post(f"/api/projects/{pid}/versions/999/export")
        return r.status_code

    assert run_async(app_with_versions, _call) == 404


def test_reexport_updates_to_latest_export_time(app_with_versions):
    """Last export wins: a second export of the same version moves the mark
    to the later time (never preserves the first, never counts)."""

    async def _call(client):
        pid = (await create_project(client))["id"]
        v1 = await create_version(client, pid, {"W": 20}, name="first")
        await client.post(f"/api/projects/{pid}/versions/{v1['id']}/export")
        first = (
            (await client.get(f"/api/projects/{pid}/versions/{v1['id']}")).json()
        )["exported_at"]
        import time

        # The server stamps with second-resolution clock precision
        # (strftime %f), so two immediate calls can share a stamp — sleep
        # past the boundary rather than assume it.
        time.sleep(1.05)
        await client.post(f"/api/projects/{pid}/versions/{v1['id']}/export")
        second = (
            (await client.get(f"/api/projects/{pid}/versions/{v1['id']}")).json()
        )["exported_at"]
        return first, second

    first, second = run_async(app_with_versions, _call)
    assert first is not None and second is not None
    assert second > first  # ISO-8601 UTC strings compare chronologically


# ---------------------------------------------------------------------------
# THE RELOAD — the decisive test
# ---------------------------------------------------------------------------


def test_exported_mark_survives_a_page_reload(app_with_versions):
    """A NEW client session over the same app/DB (the page reload / "come
    back a day later" simulation) still sees the mark, because it lives in
    the versions table, not in client state. A client-only mark would fail
    this test by construction: nothing would exist for the fresh client to
    read."""

    async def _call(client):
        pid = (await create_project(client))["id"]
        v1 = await create_version(client, pid, {"W": 20}, name="first")
        v2 = await create_version(client, pid, {"W": 25}, name="second")
        await client.post(f"/api/projects/{pid}/versions/{v1['id']}/export")
        # "Reload": a FRESH client over the same app/DB (a new session —
        # the SPA re-fetches the timeline on mount).
        fresh = AsyncClient(
            transport=ASGITransport(app=app_with_versions), base_url="http://t2"
        )
        async with fresh:
            timeline = (await fresh.get(f"/api/projects/{pid}/versions")).json()
        return v1["id"], v2["id"], timeline

    v1, v2, timeline = run_async(app_with_versions, _call)
    by_id = {v["id"]: v for v in timeline}
    # The mark is still there after the "reload" — on the right version.
    assert by_id[v1]["exported_at"] is not None
    assert by_id[v2]["exported_at"] is None
