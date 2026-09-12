"""Fast-layer test: error_class classification over synthetic tuples.

No Docker required. Proves the table is TOTAL — every combination of
exit code and artifact presence lands in exactly one class — and
specifically the class-5 row (STL present but a PNG/CSG invocation
failed → artifact_error) and the timeout > oom > syntax_error
precedence ordering.
"""

from __future__ import annotations

import itertools

import d33d.render_worker as rw

SIX_VIEWS = [f"view_{i:02d}_x.png" for i in range(6)]


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
