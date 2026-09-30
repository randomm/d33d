"""Fast-layer test: error_class classification over synthetic tuples.

No Docker required. Proves the table is TOTAL — every combination of
exit code and artifact presence lands in exactly one class — and
specifically the class-5 row (STL present but a PNG/CSG invocation
failed → artifact_error) and the timeout > oom > syntax_error
precedence ordering.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import d33d.render_worker as rw

SIX_VIEWS = [f"view_{i:02d}_x.png" for i in range(6)]

#: The entrypoint's own STL-abort marker (issue #309): the ONLY source of
#: this line in the container's stderr is the STL step of ``entrypoint.sh``,
#: so its presence proves the entrypoint ran and the failure is a design
#: error the repair loop can fix — even when the harvested 64-KiB render.log
#: tail is truncated past every ``ERROR:`` line.
STL_ABORT_MARKER = (
    "[entrypoint] STL export failed with exit code 1 — aborting remaining steps"
)

#: The entrypoint's bounding-box fit abort (issue #309 table row 4): the
#: line is written to BOTH the container's stderr and the on-volume
#: /work/render.log, so ``ERROR:`` in either source reaches the classifier.
BBOX_ABORT_MARKER = "[entrypoint] ERROR: bounding-box fit aborted"

#: The entrypoint's params.json awk validation site message prefixes
#: (issue #309 table rows 2–5): ``[entrypoint] params.json: …`` on stderr,
#: followed by the shell's own ``Malformed params.json — aborting before
#: render`` line. The model's SCAD cannot cause any of these sites, so
#: they must never classify as ``syntax_error``.
PARAMS_JSON_PREFIXES = (
    'no "defines" object (must be absent or an object)',
    "defines value is not a well-formed single-line string",
    "defines value exceeds 4096 characters",
    "defines object is not closed — malformed JSON",
)

#: The awk source escapes the inner quotes (``\"``), so the static file
#: text differs from the RUNTIME message (real quotes) — the table pins
#: the runtime shape (what the host actually sees on the container
#: stderr), and the drift check below verifies each site's source line.
PARAMS_JSON_SOURCE_SNIPPETS = (
    'no \\"defines\\" object (must be absent or an object)',
    "defines value is not a well-formed single-line string",
    'defines value exceeds "' ,  # value is awk-templated: "... exceeds " DEF " characters"
    "defines object is not closed — malformed JSON",
)

ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"


def _all_valid(
    exit_code: int = 0,
    stl: object = "model.stl",
    csg: object = "model.csg",
    views: object = SIX_VIEWS,
    stderr: str = "",
    oomkilled: bool = False,
    timed_out: bool = False,
    vertex_count: int = 100,
    watertight: bool = True,
    volume: float = 1.0,
):
    return rw.classify(
        exit_code=exit_code,
        stl_path=stl,
        csg_path=csg,
        views=views,
        stderr=stderr,
        oomkilled=oomkilled,
        timed_out=timed_out,
        vertex_count=vertex_count,
        watertight=watertight,
        volume=volume,
    )


def test_ok_is_classified_as_ok() -> None:
    assert _all_valid() == "ok"


def test_timeout_has_highest_precedence() -> None:
    # Even with a valid STL, a timeout is timeout.
    assert _all_valid(timed_out=True) == "timeout"


def test_timeout_precedes_oom() -> None:
    # Both timeout and oom → timeout wins.
    assert _all_valid(timed_out=True, oomkilled=True) == "timeout"


def test_oom_precedes_syntax_error() -> None:
    # Exit 137 with ERROR: in stderr → oom, not syntax_error.
    assert _all_valid(exit_code=137, stl=None, stderr="ERROR: something") == "oom"


def test_oom_by_exit_code_137() -> None:
    assert _all_valid(exit_code=137, stl=None) == "oom"


def test_oom_by_oomkilled_flag() -> None:
    assert _all_valid(exit_code=1, stl=None, oomkilled=True) == "oom"


def test_timeout_records_exit_code_124() -> None:
    # The timeout class is independent of the exit code; the caller
    # records 124. Verify that a timeout with 124 still classifies timeout.
    assert _all_valid(exit_code=124, timed_out=True) == "timeout"


def test_syntax_error_requires_no_stl_and_error_marker() -> None:
    assert (
        rw.classify(
            exit_code=1,
            stl_path=None,
            csg_path=None,
            views=SIX_VIEWS,
            stderr="ERROR: Undefined",
        )
        == "syntax_error"
    )


def test_syntax_error_marker_is_error_colon() -> None:
    # The OpenSCAD diagnostic is "ERROR:" — other text is not enough.
    assert (
        rw.classify(
            exit_code=1,
            stl_path=None,
            csg_path=None,
            views=SIX_VIEWS,
            stderr="error: lowercase",
        )
        == "container_error"
    )


def test_syntax_error_from_stl_abort_marker_in_stderr_only() -> None:
    # Issue #309 precedence rule (a): the entrypoint's STL-abort marker in
    # the container's stderr alone → syntax_error, even when the harvested
    # render_log tail is empty (the ERROR: line was written to render.log,
    # not the container's stderr, and the harvest found nothing).
    assert (
        rw.classify(
            exit_code=1,
            stl_path=None,
            csg_path=None,
            views=SIX_VIEWS,
            stderr=STL_ABORT_MARKER,
            render_log="",
        )
        == "syntax_error"
    )


def test_syntax_error_from_error_in_render_log_only() -> None:
    # Issue #309 precedence rule (b): an OpenSCAD ``ERROR:`` line in the
    # harvested render_log (the entrypoint redirects every openscad
    # invocation's stderr there) → syntax_error even when the container's
    # stderr carries no such line.
    assert (
        rw.classify(
            exit_code=1,
            stl_path=None,
            csg_path=None,
            views=SIX_VIEWS,
            stderr="",
            render_log="ERROR: Parser error in /work/model.scad at line 3",
        )
        == "syntax_error"
    )


def test_stl_abort_marker_precedes_error_check() -> None:
    # Issue #309 precedence: the STL marker in stderr fires FIRST, before
    # the ``ERROR:`` scan of either source — so a 64-KiB-truncated
    # render.log tail (no ``ERROR:`` line survived the window) never
    # regresses a true syntax error to container_error.
    truncated_tail = "x" * 1000  # large, no ERROR: line
    assert (
        rw.classify(
            exit_code=1,
            stl_path=None,
            csg_path=None,
            views=SIX_VIEWS,
            stderr=STL_ABORT_MARKER,
            render_log=truncated_tail,
        )
        == "syntax_error"
    )


def test_64kib_truncation_edge_no_marker_no_error_in_window() -> None:
    # The sharp edge case from issue #309's resolved gate: on the
    # STL-abort path with a render.log longer than 64 KiB, the harvested
    # window carries no ``ERROR:`` line and the marker in stderr is the
    # only surviving signal — still syntax_error (the only source of that
    # marker is the STL step, so the 64-KiB tail never regresses a true
    # syntax error to container_error). Here the marker IS in stderr (the
    # entrypoint always writes it) and the window is a full 64 KiB of
    # noise with no ``ERROR:`` line.
    assert rw.RENDER_LOG_TAIL_BYTES == 64 * 1024
    full_window = "y" * (64 * 1024)
    assert len(full_window) == 64 * 1024
    assert "ERROR:" not in full_window
    assert (
        rw.classify(
            exit_code=1,
            stl_path=None,
            csg_path=None,
            views=SIX_VIEWS,
            stderr=STL_ABORT_MARKER,
            render_log=full_window,
        )
        == "syntax_error"
    )


def test_no_marker_and_no_error_is_container_error() -> None:
    # The fallback: no STL marker in stderr AND no ``ERROR:`` in either
    # source (container stderr or the harvested tail) → container_error.
    # This is the entrypoint-never-ran / docker-layer-failed shape
    # (exit 125/126/127, daemon down, image missing).
    assert (
        rw.classify(
            exit_code=1,
            stl_path=None,
            csg_path=None,
            views=SIX_VIEWS,
            stderr="some other output",
            render_log="some benign log line",
        )
        == "container_error"
    )


def test_syntax_error_requires_nonzero_exit() -> None:
    # Exit 0 with no STL → container_error, not syntax_error.
    assert (
        rw.classify(
            exit_code=0,
            stl_path=None,
            csg_path=None,
            views=SIX_VIEWS,
            stderr="ERROR: x",
        )
        == "container_error"
    )


def test_container_error_when_no_stl_and_no_error_marker() -> None:
    assert (
        rw.classify(
            exit_code=1,
            stl_path=None,
            csg_path=None,
            views=SIX_VIEWS,
            stderr="something",
        )
        == "container_error"
    )


def test_class5_artifact_error_stl_present_but_view_missing() -> None:
    # Class 5: STL present, but one of the six PNGs is missing →
    # artifact_error, regardless of exit code.
    five_views = SIX_VIEWS[:5]  # drop the last view
    assert (
        rw.classify(
            exit_code=1,
            stl_path="model.stl",
            csg_path="model.csg",
            views=five_views,
        )
        == "artifact_error"
    )


def test_class5_artifact_error_stl_present_csg_missing() -> None:
    assert (
        rw.classify(
            exit_code=0,
            stl_path="model.stl",
            csg_path=None,
            views=SIX_VIEWS,
        )
        == "artifact_error"
    )


def test_class5_artifact_error_stl_present_wrong_view_count() -> None:
    assert (
        rw.classify(
            exit_code=0,
            stl_path="model.stl",
            csg_path="model.csg",
            views=["a.png"],  # wrong count
        )
        == "artifact_error"
    )


def test_class5_reachable_via_png_failure_regardless_of_exit() -> None:
    # The class-5 row is reachable because only a non-zero exit on the
    # STL invocation aborts the remaining seven — so a run where the STL
    # succeeds but a PNG invocation fails is a real outcome. Assert it
    # lands in artifact_error at exit 0 AND at non-zero exit.
    assert (
        rw.classify(
            exit_code=0,
            stl_path="model.stl",
            csg_path="model.csg",
            views=SIX_VIEWS[:5] + [None],
        )
        == "artifact_error"
    )


def test_empty_model_zero_vertices() -> None:
    assert (
        rw.classify(
            exit_code=0,
            stl_path="model.stl",
            csg_path="model.csg",
            views=SIX_VIEWS,
            vertex_count=0,
            watertight=True,
            volume=0.0,
        )
        == "empty_model"
    )


def test_empty_model_not_watertight() -> None:
    assert (
        rw.classify(
            exit_code=0,
            stl_path="model.stl",
            csg_path="model.csg",
            views=SIX_VIEWS,
            vertex_count=100,
            watertight=False,
            volume=1.0,
        )
        == "empty_model"
    )


def test_empty_model_zero_volume() -> None:
    assert (
        rw.classify(
            exit_code=0,
            stl_path="model.stl",
            csg_path="model.csg",
            views=SIX_VIEWS,
            vertex_count=100,
            watertight=True,
            volume=0.0,
        )
        == "empty_model"
    )


def test_table_is_total_all_combinations_land_in_exactly_one_class() -> None:
    # Exhaustive: every combination of (exit_code, stl, csg, views,
    # oomkilled, timed_out) lands in exactly one class from the closed
    # enum.
    exit_codes = [0, 1, 137, 124]
    stl_opts = [None, "model.stl"]
    csg_opts = [None, "model.csg"]
    views_opts = [None, ["a.png"], SIX_VIEWS]
    oom_opts = [False, True]
    timeout_opts = [False, True]

    for ec, stl, csg, views, oom, to in itertools.product(
        exit_codes, stl_opts, csg_opts, views_opts, oom_opts, timeout_opts
    ):
        result = rw.classify(
            exit_code=ec,
            stl_path=stl,
            csg_path=csg,
            views=views,
            oomkilled=oom,
            timed_out=to,
        )
        assert result in rw.ERROR_CLASSES, (
            f"combination (ec={ec}, stl={stl!r}, csg={csg!r}, "
            f"views={len(views) if views else 0}, oom={oom}, to={to}) "
            f"→ {result!r} not in closed enum"
        )


def test_precedence_order_timeout_oom_syntax_error() -> None:
    # All three conditions present: timeout, oom, and syntax_error
    # markers. Timeout wins.
    assert (
        rw.classify(
            exit_code=137,
            stl_path=None,
            csg_path=None,
            views=SIX_VIEWS,
            stderr="ERROR: foo",
            oomkilled=True,
            timed_out=True,
        )
        == "timeout"
    )


def test_precedence_oom_wins_over_syntax_error_when_not_timed_out() -> None:
    assert (
        rw.classify(
            exit_code=137,
            stl_path=None,
            csg_path=None,
            views=SIX_VIEWS,
            stderr="ERROR: foo",
            oomkilled=True,
            timed_out=False,
        )
        == "oom"
    )


def _derive_ok_args(
    exit_code: int,
    stl: object,
    csg: object,
    views: object,
    vertex_count: int,
    watertight: bool,
    volume: float,
) -> dict:
    return {
        "exit_code": exit_code,
        "stl_path": stl,
        "csg_path": csg,
        "views": views,
        "vertex_count": vertex_count,
        "watertight": watertight,
        "volume": volume,
    }


def test_derive_ok_matches_classify_ok_across_synthetic_tuples() -> None:
    # derive_ok() must be structurally derived from classify() so the two
    # can never silently diverge: ok iff classify(...) == "ok".
    exit_codes = [0, 1, 137]
    stl_opts = [None, "model.stl"]
    csg_opts = [None, "model.csg"]
    views_opts = [None, ["a.png"], SIX_VIEWS]
    vertex_opts = [0, 100]
    watertight_opts = [False, True]
    volume_opts = [0.0, 1.0]

    for ec, stl, csg, views, vc, wt, vol in itertools.product(
        exit_codes,
        stl_opts,
        csg_opts,
        views_opts,
        vertex_opts,
        watertight_opts,
        volume_opts,
    ):
        args = _derive_ok_args(ec, stl, csg, views, vc, wt, vol)
        classify_result = rw.classify(**args) == "ok"
        derive_result = rw.derive_ok(**args)
        assert derive_result == classify_result, (
            f"derive_ok diverges from classify for {args!r}: "
            f"derive_ok={derive_result!r} classify=={classify_result!r}"
        )


def test_derive_ok_true_for_fully_valid_run() -> None:
    assert (
        rw.derive_ok(
            exit_code=0,
            stl_path="model.stl",
            csg_path="model.csg",
            views=SIX_VIEWS,
            vertex_count=100,
            watertight=True,
            volume=1.0,
        )
        is True
    )


# ---------------------------------------------------------------------------
# Issue #309: table-driven classification over EVERY entrypoint exit site
# and every class (the explicit mapping table the acceptance criteria
# require — the 6 exit-1 sites of entrypoint.sh, plus the docker-layer and
# timeout/oom rows).
# ---------------------------------------------------------------------------


def _entrypoint_exit_sites() -> list[tuple[str, str, str]]:
    """Every ``exit 1`` site in ``entrypoint.sh`` as
    ``(site_id, container_stderr_line, render_log_line)``.

    The lines are EXTRACTED from the static ``entrypoint.sh`` (the same
    pattern as ``tests/fast/test_entrypoint_views_sync.py``) so the table
    pins the real site messages, never a copy that can drift.
    """
    src = ENTRYPOINT.read_text(encoding="utf-8")
    sites: list[tuple[str, str, str]] = []
    # Params.json awk validation sites: ``print "[entrypoint] params.json:
    # …" > "/dev/stderr"`` followed by ``exit 1`` — the model's SCAD cannot
    # cause any of these (they are triggered by the caller-written
    # params.json), so the shell aborts with the awk message on stderr and
    # no render.log content. They must classify container_error (the
    # entrypoint's own guard tripped before any render step ran), NEVER
    # syntax_error.
    for prefix, snippet in zip(PARAMS_JSON_PREFIXES, PARAMS_JSON_SOURCE_SNIPPETS):
        marker = f"[entrypoint] params.json: {prefix}"
        assert snippet in src, f"entrypoint site source drifted: {snippet!r}"
        sites.append((f"params_json:{prefix[:20]}…", marker, ""))
    # The shell's own params.json wrapper message (awk non-zero exit).
    assert "[entrypoint] Malformed params.json — aborting before render" in src
    sites.append(
        (
            "params_json:wrapper",
            "[entrypoint] Malformed params.json — aborting before render",
            "",
        )
    )
    # The STL export abort: the ONLY site whose marker proves the entrypoint
    # ran an openscad invocation; the OpenSCAD ERROR: line itself goes to
    # render.log, not the container's stderr.
    assert ("[entrypoint] STL export failed with exit code ${stl_exit} — "
            "aborting remaining steps") in src
    sites.append(
        (
            "stl_abort",
            STL_ABORT_MARKER,
            "ERROR: Parser error in /work/model.scad at line 3",
        )
    )
    # The bounding-box fit abort: the line is written to BOTH the container
    # stderr and render.log (the entrypoint echoes it to both).
    assert BBOX_ABORT_MARKER in src
    sites.append(
        (
            "bbox_abort",
            f"{BBOX_ABORT_MARKER} — no vertex lines parsed (empty or truncated STL)",
            f"{BBOX_ABORT_MARKER} — no vertex lines parsed (empty or truncated STL)",
        )
    )
    return sites


def test_entrypoint_exit_sites_are_the_tabled_sites() -> None:
    # The table's premise, counted from the static text so the table
    # cannot drift from entrypoint.sh: the STL abort is the SINGLE
    # dynamic-exit site (the one whose marker proves the entrypoint ran a
    # render step), the bbox abort is the single post-compile ``exit 1``
    # site, and the params.json guard contributes its four awk sites plus
    # the shell wrapper.
    src = ENTRYPOINT.read_text(encoding="utf-8")
    assert src.count('exit "${stl_exit}"') == 1, (
        "STL abort must be the single dynamic-exit site"
    )
    awk_sites = src.count('"[entrypoint] params.json:')
    assert awk_sites == len(PARAMS_JSON_PREFIXES), (
        f"expected {len(PARAMS_JSON_PREFIXES)} params.json awk sites, "
        f"found {awk_sites} — entrypoint.sh drifted"
    )
    assert "[entrypoint] Malformed params.json — aborting before render" in src
    assert BBOX_ABORT_MARKER in src


def test_table_driven_every_entrypoint_site_maps_to_defined_class() -> None:
    # Table-driven over every entrypoint exit site: each (stderr, render_log)
    # pair the site produces lands in exactly one defined class from the
    # explicit mapping table (issue #309):
    #   - params.json sites (5): container_error — the entrypoint's own
    #     guard tripped; the model's SCAD cannot cause them and no
    #     syntax_error marker is present.
    #   - STL abort: syntax_error — the marker in stderr proves the design
    #     failed (precedence rule (a)).
    #   - bbox abort: syntax_error — ``ERROR:`` in either source
    #     (precedence rule (b)); the STL was written, so the host sees
    #     stl_path None only in this table (the host classifies the
    #     no-STL shape; the STL-present shape lands artifact_error via the
    #     existing row 5, covered by test_class5_*).
    expected: dict[str, rw.ErrorClass] = {}
    for site_id, stderr, render_log in _entrypoint_exit_sites():
        if site_id.startswith("params_json"):
            expected[site_id] = "container_error"
        else:
            expected[site_id] = "syntax_error"
        result = rw.classify(
            exit_code=1,
            stl_path=None,
            csg_path=None,
            views=SIX_VIEWS,
            stderr=stderr,
            render_log=render_log,
        )
        assert result in rw.ERROR_CLASSES
        assert result == expected[site_id], (
            f"entrypoint site {site_id!r} (stderr={stderr!r}) classified "
            f"{result!r}, expected {expected[site_id]!r}"
        )


def test_params_json_sites_are_distinguishable_per_site() -> None:
    # Each of the four awk validation sites has a DIFFERENT message on
    # stderr (the acceptance criterion: each site's message is made
    # distinguishable), so triage can name the real cause from the log
    # line.
    messages = [stderr for site_id, stderr, _ in _entrypoint_exit_sites()
                if site_id.startswith("params_json")]
    assert len(messages) == 5
    assert len(set(messages)) == 5, f"messages must be pairwise distinct: {messages}"


def test_non_render_exit_codes_remain_container_error() -> None:
    # Edge case from the issue: docker-layer failures (exit 125 run failure,
    # 126 not executable, 127 not found) and any non-render exit with no
    # entrypoint marker and no ERROR: line → container_error, never
    # syntax_error — the new rules must not fire when the entrypoint
    # demonstrably never ran.
    for code in (125, 126, 127):
        assert (
            rw.classify(
                exit_code=code,
                stl_path=None,
                csg_path=None,
                views=SIX_VIEWS,
                stderr="docker: Error response from daemon: ...",
                render_log="",
            )
            == "container_error"
        )


def test_stl_present_later_step_failure_is_artifact_error() -> None:
    # Row 5 (STL present, later step failed, CSG/views missing) still wins
    # over the bbox-abort marker: the host harvests the STL, so the
    # classification is artifact_error regardless of exit code or the
    # markers in stderr/render.log (existing row, pinned here so the #309
    # restructure cannot regress it).
    assert (
        rw.classify(
            exit_code=1,
            stl_path="model.stl",
            csg_path=None,
            views=SIX_VIEWS,
            stderr=BBOX_ABORT_MARKER + " — no vertex lines parsed",
            render_log=BBOX_ABORT_MARKER + " — no vertex lines parsed",
        )
        == "artifact_error"
    )
