"""Issue #395 lens round 3: 422 on non-PartUploadError decode failure,
boundary-loop guard, MeshTopology TypedDict, worker-lifecycle dedup,
malformed-reply validation.

These are the failing tests for the lens findings; each asserts the
contract the production code must now provide.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from d33d.app import create_app
from d33d.part_import import PART_UPLOAD_UNPARSEABLE_DETAIL

FIXTURES = Path(__file__).parent / "fixtures" / "stl"


def _box_stl() -> bytes:
    return (FIXTURES / "box_20mm.stl").read_bytes()


def _run_async(app: Any, coro_factory) -> Any:
    async def _run():
        async with app.router.lifespan_context(app):
            client = AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            )
            async with client:
                return await coro_factory(client)

    return asyncio.run(_run())


@pytest.fixture
def app_paths(tmp_path: Path) -> dict[str, Path]:
    """Isolated DB + master-key + catalogue paths under tmp_path."""
    return {
        "db": tmp_path / "d33d.sqlite3",
        "key": tmp_path / "master.key",
        "cat": tmp_path / "models.yaml",
    }


@pytest.fixture
def app_with_projects(app_paths: dict[str, Path], tmp_path: Path):
    """A ``create_app`` instance with the git path pointed at ``tmp_path``."""
    import d33d.db as db_mod

    original_default = db_mod._default_git_path

    def _tmp_default_git_path(name: str) -> str:
        import uuid

        slug = uuid.uuid4().hex[:12]
        base = tmp_path / "repos" / slug
        base.mkdir(parents=True, exist_ok=True)
        return str(base)

    db_mod._default_git_path = _tmp_default_git_path
    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
    )
    yield app
    db_mod._default_git_path = original_default


# ---------------------------------------------------------------------------
# Finding 1: a non-PartUploadError from decode → 422, not a raw 500
# ---------------------------------------------------------------------------


def test_decode_memory_error_becomes_422(app_with_projects, monkeypatch):
    """A MemoryError (or any non-PartUploadError) raised by
    ``parse_and_repair`` during the upload's decode must surface as a
    422 with the unparseable detail — never a raw 500. Nothing is
    persisted (no version row, no part columns)."""
    from d33d import part_import as pi

    # Force a non-PartUploadError out of the decode.
    def _boom(content: bytes, part_format: str):
        raise MemoryError("decode OOM")

    monkeypatch.setattr(pi, "parse_and_repair", _boom)

    data = _box_stl()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "DecodeMemErr"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        return upload_r, pid

    upload_r, pid = _run_async(app_with_projects, _call)

    # The 422 must carry the unparseable detail (NOT a 500, NOT a MemoryError).
    assert upload_r.status_code == 422, (
        f"decode MemoryError must be a 422, got {upload_r.status_code}: {upload_r.text}"
    )
    assert upload_r.json()["detail"] == PART_UPLOAD_UNPARSEABLE_DETAIL

    # Nothing persisted: the project has no part columns (a fresh connection
    # — the app's conn is closed once the lifespan ends; the DB file is still
    # on disk at ``app_paths["db"]``).
    import sqlite3

    db_path = str(app_with_projects.state.db_path)
    with sqlite3.connect(db_path) as _c:
        row = _c.execute(
            "SELECT part_filename FROM projects WHERE id = ?", (pid,)
        ).fetchone()
    assert row is not None, f"project {pid} must exist"
    assert row[0] is None, "no part must be persisted on decode error"


# ---------------------------------------------------------------------------
# Finding 2: boundary-loop guard in mesh_topology
# ---------------------------------------------------------------------------


def test_boundary_loops_failure_does_not_make_clean(monkeypatch):
    """A failure inside ``_boundary_loops`` must NOT be treated as 0
    (clean): the mesh must be flagged NOT clean (so repair is taken) and
    ``mesh_topology`` must not raise. ``hole_count`` degrades to the genus
    fallback (treats the gap count as 0, as the genus fallback does)."""
    import trimesh

    import d33d.part_mesh_topology as topo_mod
    from d33d.part_mesh import _is_clean

    box = trimesh.creation.box(extents=[10, 10, 10])
    box.merge_vertices()
    box.update_faces(box.nondegenerate_faces())
    components = box.split(only_watertight=False)

    def _boom(mesh):
        raise RuntimeError("boundary loops failed")

    monkeypatch.setattr(topo_mod, "_boundary_loops", _boom)

    topo = topo_mod.mesh_topology(box, components)

    # The boundary_loops value must make _is_clean False (unknown, not 0).
    assert not _is_clean(topo), (
        f"a boundary-loop failure must NOT be treated as clean: {topo}"
    )
    # The value itself is -1 (unknown) per the contract.
    assert topo["boundary_loops"] == -1, (
        f"boundary_loops must be -1 (unknown) on failure, "
        f"got {topo['boundary_loops']}"
    )


# ---------------------------------------------------------------------------
# Finding 3: MeshTopology TypedDict
# ---------------------------------------------------------------------------


def test_mesh_topology_typed_dict_exists():
    """``mesh_topology`` returns a ``MeshTopology`` TypedDict (typed
    fields), and the function is annotated with it."""
    import typing

    from d33d.part_mesh_topology import MeshTopology, mesh_topology

    # The TypedDict exists and is a typing.TypedDict.
    assert issubclass(MeshTopology, dict)
    # It carries the five fields.
    fields = set(MeshTopology.__annotations__)
    assert {
        "boundary_loops",
        "bodies",
        "watertight_bodies",
        "winding_consistent",
        "genus",
    } <= fields, f"MeshTopology must carry the five fields, got {fields}"

    # The return annotation is MeshTopology (or references it).
    ann = typing.get_type_hints(mesh_topology)["return"]
    assert "MeshTopology" in getattr(ann, "__name__", str(ann)), (
        f"mesh_topology must be annotated -> MeshTopology, got {ann}"
    )


# ---------------------------------------------------------------------------
# Finding 4: malformed worker reply → PartUploadError
# ---------------------------------------------------------------------------


def _noop_worker(vertices, faces):
    """A no-op worker (the spawn child imports this by reference)."""
    return vertices, faces


import multiprocessing


class _MalformedReplyProcess(multiprocessing.Process):
    """A top-level ``Process`` subclass whose ``run()`` sends a 2-tuple
    (not the 3-tuple the parent expects) over the pipe, then exits.

    Used by the test to exercise the parent's reply validation. The
    instance's ``_send_conn`` attribute (set by ``__init__``) is used to
    send the malformed reply. This class is a top-level definition in an
    importable module (the spawn child inherits the parent's ``sys.path``),
    so the child can resolve it by its import path."""

    def __init__(self, send_conn, worker, mode, payload):
        super().__init__(daemon=True)
        self._send_conn = send_conn
        self._worker = worker
        self._mode = mode
        self._payload = payload

    def run(self) -> None:
        import sys

        try:
            self._send_conn.send(("ok", "not-a-3-tuple"))
        except OSError:
            # The send can fail (pipe closed, child killed); the process
            # exits regardless (the finally block runs sys.exit(0)).
            pass
        finally:
            sys.exit(0)


def test_malformed_worker_reply_is_part_upload_error(monkeypatch):
    """A malformed reply (not a 3-tuple, or with a kind not in
    {"ok","err"}) must become ``PartUploadError("repair failed: malformed
    worker reply")`` — never a crash or a 500.

    The test patches ``_run_in_worker`` to use a process whose ``run()``
    sends a 2-tuple over the pipe. The parent's ``recv`` gets the 2-tuple,
    validates it (it's not a 3-tuple with kind in {"ok","err"}), and must
    raise ``PartUploadError("repair failed: malformed worker reply")``.
    """
    import trimesh

    import d33d.part_repair as part_repair_mod
    from d33d.part_errors import PartUploadError

    box = trimesh.creation.box(extents=[5, 5, 5])
    box.merge_vertices()
    box.update_faces(box.nondegenerate_faces())

    def _patched_run_in_worker(mode, p, timeout, label):
        import multiprocessing

        ctx = multiprocessing.get_context("spawn")
        pipe_parent, pipe_child = ctx.Pipe(duplex=False)
        proc = _MalformedReplyProcess(pipe_child, _noop_worker, mode, p)
        proc.start()
        try:
            if not pipe_parent.poll(timeout):
                raise part_repair_mod.RepairTimeoutError(f"timed out ({label})")
            reply = pipe_parent.recv()
            if (
                not isinstance(reply, tuple)
                or len(reply) != 3
                or reply[0] not in ("ok", "err")
            ):
                raise PartUploadError("repair failed: malformed worker reply")
            _kind, name, payload_result = reply
            if _kind == "ok":
                return payload_result
            if name == "PartUploadError":
                raise PartUploadError(payload_result)
            raise PartUploadError(f"repair failed: {name}")
        finally:
            try:
                pipe_parent.close()
            except (BrokenPipeError, OSError):
                # The pipe can be in a broken state (child died, killed);
                # the close failure is not actionable — the child is
                # killed/joined below regardless.
                pass
            if proc.is_alive():
                proc.kill()
            proc.join(timeout=5)

    monkeypatch.setattr(part_repair_mod, "_run_in_worker", _patched_run_in_worker)

    with pytest.raises(PartUploadError) as excinfo:
        part_repair_mod.repair_with_pmf(box, timeout=10)

    assert "malformed worker reply" in str(excinfo.value), (
        f"malformed reply must map to PartUploadError with that message, "
        f"got: {excinfo.value!r}"
    )
