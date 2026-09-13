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
import subprocess
from pathlib import Path

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


def test_valid_rc_plates_not_list_surfaces_real_verdict(tmp_path):
    """(6) valid int return_code + malformed sliced_plates → real rc and
    error_string are preserved, objects degrades to None. The slicer's own
    diagnostic is no longer discarded for a generic shape message."""
    (tmp_path / "result.json").write_text(
        json.dumps({"return_code": -50, "error_string": "over envelope"})
    )
    rc, err, objects = _parse_orca_result_json(tmp_path)
    assert rc == -50
    assert err == "over envelope"
    assert objects is None


def test_valid_rc_plate_not_dict_surfaces_real_verdict(tmp_path):
    """(6) valid return_code + a non-dict plate → real rc preserved."""
    (tmp_path / "result.json").write_text(
        json.dumps({"return_code": -50, "error_string": "boom", "sliced_plates": ["x"]})
    )
    rc, err, objects = _parse_orca_result_json(tmp_path)
    assert rc == -50
    assert err == "boom"
    assert objects is None


def test_valid_rc_objects_not_list_surfaces_real_verdict(tmp_path):
    """(6) valid return_code + objects is a scalar → real rc preserved,
    objects is None (a scalar is not a JSON array)."""
    (tmp_path / "result.json").write_text(
        json.dumps({"return_code": -50, "error_string": "err", "sliced_plates": [{"objects": 5}]})
    )
    rc, err, objects = _parse_orca_result_json(tmp_path)
    assert rc == -50
    assert err == "err"
    assert objects is None


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


# ---------------------------------------------------------------------------
# Ticket #43 — QIDI X-Plus 5 machine preset pinning
# ---------------------------------------------------------------------------


def _fake_orca_family_bin(tmp_path, name: str) -> Path:
    """Create a fake Orca-family binary that dumps argv to a file and
    writes a minimal success result.json + plate_1.gcode."""
    fake_bin = tmp_path / name
    fake_bin.write_text(
        "#!/bin/sh\n"
        'prev=""\n'
        'outdir=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--outputdir" ]; then outdir="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        'touch "$outdir/plate_1.gcode"\n'
        'echo \'{"return_code": 0, "error_string": "", "sliced_plates": [{"objects": [1]}]}\' > "$outdir/result.json"\n'
        '# Dump full argv (NUL-separated for safe parsing) to $0.argv\n'
        'for a in "$@"; do\n'
        '  printf "%s\\0" "$a"\n'
        'done > "${0}.argv"\n'
    )
    fake_bin.chmod(0o755)
    return fake_bin


def _read_nul_separated(path: Path) -> list[str]:
    """Read a NUL-separated file into a list of strings."""
    data = path.read_bytes()
    parts = data.split(b"\x00")
    # Drop trailing empty element from the final NUL
    if parts and parts[-1] == b"":
        parts.pop()
    return [p.decode() for p in parts]


def test_qidi_branch_includes_load_settings_xplus5(tmp_path, monkeypatch):
    """The QIDI branch of slice_dry_run passes --load-settings with both
    X-Plus 5 preset names as a single ;-joined argv element (ticket #43).
    Drives the real slice_orca_family via the qidi branch."""
    fake_bin = _fake_orca_family_bin(tmp_path, "fake-qidi")
    (tmp_path / "model.stl").write_text("stl solid x\nendsolid x\n")

    monkeypatch.setattr(
        slicer, "find_slicer", lambda kind: str(fake_bin) if kind == "qidi" else None
    )

    result = slicer.slice_dry_run(str(tmp_path / "model.stl"))

    # Success path — the fake binary wrote a good result.json
    assert result.ok is True
    assert result.slicer == "qidi"

    # The argv dump is written next to the model file
    argv_path = Path(str(fake_bin) + ".argv")
    assert argv_path.is_file(), f"expected argv dump at {argv_path}"
    argv = _read_nul_separated(argv_path)

    # --load-settings must be present
    assert "--load-settings" in argv
    idx = argv.index("--load-settings")
    assert idx + 1 < len(argv)

    # The value is the single ;-joined pair of bare profile names
    expected_value = f"{slicer.QIDI_XPLUS5_MACHINE_PRESET};{slicer.QIDI_XPLUS5_PROCESS_PRESET}"
    assert argv[idx + 1] == expected_value

    # Assert the exact expected literal value (guards against drift)
    assert argv[idx + 1] == slicer._LOAD_SETTINGS_VALUE


def test_orca_branch_includes_load_settings_xplus5(tmp_path, monkeypatch):
    """The OrcaSlicer branch of slice_dry_run passes --load-settings with
    both X-Plus 5 preset names as a single ;-joined argv element (ticket #43).
    Drives the real slice_orca_family via the orca fallback branch."""
    fake_bin = _fake_orca_family_bin(tmp_path, "fake-orca")
    (tmp_path / "model.stl").write_text("stl solid x\nendsolid x\n")

    monkeypatch.setattr(
        slicer, "find_slicer", lambda kind: str(fake_bin) if kind == "orca" else None
    )

    result = slicer.slice_dry_run(str(tmp_path / "model.stl"))

    assert result.ok is True
    assert result.slicer == "orca"

    argv_path = Path(str(fake_bin) + ".argv")
    assert argv_path.is_file(), f"expected argv dump at {argv_path}"
    argv = _read_nul_separated(argv_path)

    assert "--load-settings" in argv
    idx = argv.index("--load-settings")
    assert idx + 1 < len(argv)

    expected_value = f"{slicer.QIDI_XPLUS5_MACHINE_PRESET};{slicer.QIDI_XPLUS5_PROCESS_PRESET}"
    assert argv[idx + 1] == expected_value
    assert argv[idx + 1] == slicer._LOAD_SETTINGS_VALUE


def test_preset_constants_defined_and_referenced():
    """The machine preset constants are defined at module level in
    d33d/slicer.py and are referenced by the dry-run path (ticket #43)."""
    # Constants exist with the expected values
    assert slicer.QIDI_XPLUS5_MACHINE_PRESET == "Qidi X-Plus 5 0.4 nozzle.json"
    assert slicer.QIDI_XPLUS5_PROCESS_PRESET == "0.20mm Standard @X-Plus 5.json"

    # The --load-settings value is the ;-joined pair (single argv element)
    expected = slicer._LOAD_SETTINGS_VALUE
    assert f"{slicer.QIDI_XPLUS5_MACHINE_PRESET};{slicer.QIDI_XPLUS5_PROCESS_PRESET}" == expected

    # The _LOAD_SETTINGS_VALUE (used by slice_orca_family) matches
    assert slicer._LOAD_SETTINGS_VALUE == expected


def test_prusa_branch_has_no_load_settings(tmp_path, monkeypatch):
    """The PrusaSlicer fallback branch builds its argv [bin, model,
    -export-slicedata, dir] with NO --load-settings flag and no crash
    (ticket #43). Uses monkeypatched subprocess.run to capture the argv."""
    fake_prusa = str(tmp_path / "fake-prusa")
    (tmp_path / "model.stl").write_text("stl solid x\nendsolid x\n")

    captured_argv: list[str] | None = None

    def fake_run(cmd, **kwargs):
        nonlocal captured_argv
        captured_argv = cmd
        # Simulate a successful prusa run: write a gcode file to -export-slicedata dir
        # Find the -export-slicedata dir from cmd
        idx = cmd.index("-export-slicedata")
        outdir = Path(cmd[idx + 1])
        outdir.mkdir(parents=True, exist_ok=True)
        gcode_path = outdir / "object_1.gcode"
        gcode_path.write_text("G1 X0 Y0 Z0\nG1 X1 Y0 Z0\n")
        # Return a mock completed process
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(slicer.subprocess, "run", fake_run)
    monkeypatch.setattr(
        slicer, "find_slicer", lambda kind: fake_prusa if kind == "prusa" else None
    )

    result = slicer.slice_dry_run(str(tmp_path / "model.stl"))

    # The prusa branch should succeed (gcode produced, rc=0)
    assert result.ok is True
    assert result.slicer == "prusa"

    # Assert the captured argv
    assert captured_argv is not None
    assert "--load-settings" not in captured_argv, (
        f"Prusa branch must NOT include --load-settings, got: {captured_argv}"
    )

    # The prusa argv is [bin, model, -export-slicedata, dir]
    assert captured_argv[0] == fake_prusa
    assert captured_argv[1] == str(tmp_path / "model.stl")
    assert captured_argv[2] == "-export-slicedata"
    # 4th element is the output dir
    assert len(captured_argv) == 4
