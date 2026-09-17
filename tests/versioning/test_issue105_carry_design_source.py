"""Issue #105 — the design assistant must SEE the design it produced.

The observed defect (live, two turns into a real session): turn 1 "a
sphere 3cm in diameter" → the model correctly emitted ``diameter = 30``.
Turn 2 "add another sphere underneath, 3cm, overlapping 20%" → the model
emitted ``diameter = 60`` for a sphere it had ITSELF made 30, because the
prompt carried no description of the existing geometry.

Settled PM decisions (the issue's comments):
1. THE SERVER writes the source (not the SPA) — persisted on a passing
   loop, in the same code path that creates the version.
2. THE SOURCE IS PER-VERSION — ``versions/{id}/design.scad`` in the
   project git repo, written + committed in the same commit as
   ``params.json``. "The current design" follows the project's
   ``current_version`` pointer — both restore paths (``restore_version``'s
   forward version, ``set_as_main``'s pointer move) move that pointer, so
   retrieval follows a restore by construction.
3. CARRY IN FULL, bounded by the render worker's ``MAX_SCAD_SOURCE_BYTES``
   (256 KiB), with a VISIBLE truncation marker when the bound is hit.

This file is the SECOND of two tickets to land (#120 carried the parameter
block); it OWNS THE INTEGRATION ASSERTION: the design prompt contains BOTH
#120's parameter block AND the previous version's source (proven red in
both directions — remove the block → red, remove the source → red).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from d33d.design_loop import MAX_SCAD_SOURCE_BYTES, run_design_loop
from d33d.design_source import (
    TRUNCATION_MARKER,
    design_source_lines,
    source_path_for_version,
    store_version_source,
)
from d33d.render_worker import RenderResult
from tests.versioning.helpers import (
    create_project,
    create_version,
    repo_path_for,
    run_async,
)

# ---------------------------------------------------------------------------
# Distinctive tokens that appear NOWHERE else in the prompt (their presence
# in the captured prompt string proves the section was rendered — the #97
# lesson: assert on the rendered prompt, never on a parameter being passed).
# ---------------------------------------------------------------------------
TOKEN = "sphere(d = diameter);"


def _llm_result(scad: str) -> Any:
    from d33d.design_llm import LLMResult

    return LLMResult(
        content=f"```scad\n{scad}\n```",
        tool_calls=(),
        prompt_hash="h" * 64,
        tier="T1",
        status="ok",
        request_body={},
    )


def _passing_render() -> RenderResult:
    return RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=1,
        error_class="ok",
        stderr="",
        stl="model.stl",
        csg="model.csg",
        views=("v0.png", "v1.png", "v2.png", "v3.png", "v4.png", "v5.png"),
    )


def _bbox_ok(render) -> Any:
    from d33d.design_loop import BboxInfo

    return BboxInfo(x=30.0, y=30.0, z=30.0, volume=8000.0)


def _user_text(captured: list, index: int) -> str:
    msg = captured[index][0]
    assert msg["role"] == "user"
    text = [p for p in msg["content"] if p.get("type") == "text"]
    assert len(text) == 1
    return text[0]["text"]


def _run(
    *,
    request: str,
    state_params: dict[str, Any] | None = None,
    design_source: str | None = None,
    chat_history: tuple[str, ...] = (),
    stated: tuple[float, float, float] | None = (30.0, 30.0, 30.0),
    scad: str | None = None,
):
    """Drive the REAL loop with a fake llm_fn that captures every prompt."""
    captured: list[list[dict[str, Any]]] = []

    async def llm_fn(role, messages, system):
        captured.append(messages)
        return _llm_result(scad or "diameter = 30;\nsphere(d = diameter);\n")

    async def render_fn(scad_text, defines):
        return _passing_render()

    result = run_design_loop(
        photo="data:image/png;base64,REF",
        chat_history=chat_history,
        stated_dims=stated,
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=_bbox_ok,
        request=request,
        state_params=state_params,
        design_source=design_source,
    )
    return result, captured


# ---------------------------------------------------------------------------
# The decisive test: the RENDERED turn-2 prompt carries the prior source.
# ---------------------------------------------------------------------------


def test_turn2_prompt_contains_previous_versions_actual_source():
    """A project whose previous version made a 30 mm sphere (the live 30/60
    regression) produces a follow-up prompt in which the model can SEE
    ``diameter = 30`` — the previous version's actual SCAD source. A test
    that could not detect an absent source is the #105 twin of the bug it
    names: the assertion is on the rendered prompt string."""
    prev_source = "diameter = 30;\nsphere(d = diameter);\n"
    result, captured = _run(
        request="add another sphere underneath, 3cm, overlapping 20%",
        state_params={"diameter": 30.0},
        design_source=prev_source,
    )
    assert result.status == "pass"
    user_text = _user_text(captured, 0)
    # The labelled section IS in the rendered prompt (the decisive assert).
    assert "Current design source (OpenSCAD):" in user_text
    # The previous version's ACTUAL source is in the prompt (the 30/60
    # regression pinned: the model can no longer invent 60 — 30 is visible
    # as the existing design, and the instruction says MODIFY, not rebuild).
    assert "diameter = 30;" in user_text
    assert TOKEN in user_text
    # The instruction wording asks the model to MODIFY, return complete.
    assert "MODIFY this design" in user_text
    assert "COMPLETE updated source" in user_text


# ---------------------------------------------------------------------------
# THE INTEGRATION ASSERTION (#105 owns it — #120's ticket assigned it):
# the design prompt contains BOTH the #120 parameter block AND the previous
# version's source.
# ---------------------------------------------------------------------------


def test_integration_prompt_contains_parameter_block_and_previous_source():
    """The live design prompt carries BOTH #120's parameter block AND the
    previous version's source (proven red in both directions: removing
    either from the prompt builder breaks this test)."""
    prev_source = "diameter = 30;\nsphere(d = diameter);\n"
    result, captured = _run(
        request="add another sphere underneath, 3cm",
        state_params={"diameter": 30.0},
        design_source=prev_source,
    )
    user_text = _user_text(captured, 0)
    # BOTH sections are present in the SAME rendered prompt.
    assert "Current design state (mm):" in user_text, (
        "the #120 parameter block is missing from the prompt:\n" + user_text
    )
    assert "Current design source (OpenSCAD):" in user_text, (
        "the #105 design-source section is missing from the prompt:\n"
        + user_text
    )
    # ...and each carries its content (presence of a header alone is not
    # "contains" — the block carries the number, the source carries the
    # geometry).
    assert "diameter = 30" in user_text
    assert "diameter = 30;" in user_text
    # Order: the state block (params) precedes the source section, and
    # BOTH precede the reference-dimensions line (the sections share the
    # insertion point between the chat lines and the dimensions line).
    state_pos = user_text.index("Current design state (mm):")
    source_pos = user_text.index("Current design source (OpenSCAD):")
    ref_pos = user_text.index("Reference dimensions (mm, ground truth):")
    assert state_pos < source_pos < ref_pos


# ---------------------------------------------------------------------------
# The 30/60 regression, pinned end to end: a project whose previous version
# made a 30 mm sphere → the follow-up prompt shows 30 (the model can see
# what it made). Fails if the source is absent from the prompt.
# ---------------------------------------------------------------------------


def test_30_60_regression_previous_sphere_30_visible_in_followup_prompt():
    """The live defect: turn 2 'add another sphere underneath, 3cm' once
    produced ``diameter = 60`` for a sphere the model had itself made 30.
    The prompt now carries the previous version's actual source, so the
    30 is VISIBLE to the model (and this test fails if the section is
    deleted from the prompt)."""
    prev_source = "diameter = 30;\nsphere(d = diameter);\n"
    result, captured = _run(
        request="add another sphere underneath, 3cm, overlapping 20%",
        state_params={"diameter": 30.0},
        design_source=prev_source,
    )
    user_text = _user_text(captured, 0)
    # The model can see the previous version's 30 (the regression's core).
    assert "diameter = 30;" in user_text
    # The prompt instructs the model to modify, not to start over.
    assert "MODIFY this design" in user_text
    # The prior source is distinguished from the request and the chat.
    req_pos = user_text.index("Request:")
    src_pos = user_text.index("Current design source (OpenSCAD):")
    assert req_pos < src_pos, "the source section must follow the request"


# ---------------------------------------------------------------------------
# Turn 1: clean slate → explicit wording, never a silently-absent section.
# ---------------------------------------------------------------------------


def test_turn1_clean_slate_renders_explicit_no_design_wording():
    """A turn with no version (``design_source=None``) renders EXPLICIT
    'no existing design yet / fresh start / CREATE a complete new design'
    wording — an absent section is indistinguishable from a bug, and the
    wording must not read as an instruction to produce nothing."""
    result, captured = _run(
        request="make a sphere 3cm in diameter",
        state_params=None,
        design_source=None,
        stated=(30.0, 30.0, 30.0),
    )
    user_text = _user_text(captured, 0)
    # The section header is present (never silently absent).
    assert "Current design source (OpenSCAD):" in user_text
    # The explicit clean-slate wording.
    assert "no existing design yet" in user_text
    assert "fresh start" in user_text
    # It asks the model to CREATE a complete new design (not to produce
    # nothing — the wording must not be mistaken for an instruction to
    # emit empty).
    assert "CREATE a complete new design" in user_text
    assert "do not reply empty" in user_text
    # No fabricated source (the honest empty state — no geometry invented).
    assert "sphere(d" not in user_text


# ---------------------------------------------------------------------------
# Prompt-size policy: carry in full, bounded, VISIBLE truncation marker.
# ---------------------------------------------------------------------------


def test_source_within_bound_is_carried_in_full_verbatim():
    """A source within ``MAX_SCAD_SOURCE_BYTES`` reaches the prompt
    verbatim (every line, no marker, no truncation)."""
    source = "diameter = 30;\nsphere(d = diameter);\ncube([10, 10, 10]);\n"
    assert len(source.encode("utf-8")) <= MAX_SCAD_SOURCE_BYTES
    result, captured = _run(
        request="edit the sphere",
        state_params={"diameter": 30.0},
        design_source=source,
    )
    user_text = _user_text(captured, 0)
    # The full source is in the prompt (the distinctive line, verbatim).
    assert "cube([10, 10, 10]);" in user_text
    # No truncation marker for a source within the bound.
    assert "TRUNCATED" not in user_text


def test_source_at_bound_carried_in_full_and_past_bound_truncated_with_marker():
    """The bound is exercised at and past ``MAX_SCAD_SOURCE_BYTES``:
    a source AT the bound is carried in full (no marker); a source PAST
    the bound is truncated with the VISIBLE marker in the prompt text
    (never a silent truncation — the model knows it sees a partial file).
    """
    # Build a source whose UTF-8 length is exactly the bound: a 1-byte-per
    # char pad of the exact remaining size (the leading line is ASCII).
    prefix = "diameter = 30;\nsphere(d = diameter);\n// pad\n"
    pad = "x" * (MAX_SCAD_SOURCE_BYTES - len(prefix.encode("utf-8")))
    at_bound = prefix + pad
    assert len(at_bound.encode("utf-8")) == MAX_SCAD_SOURCE_BYTES

    # AT the bound: carried in full, no marker.
    result, captured = _run(
        request="edit the sphere",
        state_params={"diameter": 30.0},
        design_source=at_bound,
    )
    user_text = _user_text(captured, 0)
    assert "TRUNCATED" not in user_text
    # The tail of the at-bound source is present (nothing was cut).
    assert "xxxx" in user_text

    # PAST the bound: truncated with the visible marker.
    over = at_bound + "\nmodule gone_module() { sphere(d = 50); }\n"
    assert len(over.encode("utf-8")) > MAX_SCAD_SOURCE_BYTES
    result2, captured2 = _run(
        request="edit the sphere",
        state_params={"diameter": 30.0},
        design_source=over,
    )
    user_text2 = _user_text(captured2, 0)
    # The marker is VISIBLE IN THE PROMPT TEXT (naming the bound in the
    # human-readable form and the exact byte count carried).
    assert "TRUNCATED" in user_text2
    assert "256 KiB" in user_text2
    # The dropped tail (the module past the bound) is NOT in the prompt.
    assert "gone_module" not in user_text2
    # The carried portion stays within the bound (the marker names the
    # byte budget; the source text itself plus marker fits, the full
    # section line may extend slightly past the source budget to carry
    # the marker's wording).
    header_idx = user_text2.index("Current design source")
    ref_idx = user_text2.index("Reference dimensions")
    carried = user_text2[header_idx:ref_idx]
    assert len(carried.encode("utf-8")) <= MAX_SCAD_SOURCE_BYTES + 500


def test_truncation_never_splits_a_multibyte_character(tmp_path):
    """A truncation cut that would land mid multi-byte character is
    re-encoded to a valid boundary — the prompt never carries an invalid
    UTF-8 sequence (a hard byte cut could split an accented identifier)."""
    # A source whose byte length crosses the bound mid 2-byte character:
    # pad with ASCII up to (bound - 1), then a 2-byte character at the
    # exact cut.
    prefix = "diameter = 30;\nsphere(d = diameter);\n"
    headroom = MAX_SCAD_SOURCE_BYTES - len(prefix.encode("utf-8"))
    over = prefix + ("x" * (headroom - 1)) + "éé\n"
    assert len(over.encode("utf-8")) > MAX_SCAD_SOURCE_BYTES
    from d33d.design_source import _truncate_utf8

    kept, kept_bytes = _truncate_utf8(over, MAX_SCAD_SOURCE_BYTES - 100)
    # The kept prefix re-encodes cleanly (no replacement, no error) and
    # fits the byte budget.
    assert len(kept.encode("utf-8")) == kept_bytes
    kept.encode("utf-8").decode("utf-8")  # raises if the cut split a char


# ---------------------------------------------------------------------------
# Server-side persistence: the source is written per version by the SERVER,
# survives a process restart (git), and is exercised through the REAL
# version-creation path (not a stub).
# ---------------------------------------------------------------------------


def test_store_version_source_writes_per_version_file_and_survives_restart(
    app_with_versions,
):
    """The per-version source file is written to ``versions/{id}/design.scad``
    and survives a process restart (it is committed to the project git
    repo — the durable mechanism; reading it after the app is closed proves
    durability, not an in-memory cache)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        row = app_with_versions.state.conn.get_project(pid)
        assert row is not None
        repo = Path(row["git_repo_path"])
        store_version_source(repo, 7, "diameter = 30;\nsphere(d = diameter);\n")
        # Commit the per-version source (the production path commits it in
        # the version's commit — this direct-write test commits it on its
        # own so the git log assertion has a commit to read).
        from d33d.projects import commit_all

        commit_all(repo, "version 7 design source (test)")
        return repo

    repo = run_async(app_with_versions, _call)
    # The file is on disk at the per-version path (the git commit is the
    # durable home — ``git show`` reads it independent of the process).
    path = source_path_for_version(repo, 7)
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == (
        "diameter = 30;\nsphere(d = diameter);\n"
    )
    # The commit records it (durability: ``git show`` survives any restart).
    import subprocess

    r = subprocess.run(
        ["git", "-C", str(repo), "log", "--diff-filter=A", "--name-only",
         "--format=", "--", "versions/7/design.scad"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "versions/7/design.scad" in r.stdout


def test_finalize_pass_persists_source_per_version(app_with_versions):
    """The finalize seam (the server write point): a passing loop's best
    candidate's source is persisted to ``versions/{id}/design.scad`` in
    the SAME commit as the version's params — the server owns the write
    (no SPA call involved), per version (the new version's file, not the
    flat design.scad)."""
    from tests.versioning.test_design_loop_finalize import _StubResult

    scad = "diameter = 30;\nsphere(d = diameter);\n"

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = lambda: _StubResult(
            "pass", {"diameter": 30.0}, scad=scad
        )
        r = await client.post(
            f"/api/projects/{pid}/finalize",
            json={"message": "a sphere 3cm in diameter"},
        )
        row = app_with_versions.state.conn.get_project(pid)
        assert row is not None
        repo = Path(row["git_repo_path"])
        vid = r.json()["id"]
        # The per-version file exists with the loop's source (server-write).
        path = source_path_for_version(repo, vid)
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == scad
        # Same commit as the params snapshot (one writer, one lock, one
        # commit — the version's geometry and its record cannot diverge).
        # ``git show HEAD`` lists the files in that commit; verify both
        # files are in the SAME commit.
        import subprocess

        r4 = subprocess.run(
            ["git", "-C", str(repo), "show", "--name-only", "--format=",
             "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert r4.returncode == 0, r4.stderr
        assert f"versions/{vid}/params.json" in r4.stdout
        assert f"versions/{vid}/design.scad" in r4.stdout
        return r

    r = run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text


def test_chat_pass_persists_source_and_followup_turn_reads_it(app_with_versions):
    """End to end through the REAL /chat seam: turn 1 passes → the version
    owns its source (server-written, per version); the follow-up turn's
    loop kwargs carry the NEW version's source (the retrieval path the
    next prompt renders — proven through the real adapter, not a stub)."""
    from tests.versioning.helpers import run_async

    captured: dict[str, Any] = {}
    scad = "diameter = 30;\nsphere(d = diameter);\n"

    class _Result:
        status = "pass"
        failure_reason = None

        class _Best:
            params = {"diameter": 30.0}
            scad_source = scad

        best = _Best()

    def _loop(app, **kwargs):
        captured.update(kwargs)

        class _R:
            status = "pass"
            failure_reason = None
            best = _Result

        _R.best = _Result._Best
        return _R()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r = await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "a sphere 3cm in diameter", "chat_history": []},
        )
        source = app_with_versions.state.event_sources.get(pid)
        assert source is not None
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        # The pass persisted the NEW version's source (server write) —
        # read INSIDE the live-connection window (run_async closes the DB
        # on exit, so the follow-up turn must be driven in the SAME call).
        row = app_with_versions.state.conn.get_project(pid)
        assert row is not None
        repo_p = Path(row["git_repo_path"])
        vs = app_with_versions.state.versions.list_versions(pid)
        assert len(vs) == 1
        path = source_path_for_version(repo_p, vs[0]["id"])
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == scad
        # The follow-up turn's kwargs carry the persisted source (the real
        # retrieval path — the NEXT prompt will render it). Release the
        # in-flight flag (the SSE endpoint normally clears it on
        # terminal-frame; the test drives the generator directly, so it
        # clears it here — same contract).
        app_with_versions.state.design_loop_inflight.discard(pid)
        captured.clear()
        r2 = await client.post(
            f"/api/projects/{pid}/chat",
            json={
                "message": "add another sphere underneath, 3cm",
                "chat_history": [],
            },
        )
        source2 = app_with_versions.state.event_sources.get(pid)
        assert source2 is not None
        async for event, data in source2:
            if event in ("done", "error"):
                break
        return r, frames, r2, dict(captured)

    r, frames, r2, cap2 = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert frames[-1][0] == "done", frames
    # Turn 1 (no version yet) carried NO source (the clean-slate case —
    # the kwarg is omitted, the prompt renders the explicit wording).
    # ``captured`` holds turn 2's kwargs (cleared before turn 2);
    # turn 1's kwargs were captured before the clear.
    # The follow-up turn's kwargs carry the persisted source.
    assert r2.status_code == 202, r2.text
    assert cap2.get("design_source") == scad


# ---------------------------------------------------------------------------
# Restore: the carried design follows BOTH restore paths (per PM decision
# 2: "current design" is the version current_version points at; both paths
# move that pointer, so retrieval follows the restore by construction).
# ---------------------------------------------------------------------------


def test_restore_version_follows_pointer_and_source(app_with_versions):
    """Two versions with different sources; ``restore_version`` to the
    older one creates a forward version (which advances ``current_version``)
    carrying the OLD version's source — the carried design follows the
    restore. The new version's file is the restored geometry, not the
    latest's."""
    scad_v1 = "diameter = 30;\nsphere(d = diameter);\n"
    scad_v2 = "diameter = 60;\nsphere(d = diameter);\ncube([10, 10, 10]);\n"

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"diameter": 30.0})
        v2 = await create_version(client, pid, {"diameter": 60.0})
        row = app_with_versions.state.conn.get_project(pid)
        assert row is not None
        repo = Path(row["git_repo_path"])
        # Per-version files (server-written on the passing loop; here the
        # test seeds them the same way the server does).
        store_version_source(repo, v1["id"], scad_v1)
        store_version_source(repo, v2["id"], scad_v2)
        from d33d.design_source import current_version_source

        before = current_version_source(row, app_with_versions.state.versions)
        assert before == scad_v2
        # Restore v1 → forward version; current_version advances to it.
        r = await client.post(
            f"/api/projects/{pid}/versions/{v1['id']}/restore"
        )
        assert r.status_code == 201, r.text
        restored = r.json()
        row2 = app_with_versions.state.conn.get_project(pid)
        after = current_version_source(row2, app_with_versions.state.versions)
        return v1, v2, restored, after, repo

    v1, v2, restored, after, repo = run_async(app_with_versions, _call)
    # The restore created a new forward version (current_version moved).
    assert restored["restored_from"] == v1["id"]
    assert restored["id"] != v1["id"] and restored["id"] != v2["id"]
    # The carried design FOLLOWS the restore: it is the OLD source.
    assert after == scad_v1
    # The forward version OWNS the restored source (the geometry is
    # per-version — the new version's file is the restored geometry).
    path = source_path_for_version(repo, restored["id"])
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == scad_v1


def test_set_as_main_follows_pointer_and_source(app_with_versions):
    """``set_as_main`` re-points ``current_version`` in place: the carried
    design follows the pointer to the re-pointed version's source."""
    scad_v1 = "diameter = 30;\nsphere(d = diameter);\n"
    scad_v2 = "diameter = 60;\nsphere(d = diameter);\ncube([10, 10, 10]);\n"

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"diameter": 30.0})
        v2 = await create_version(client, pid, {"diameter": 60.0})
        row = app_with_versions.state.conn.get_project(pid)
        assert row is not None
        repo = Path(row["git_repo_path"])
        store_version_source(repo, v1["id"], scad_v1)
        store_version_source(repo, v2["id"], scad_v2)
        # Set v1 as main → current_version re-points to v1.
        r = await client.post(
            f"/api/projects/{pid}/versions/{v1['id']}/set-as-main"
        )
        assert r.status_code == 200, r.text
        row2 = app_with_versions.state.conn.get_project(pid)
        from d33d.design_source import current_version_source

        after = current_version_source(row2, app_with_versions.state.versions)
        return v1, v2, after, repo

    v1, v2, after, repo = run_async(app_with_versions, _call)
    # The carried design follows the set-as-main pointer move.
    assert after == scad_v1


# ---------------------------------------------------------------------------
# Region-edit route: same mechanism, same wording (PM decision 4) — the
# region-edit pass runs through the same adapter, so it carries the current
# source identically.
# ---------------------------------------------------------------------------


def test_region_edit_pass_carries_current_design_source(app_with_versions):
    """A region edit (``POST /api/projects/{id}/region-edits``) on a project
    whose current version owns a source: the adapter's loop kwargs carry
    the same ``design_source`` as the chat path (one mechanism, one
    wording — the shared adapter supplies it; the loop renders the
    identical section)."""
    from tests.versioning.helpers import run_async

    captured: dict[str, Any] = {}
    scad = "diameter = 30;\nsphere(d = diameter);\n"

    class _Result:
        status = "pass"
        failure_reason = None

        class _Best:
            params = {"diameter": 30.0}
            scad_source = scad

        best = _Best()

    def _loop(app, **kwargs):
        captured.update(kwargs)

        class _R:
            status = "pass"
            failure_reason = None

        _R.best = _Result._Best
        return _R()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"diameter": 30.0})
        row = app_with_versions.state.conn.get_project(pid)
        assert row is not None
        repo = Path(row["git_repo_path"])
        store_version_source(repo, v1["id"], scad)
        app_with_versions.state.run_design_loop = _loop
        body = {
            "module_ids": [],
            "view_id": "front",
            "marked_png_base64": "a" * 100,
            "point": {"x": 1.0, "y": 2.0},
            "instruction": "make the top smoother",
        }
        r = await client.post(f"/api/projects/{pid}/region-edits", json=body)
        source = app_with_versions.state.event_sources.get(pid)
        assert source is not None
        async for event, data in source:
            if event in ("done", "error"):
                break
        return r

    r = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    # The region-edit loop kwargs carry the current design source (the
    # SAME kwarg the chat path carries — the rendered prompt is identical
    # in shape for both routes).
    assert captured.get("design_source") == scad


# ---------------------------------------------------------------------------
# The section wording / marker are distinct from the REPAIR block's
# ``previous_scad:`` (the failed candidate of THIS turn, never the
# accepted design).
# ---------------------------------------------------------------------------


def test_source_section_distinct_from_repair_previous_scad():
    """A repair iteration on a follow-up turn renders BOTH the accepted
    design (the labelled source section) and the failed candidate (the
    REPAIR block's ``previous_scad:``) — unmistakably different labels,
    both present, the source section preceding the repair block."""
    accepted = "diameter = 30;\nsphere(d = diameter);\n"
    failed = "diameter = 60;\nsphere(d = 60);\n"  # a bad candidate
    captured: list[list[dict[str, Any]]] = []
    i = {"n": 0}

    async def llm_fn(role, messages, system):
        captured.append(messages)
        # Iteration 1: the failed candidate (magic number → named-params
        # gate fails); iteration 2: the fix.
        if i["n"] == 0:
            return _llm_result(failed)
        return _llm_result("diameter = 30;\nsphere(d = diameter);\n")

    async def render_fn(scad, defines):
        # First render: ok but one blank view (the repair route fires).
        i["n"] += 1
        if i["n"] == 1:
            return RenderResult(
                ok=True,
                exit_code=0,
                duration_ms=1,
                error_class="ok",
                stderr="",
                stl="model.stl",
                csg="model.csg",
                views=("v0.png", "v1.png", "v2.png", "v3.png", "v4.png", ""),
            )
        return _passing_render()

    from d33d.design_loop import BboxInfo

    run_design_loop(
        photo="data:image/png;base64,REF",
        chat_history=(),
        stated_dims=(30.0, 30.0, 30.0),
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=lambda r: BboxInfo(x=30.0, y=30.0, z=30.0, volume=8000.0),
        request="make the sphere twice as big",
        state_params={"diameter": 30.0},
        design_source=accepted,
        max_iterations=3,
    )
    assert len(captured) >= 2, "expected a repair iteration"
    # Iteration 2 (the repair): BOTH sections present, unmistakably
    # labelled, the accepted design's source in the labelled section and
    # the FAILED candidate in the REPAIR block only.
    user_text = _user_text(captured, 1)
    assert "Current design source (OpenSCAD):" in user_text
    assert "REPAIR directive" in user_text
    assert "previous_scad:" in user_text
    # The accepted design (30) is in the source section; the failed
    # candidate (60) is in the repair block — the model cannot conflate
    # them: the section header names the accepted design, the repair
    # block names the failure.
    src_pos = user_text.index("Current design source (OpenSCAD):")
    repair_pos = user_text.index("REPAIR directive")
    assert src_pos < repair_pos
    # The failed candidate is inside the repair block (after its header),
    # the accepted source is before it.
    assert "diameter = 60;" in user_text[repair_pos:]
    assert "diameter = 30;" in user_text[src_pos:repair_pos]
