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

from d33d import slicer
from d33d.slicer import SliceDryRunResult


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
