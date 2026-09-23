"""Fast-layer tests for Orca-family result.json success semantics (ticket #4, gate 6).

Pins the corrected contract in ``d33d/slicer.py``: when the Orca-family
(QIDI/Orca) branch produces G-code, ``result.json`` is the machine-readable
verdict. A missing or corrupt ``result.json`` (``json_rc == -1``) must be a
FAILURE, not a default pass — the same stricter semantics as the
PrusaSlicer path (process exit code only). The old behaviour
(``ok = json_rc == 0 if json_rc != -1 else True``) treated an unreadable
verdict as success, which is backwards.

Also covers ticket #238: profile path resolution inside the app bundle,
resolved-binary logging, discovery precedence, and the missing-profile
failure mode.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from d33d import slicer
from d33d.slicer import (
    SliceDryRunResult,
    _parse_orca_result_json,
    _resolve_orca_family_profiles,
)


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


# ---------------------------------------------------------------------------
# Ticket #238 — profile path resolution, logging, discovery precedence
# ---------------------------------------------------------------------------


def _make_fake_app_bundle(tmp_path: Path, app_name: str, include_profiles: bool = True) -> Path:
    """Create a minimal macOS .app bundle structure with an executable.

    Returns the path to the fake binary inside the bundle.
    """
    app_dir = tmp_path / f"{app_name}.app"
    bin_dir = app_dir / "Contents" / "MacOS"
    bin_dir.mkdir(parents=True)
    bin_path = bin_dir / app_name
    bin_path.write_text("#!/bin/sh\nexit 0\n")
    bin_path.chmod(0o755)

    if include_profiles:
        profiles_root = app_dir / "Contents" / "Resources" / "profiles"
        machine_dir = profiles_root / "X 5 Series" / "machine"
        process_dir = profiles_root / "X 5 Series" / "process"
        machine_dir.mkdir(parents=True)
        process_dir.mkdir(parents=True)
        (machine_dir / slicer.QIDI_XPLUS5_MACHINE_PRESET).write_text("{}")
        (process_dir / slicer.QIDI_XPLUS5_PROCESS_PRESET).write_text("{}")

    return bin_path


def test_resolve_profiles_in_app_bundle(tmp_path):
    """_resolve_orca_family_profiles finds both profile files inside a
    properly structured .app bundle and returns absolute paths."""
    bin_path = _make_fake_app_bundle(tmp_path, "FakeQIDI")
    machine, process = _resolve_orca_family_profiles(str(bin_path))
    assert machine is not None
    assert process is not None
    # Both paths exist on disk
    assert Path(machine).is_file()
    assert Path(process).is_file()
    # They are absolute paths
    assert Path(machine).is_absolute()
    assert Path(process).is_absolute()
    # They are inside the bundle
    assert str(tmp_path) in machine
    assert str(tmp_path) in process


def test_resolve_profiles_no_bundle_returns_none(tmp_path):
    """A binary not inside a .app bundle → both paths are None (bare-name
    fallback)."""
    bare_bin = tmp_path / "bare-slicer"
    bare_bin.write_text("#!/bin/sh\nexit 0\n")
    bare_bin.chmod(0o755)
    machine, process = _resolve_orca_family_profiles(str(bare_bin))
    assert machine is None
    assert process is None


def test_resolve_profiles_bundle_missing_profiles_returns_none(tmp_path):
    """A .app bundle that has no profile files → both None."""
    bin_path = _make_fake_app_bundle(tmp_path, "NoProfiles", include_profiles=False)
    machine, process = _resolve_orca_family_profiles(str(bin_path))
    assert machine is None
    assert process is None


def test_resolve_profiles_only_machine_missing(tmp_path):
    """Bundle has only the machine profile, missing process → process is None."""
    app_dir = tmp_path / "Partial.app"
    machine_dir = app_dir / "Contents" / "Resources" / "profiles" / "X 5 Series" / "machine"
    machine_dir.mkdir(parents=True)
    (machine_dir / slicer.QIDI_XPLUS5_MACHINE_PRESET).write_text("{}")
    # No process dir
    bin_dir = app_dir / "Contents" / "MacOS"
    bin_dir.mkdir(parents=True)
    bin_path = bin_dir / "Partial"
    bin_path.write_text("#!/bin/sh\nexit 0\n")
    bin_path.chmod(0o755)

    machine, process = _resolve_orca_family_profiles(str(bin_path))
    assert machine is not None
    assert process is None


def test_slice_orca_family_uses_resolved_paths_in_app_bundle(tmp_path, monkeypatch):
    """When the binary is in a .app bundle with profiles, --load-settings
    contains the resolved absolute paths (not bare names)."""
    # Create a fake bundle with a binary that dumps argv
    app_dir = tmp_path / "TestQIDI.app"
    bin_dir = app_dir / "Contents" / "MacOS"
    bin_dir.mkdir(parents=True)
    fake_bin = bin_dir / "TestQIDI"
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
        'for a in "$@"; do\n'
        '  printf "%s\\0" "$a"\n'
        'done > "${0}.argv"\n'
    )
    fake_bin.chmod(0o755)

    # Add profiles to the bundle
    profiles_root = app_dir / "Contents" / "Resources" / "profiles" / "X 5 Series"
    (profiles_root / "machine").mkdir(parents=True)
    (profiles_root / "process").mkdir(parents=True)
    (profiles_root / "machine" / slicer.QIDI_XPLUS5_MACHINE_PRESET).write_text("{}")
    (profiles_root / "process" / slicer.QIDI_XPLUS5_PROCESS_PRESET).write_text("{}")

    (tmp_path / "model.stl").write_text("stl solid x\nendsolid x\n")

    monkeypatch.setattr(
        slicer, "find_slicer", lambda kind: str(fake_bin) if kind == "qidi" else None
    )

    result = slicer.slice_dry_run(str(tmp_path / "model.stl"))
    assert result.ok is True

    argv_path = Path(str(fake_bin) + ".argv")
    argv = _read_nul_separated(argv_path)
    idx = argv.index("--load-settings")
    load_settings_value = argv[idx + 1]

    # The value must contain absolute paths (containing ".app"), not bare names
    assert ".app" in load_settings_value
    assert str(tmp_path) in load_settings_value
    # Both profiles are present (joined by ;)
    parts = load_settings_value.split(";")
    assert len(parts) == 2
    assert Path(parts[0]).is_file()
    assert Path(parts[1]).is_file()


def test_slice_orca_family_falls_back_to_bare_names_no_bundle(tmp_path, monkeypatch):
    """When the binary is NOT in a .app bundle, --load-settings uses the
    bare profile names (the #43 fallback)."""
    fake_bin = _fake_orca_family_bin(tmp_path, "bare-slicer")
    (tmp_path / "model.stl").write_text("stl solid x\nendsolid x\n")

    monkeypatch.setattr(
        slicer, "find_slicer", lambda kind: str(fake_bin) if kind == "qidi" else None
    )

    result = slicer.slice_dry_run(str(tmp_path / "model.stl"))
    assert result.ok is True

    argv_path = Path(str(fake_bin) + ".argv")
    argv = _read_nul_separated(argv_path)
    idx = argv.index("--load-settings")
    load_settings_value = argv[idx + 1]

    # Should be the bare names (the _LOAD_SETTINGS_VALUE fallback)
    assert load_settings_value == slicer._LOAD_SETTINGS_VALUE


def test_missing_profile_failure_mode(tmp_path, monkeypatch):
    """A fake binary that exits non-zero and writes neither gcode nor
    result.json (the 'can not find setting file' failure mode from the
    502 bug) → ok=False, slicer reports the error."""
    fake_bin = tmp_path / "failing-slicer"
    fake_bin.write_text(
        "#!/bin/sh\n"
        'echo "can not find setting file: Qidi X-Plus 5 0.4 nozzle.json" >&2\n'
        "exit 253\n"
    )
    fake_bin.chmod(0o755)
    (tmp_path / "model.stl").write_text("stl solid x\nendsolid x\n")

    monkeypatch.setattr(
        slicer, "find_slicer", lambda kind: str(fake_bin) if kind == "qidi" else None
    )

    result = slicer.slice_dry_run(str(tmp_path / "model.stl"))
    assert result.ok is False
    assert result.slicer == "qidi"
    assert "can not find setting file" in result.error_string


def test_discovery_env_pin_wins_over_path(tmp_path, monkeypatch):
    """An env var pin pointing at a valid file wins over PATH/app-dir
    discovery. A pin to a non-existent file is silently ignored."""
    # Create a real file to pin to
    pinned_bin = tmp_path / "my-qidi"
    pinned_bin.write_text("#!/bin/sh\nexit 0\n")
    pinned_bin.chmod(0o755)

    monkeypatch.setenv("QIDI_SLICER_BIN", str(pinned_bin))
    found = slicer.find_slicer("qidi")
    assert found == str(pinned_bin)

    # Now point the pin at a non-existent file → falls through to other search
    monkeypatch.setenv("QIDI_SLICER_BIN", str(tmp_path / "does-not-exist"))
    # Also remove from PATH to ensure no fallback finds it
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    # The _APP_BIN_DIRS check might still find it on a Mac, so we only
    # verify it doesn't return the non-existent pin path
    found = slicer.find_slicer("qidi")
    if found is not None:
        assert found != str(tmp_path / "does-not-exist")


def test_slice_dry_run_logs_resolved_binary(tmp_path, monkeypatch, caplog):
    """slice_dry_run emits an INFO log line naming the resolved binary path."""
    fake_bin = _fake_orca_family_bin(tmp_path, "log-slicer")
    (tmp_path / "model.stl").write_text("stl solid x\nendsolid x\n")

    monkeypatch.setattr(
        slicer, "find_slicer", lambda kind: str(fake_bin) if kind == "qidi" else None
    )

    with caplog.at_level("INFO", logger="d33d.slicer"):
        slicer.slice_dry_run(str(tmp_path / "model.stl"))

    # The resolved binary path must appear in the log
    assert any(str(fake_bin) in r.message for r in caplog.records), (
        f"Expected binary path in log, got: {[r.message for r in caplog.records]}"
    )
    # The --load-settings value must also be logged
    all_messages = "\n".join(r.message for r in caplog.records)
    assert "--load-settings" in all_messages


def test_slice_dry_run_logs_orca_binary(tmp_path, monkeypatch, caplog):
    """The orca branch also logs its resolved binary."""
    fake_bin = _fake_orca_family_bin(tmp_path, "log-orca")
    (tmp_path / "model.stl").write_text("stl solid x\nendsolid x\n")

    monkeypatch.setattr(
        slicer, "find_slicer", lambda kind: str(fake_bin) if kind == "orca" else None
    )

    with caplog.at_level("INFO", logger="d33d.slicer"):
        slicer.slice_dry_run(str(tmp_path / "model.stl"))

    assert any(str(fake_bin) in r.message for r in caplog.records)

