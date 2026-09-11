"""Fast-layer tests for Orca-family result.json success semantics (ticket #4, gate 6).

Pins the corrected contract in ``d33d/slicer.py``: when the Orca-family
(QIDI/Orca) branch produces G-code, ``result.json`` is the machine-readable
verdict. A missing or corrupt ``result.json`` (``json_rc == -1``) must be a
FAILURE, not a default pass — the same stricter semantics as the
PrusaSlicer path (process exit code only). The old behaviour
(``ok = json_rc == 0 if json_rc != -1 else True``) treated an unreadable
verdict as success, which is backwards.
"""

from __future__ import annotations

import json

from d33d import slicer
from d33d.slicer import SliceDryRunResult, _parse_orca_result_json


def _stub_ok() -> SliceDryRunResult:
    """A gate-6 stub that passes (no real slicer needed)."""
    return SliceDryRunResult(
        ok=True,
        slicer="stub",
        gcode_path="stub.gcode",
        gcode_lines=1,
        return_code=0,
        error_string="",
        objects=1,
        detail="stubbed success",
    )


def test_orca_missing_result_json_is_failure(tmp_path, monkeypatch):
    """G-code produced but no result.json → ok=False (fail closed), not a
    default pass. Drives the real ``slice_orca_family`` + result.json parse
    against a fake binary that writes a G-code file and no result.json."""
    fake_bin = tmp_path / "fake-orca"
    fake_bin.write_text('#!/bin/sh\ntouch "$5".gcode\n')
    fake_bin.chmod(0o755)
    # The argv the driver builds: [bin, model, --slice, 0, --no-check, --outputdir, dir]
    # The shell script receives model=$1 ... outputdir=$7; write plate_1.gcode there.
    fake_bin.write_text(
        "#!/bin/sh\n"
        'outdir=""\n'
        'for a in "$@"; do :; done\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--outputdir" ]; then outdir="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        'touch "$outdir/plate_1.gcode"\n'
    )
    fake_bin.chmod(0o755)
    (tmp_path / "model.stl").write_text("stl solid x\nendsolid x\n")

    monkeypatch.setattr(
        slicer, "find_slicer", lambda kind: str(fake_bin) if kind == "orca" else None
    )

    result = slicer.slice_dry_run(str(tmp_path / "model.stl"))

    assert result.ok is False
    assert result.slicer == "orca"
    # G-code was produced (proves the binary ran) but the missing result.json
    # must not be treated as success.
    assert result.gcode_path is not None
    assert result.return_code == -1
    assert "result.json" in result.error_string


def test_orca_corrupt_result_json_is_failure(tmp_path, monkeypatch):
    """Corrupt (unparseable) result.json → ok=False, same as missing."""
    fake_bin = tmp_path / "fake-orca"
    fake_bin.write_text(
        "#!/bin/sh\n"
        'outdir=""\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--outputdir" ]; then outdir="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        'touch "$outdir/plate_1.gcode"\n'
        "echo 'not json {{{' > \"$outdir/result.json\"\n"
    )
    fake_bin.chmod(0o755)
    (tmp_path / "model.stl").write_text("stl solid x\nendsolid x\n")

    monkeypatch.setattr(
        slicer, "find_slicer", lambda kind: str(fake_bin) if kind == "orca" else None
    )

    result = slicer.slice_dry_run(str(tmp_path / "model.stl"))

    assert result.ok is False
    assert result.slicer == "orca"
    assert result.gcode_path is not None
    assert "result.json" in result.error_string


def _wrong_shape_case(tmp_path, payload: object) -> None:
    """A valid-JSON but wrong-shape result.json must degrade to the
    failure sentinel ``(-1, "...", None)`` without raising."""
    (tmp_path / "result.json").write_text(json.dumps(payload))
    rc, err, objects = _parse_orca_result_json(tmp_path)
    assert rc == -1
    assert objects is None
    assert isinstance(err, str) and err


def test_wrong_shape_result_json_array(tmp_path):
    """(a) result.json is a JSON array, not an object → sentinel, no crash."""
    _wrong_shape_case(tmp_path, [1, 2, 3])


def test_wrong_shape_result_json_string_payload(tmp_path):
    """Valid JSON that is a bare string → sentinel, no crash."""
    _wrong_shape_case(tmp_path, "hello")


def test_wrong_shape_result_json_number(tmp_path):
    """Valid JSON that is a bare number → sentinel, no crash."""
    _wrong_shape_case(tmp_path, 42)


def test_wrong_shape_result_json_string_return_code(tmp_path):
    """(b) return_code is a string instead of int → sentinel rc, no crash."""
    _wrong_shape_case(tmp_path, {"return_code": "not-a-number"})


def test_wrong_shape_result_json_null_return_code(tmp_path):
    """(c) return_code is null → sentinel rc, no crash."""
    _wrong_shape_case(tmp_path, {"return_code": None})


def test_wrong_shape_result_json_plates_not_list(tmp_path):
    """(d) sliced_plates is a dict instead of a list → no crash."""
    _wrong_shape_case(tmp_path, {"return_code": 0, "sliced_plates": {"objects": [1]}})


def test_wrong_shape_result_json_plate_not_dict(tmp_path):
    """(e) an entry in sliced_plates is not a dict → no crash."""
    _wrong_shape_case(tmp_path, {"return_code": 0, "sliced_plates": ["not a plate"]})


def test_wrong_shape_result_json_objects_not_list(tmp_path):
    """sliced_plates[0].objects is not a list → full failure sentinel, no crash."""
    _wrong_shape_case(tmp_path, {"return_code": 0, "sliced_plates": [{"objects": 5}]})


def test_orca_clean_result_json_is_success(tmp_path, monkeypatch):
    """A well-formed result.json with return_code 0 → ok=True (the happy
    path still works after the semantics fix)."""
    fake_bin = tmp_path / "fake-orca"
    fake_bin.write_text(
        "#!/bin/sh\n"
        'outdir=""\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--outputdir" ]; then outdir="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        'touch "$outdir/plate_1.gcode"\n'
        'echo \'{"return_code": 0, "error_string": "", "sliced_plates": [{"objects": [1]}]}\' > "$outdir/result.json"\n'
    )
    fake_bin.chmod(0o755)
    (tmp_path / "model.stl").write_text("stl solid x\nendsolid x\n")

    monkeypatch.setattr(
        slicer, "find_slicer", lambda kind: str(fake_bin) if kind == "orca" else None
    )

    result = slicer.slice_dry_run(str(tmp_path / "model.stl"))

    assert result.ok is True
    assert result.slicer == "orca"
    assert result.gcode_path is not None
    assert result.return_code == 0
    assert result.objects == 1
