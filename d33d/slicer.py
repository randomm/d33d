"""Headless slice dry-run driver for the QIDI Plus 5 (ticket #4, gate 6).

The slice dry run is gate 6 of the seven-gate order in
``d33d/print_validation.py`` — its own gate, never folded into the
watertight check.  This module is the spike the spec calls out: QIDI
Studio is an OrcaSlicer fork, so the driver is written against the
documented OrcaSlicer CLI flag surface (long options, positional input
files) and verified against the real binaries.

Verified against the real binaries (2026-09-11):

* QIDIStudio 02.07.02.60 (``/Applications/QIDIStudio.app``) — **drivable
  headlessly** despite the ticket's "no official headless CLI" note:
  ``QIDIStudio <file.stl|file.3mf> --slice 0 --no-check --outputdir <dir>``
  produces ``plate_1.gcode`` plus a machine-readable ``result.json``
  (``return_code``, ``error_string``, per-plate ``objects``,
  ``triangle_count``).  Over-envelope input fails loudly:
  ``return_code -50``.
* OrcaSlicer 2.5.0-dev (``/Applications/OrcaSlicer.app``) — same flag
  surface, same output convention, usable as an intermediate fallback.

Explicit fallback (spec: "don't let this block the ticket"): if no
QIDI/Orca binary is on PATH, the gate falls back to **PrusaSlicer CLI**
(``prusa-slicer <input.stl> -export-slicedata <dir>``) as the printability
proxy and the operator does the production slice in the GUI.  Binary
paths may be overridden via the env vars ``QIDI_SLICER_BIN``,
``ORCA_SLICER_BIN`` and ``PRUSA_SLICER_BIN`` so the same driver runs on
the linux/amd64 box without source changes.

This module NEVER vendors or builds a slicer binary; it only locates,
invokes, and interprets one.

Success contract (pinned): for the Orca-family (QIDI / Orca) branch,
``result.json``'s ``return_code`` is the SOLE authority for the success
verdict. The process exit code is NOT co-equal — it is recorded for
diagnostics only. Only ``return_code == 0`` in a well-formed ``result.json``
marks the slice usable; a missing, corrupt, or ``return_code != 0``
``result.json`` is a failure (fail-closed). For the PrusaSlicer fallback
there is no ``result.json``, so the process exit code IS the success
signal.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------

#: Default PATH names probed for each slicer (in precedence order).
#: QIDI Studio is the target; OrcaSlicer is the same binary family (QIDI
#: Studio is an OrcaSlicer fork) and the intermediate fallback; PrusaSlicer
#: is the explicit spec-mandated final fallback.
_SLICER_SEARCH_NAMES: dict[str, tuple[str, ...]] = {
    "qidi": ("QIDIStudio", "qidi-studio", "qidistudio"),
    "orca": ("OrcaSlicer", "orca-slicer", "orcaslicer"),
    "prusa": ("PrusaSlicer", "prusa-slicer", "prusaslicer"),
}

#: Extra PATH entries for installed app bundles (macOS; on the linux/amd64
#: box these are simply absent).
_APP_BIN_DIRS: tuple[str, ...] = (
    "/Applications/QIDIStudio.app/Contents/MacOS",
    "/Applications/OrcaSlicer.app/Contents/MacOS",
    "/Applications/PrusaSlicer.app/Contents/MacOS",
)

#: Environment variable names that pin a specific binary path, overriding
#: PATH discovery entirely (used on the amd64 box to point at
#: source-built binaries without renaming them to the macOS app names).
_BIN_ENV: dict[str, str] = {
    "qidi": "QIDI_SLICER_BIN",
    "orca": "ORCA_SLICER_BIN",
    "prusa": "PRUSA_SLICER_BIN",
}


def _candidate_paths(name: str, path_names: tuple[str, ...]) -> list[str]:
    """All plausible filesystem paths for one slicer name, no ordering."""
    paths: list[str] = []
    for directory in _APP_BIN_DIRS:
        for pname in path_names:
            paths.append(os.path.join(directory, pname))
    return paths


def find_slicer(kind: Literal["qidi", "orca", "prusa"]) -> str | None:
    """Locate a slicer binary of ``kind`` or return None.

    Order: env-var pin (``QIDI_SLICER_BIN`` / ``ORCA_SLICER_BIN`` /
    ``PRUSA_SLICER_BIN``) → PATH lookup → known app-bundle locations.
    """
    env_var = _BIN_ENV[kind]
    pinned = os.environ.get(env_var)
    if pinned and Path(pinned).is_file():
        return pinned
    for pname in _SLICER_SEARCH_NAMES[kind]:
        found = shutil.which(pname)
        if found:
            return found
    for path in _candidate_paths(kind, _SLICER_SEARCH_NAMES[kind]):
        if Path(path).is_file():
            return path
    return None


def available_slicers() -> dict[str, str]:
    """Map of kind → binary path for every slicer found, in precedence
    order (qidi, orca, prusa). Missing kinds are omitted."""
    found: dict[str, str] = {}
    for kind in ("qidi", "orca", "prusa"):
        path = find_slicer(kind)
        if path:
            found[kind] = path
    return found


# ---------------------------------------------------------------------------
# Result contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SliceDryRunResult:
    """Outcome of a headless slice dry run (gate 6).

    Attributes:
        ok: True iff the slicer produced a usable slice result.
        slicer: Which binary was driven (``"qidi"`` / ``"orca"`` /
            ``"prusa"``).
        gcode_path: Path to the produced G-code, or None.
        gcode_lines: Line count of the produced G-code (0 if none).
        return_code: Slicer-reported return code where available
            (result.json ``return_code``), else the process exit code.
        error_string: Slicer-reported error string where available,
            else the trailing stderr, else "" on success.
        objects: Number of objects the slicer confirmed it placed, or
            None where the fallback cannot report it.
        detail: Human-readable diagnostic for the gate report.
    """

    ok: bool
    slicer: str
    gcode_path: str | None
    gcode_lines: int
    return_code: int
    error_string: str
    objects: int | None
    detail: str


def _fail(kind: str, detail: str, error_string: str = "") -> SliceDryRunResult:
    return SliceDryRunResult(
        ok=False,
        slicer=kind,
        gcode_path=None,
        gcode_lines=0,
        return_code=-1,
        error_string=error_string,
        objects=None,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Orca-family (QIDI / Orca) headless slicing
# ---------------------------------------------------------------------------

_RESULT_JSON_NAME = "result.json"
_GCODE_RE = re.compile(r"plate_\d+\.gcode$")

# QIDI X-Plus 5 machine preset constants (ticket #43).
# These are bare profile names that each Orca-family slicer (QIDI Studio,
# OrcaSlicer) resolves from its own bundled profiles directory. The driver
# does NOT resolve paths or fail-closed on absence — the slicer reports
# its own error if the profile is missing.
QIDI_XPLUS5_MACHINE_PRESET = "Qidi X-Plus 5 0.4 nozzle.json"
QIDI_XPLUS5_PROCESS_PRESET = "0.20mm Standard @X-Plus 5.json"

# The --load-settings flag value: both presets joined by ";" as a single
# argv element. Each slicer parses the pair and applies the machine + process
# profile to the dry-run slice.
_LOAD_SETTINGS_VALUE = (
    f"{QIDI_XPLUS5_MACHINE_PRESET};{QIDI_XPLUS5_PROCESS_PRESET}"
)


def _parse_orca_result_json(outdir: Path) -> tuple[int, str, int | None]:
    """Read the slicer's result.json → (return_code, error_string, objects).

    QIDI Studio (and the Orca family in general) writes a ``result.json``
    next to the G-code with ``return_code``, ``error_string`` and
    per-plate ``objects``.  Absent or malformed → unknown (−1).
    """
    result_file = outdir / _RESULT_JSON_NAME
    if not result_file.is_file():
        return -1, "slicer wrote no result.json", None
    try:
        data = json.loads(result_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return -1, f"result.json unreadable: {e}", None
    if not isinstance(data, dict):
        return -1, "result.json is not a JSON object", None
    raw_rc = data.get("return_code", -1)
    # return_code and sliced_plates are validated INDEPENDENTLY: a valid
    # integer return_code with a malformed sliced_plates must still surface
    # the slicer's real verdict and error_string (item 6) rather than being
    # discarded for a generic shape-violation message. The objects count
    # degrades to None when sliced_plates is malformed, but the return code
    # and error text are preserved.
    plates = data.get("sliced_plates")
    objects = None
    if isinstance(plates, list):
        for plate in plates:
            if not isinstance(plate, dict):
                objects = None
                break
            plate_objects = plate.get("objects")
            if not isinstance(plate_objects, list):
                objects = None
                break
            if plate_objects:
                objects = (objects or 0) + len(plate_objects)
    if isinstance(raw_rc, int):
        return raw_rc, str(data.get("error_string", "")), objects
    return -1, "result.json has an unexpected shape", None


def _first_gcode(outdir: Path) -> Path | None:
    """First plate N.gcode the slicer wrote into outdir, else None."""
    for path in sorted(outdir.iterdir()):
        if path.is_file() and _GCODE_RE.search(path.name):
            return path
    return None


def _count_gcode_lines(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def slice_orca_family(
    binary: str,
    model_path: str,
    output_dir: str | None = None,
    timeout_s: int = 300,
) -> tuple[int, str, Path | None]:
    """Drive an Orca-family binary headlessly.

    The QIDI X-Plus 5 machine profile is pinned via ``--load-settings`` so
    the slice is validated against the correct machine preset (0.4mm
    nozzle, 320×320×300mm envelope).  Both the QIDI Studio and OrcaSlicer
    branches use this builder (ticket #43).

    Returns (process_exit_code, stderr_tail, gcode_path_or_None).  On
    success the gcode path is non-None.
    """
    outdir = _prepare_outdir(output_dir, "d33d_slice_")
    cmd = [
        binary,
        str(model_path),
        "--slice",
        "0",
        "--no-check",
        "--outputdir",
        str(outdir),
        "--load-settings",
        _LOAD_SETTINGS_VALUE,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -99, f"slice timed out after {timeout_s}s", None
    gcode = _first_gcode(outdir)
    stderr_tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
    return proc.returncode, stderr_tail, gcode


def _prepare_outdir(output_dir: str | None, prefix: str) -> Path:
    """Resolve the output directory, creating it if needed."""
    outdir = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(prefix=prefix))
    outdir.mkdir(parents=True, exist_ok=True)
    return outdir


# ---------------------------------------------------------------------------
# PrusaSlicer fallback (spec-mandated printability proxy)
# ---------------------------------------------------------------------------


def slice_prusa(
    binary: str,
    model_path: str,
    output_dir: str | None = None,
    timeout_s: int = 300,
) -> tuple[int, str, Path | None]:
    """Drive the PrusaSlicer CLI as the explicit fallback proxy.

    ``prusa-slicer <input.stl> -export-slicedata <dir>`` performs a
    prepare+slice without a GUI. The export directory contains per-object
    ``*.gcode`` files on success.
    """
    outdir = _prepare_outdir(output_dir, "d33d_prusa_")
    cmd = [binary, str(model_path), "-export-slicedata", str(outdir)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -99, f"slice timed out after {timeout_s}s", None
    for path in sorted(outdir.iterdir()):
        if path.is_file() and path.suffix.lower() in (".gcode", ".g"):
            return proc.returncode, (proc.stderr or "").strip()[-2000:], path
    return proc.returncode, (proc.stderr or proc.stdout or "").strip()[-2000:], None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def slice_dry_run(
    model_path: str,
    output_dir: str | None = None,
    timeout_s: int = 300,
) -> SliceDryRunResult:
    """Run gate 6 — the headless slice dry run — on a mesh file.

    Precedence: QIDI Studio → OrcaSlicer → PrusaSlicer (explicit spec
    fallback).  The file may be an STL or a 3MF; on 3MF input the
    QIDI/Orca branch is used so the emitted 3MF is proven to load and
    slice in the target slicer (acceptance: "a 3MF in mm that loads in
    QIDI Studio and slices to non-empty G-code").

    Returns a :class:`SliceDryRunResult` — the ``ok`` flag and
    ``error_string`` are what the gate reports (its own diagnosable
    class, never folded into "not watertight").
    """
    model = Path(model_path)
    if not model.is_file():
        return _fail(
            "none",
            f"input model does not exist: {model}",
            error_string="missing input",
        )

    # QIDI Studio first (target printer).
    qidi_bin = find_slicer("qidi")
    if qidi_bin:
        process_rc, stderr_tail, gcode = slice_orca_family(
            qidi_bin, model, output_dir, timeout_s
        )
        outdir = Path(output_dir) if output_dir else (gcode.parent if gcode else Path())
        if gcode is not None:
            # result.json is the Orca-family's machine-readable verdict and
            # is the SOLE authority for success here: result.json's
            # return_code decides, the process exit code does NOT.
            # (documented contract — see module docstring). Missing or
            # corrupt result.json (json_rc == -1) is a FAILURE, not a
            # default pass: G-code alone proves the binary ran, but the
            # plate may be empty (over-envelope, unsupported) and only
            # result.json says whether the slice is usable.
            json_rc, json_err, objects = _parse_orca_result_json(outdir)
            ok = json_rc == 0
            gcode_lines = _count_gcode_lines(gcode)
            return SliceDryRunResult(
                ok=ok,
                slicer="qidi",
                gcode_path=str(gcode),
                gcode_lines=gcode_lines,
                return_code=json_rc,
                error_string=json_err if not ok else "",
                objects=objects,
                detail=(
                    f"QIDI Studio produced {gcode.name} ({gcode_lines} lines)"
                    + (f" (process exit {process_rc})" if process_rc else "")
                ),
            )
        json_rc, json_err, _ = (
            _parse_orca_result_json(outdir) if outdir else (-1, "", None)
        )
        return _fail(
            "qidi",
            f"QIDI Studio produced no G-code (exit {process_rc}): {stderr_tail or json_err}",
            error_string=stderr_tail or json_err,
        )

    # OrcaSlicer (same family; intermediate fallback).
    orca_bin = find_slicer("orca")
    if orca_bin:
        process_rc, stderr_tail, gcode = slice_orca_family(
            orca_bin, model, output_dir, timeout_s
        )
        outdir = Path(output_dir) if output_dir else (gcode.parent if gcode else Path())
        if gcode is not None:
            # result.json's return_code is the sole authority for success
            # (process exit code is not co-equal). Missing/corrupt
            # result.json (json_rc == -1) is a failure, not a default pass.
            json_rc, json_err, objects = (
                _parse_orca_result_json(outdir) if outdir else (-1, "", None)
            )
            ok = json_rc == 0
            gcode_lines = _count_gcode_lines(gcode)
            return SliceDryRunResult(
                ok=ok,
                slicer="orca",
                gcode_path=str(gcode),
                gcode_lines=gcode_lines,
                return_code=json_rc,
                error_string=json_err if not ok else "",
                objects=objects,
                detail=(
                    f"OrcaSlicer produced {gcode.name} ({gcode_lines} lines)"
                    + (f" (process exit {process_rc})" if process_rc else "")
                ),
            )
        json_rc, json_err, _ = (
            _parse_orca_result_json(outdir) if outdir else (-1, "", None)
        )
        return _fail(
            "orca",
            f"OrcaSlicer produced no G-code (exit {process_rc}): {stderr_tail or json_err}",
            error_string=stderr_tail or json_err,
        )

    # PrusaSlicer — the explicit spec fallback.
    prusa_bin = find_slicer("prusa")
    if prusa_bin:
        rc, stderr_tail, gcode = slice_prusa(prusa_bin, model, output_dir, timeout_s)
        if gcode is not None:
            gcode_lines = _count_gcode_lines(gcode)
            return SliceDryRunResult(
                ok=rc == 0,
                slicer="prusa",
                gcode_path=str(gcode),
                gcode_lines=gcode_lines,
                return_code=rc,
                error_string="" if rc == 0 else stderr_tail,
                objects=None,
                detail=(
                    f"PrusaSlicer fallback produced {gcode.name} "
                    f"({gcode_lines} lines); production slice is done in the GUI"
                ),
            )
        return _fail(
            "prusa",
            f"PrusaSlicer produced no G-code (exit {rc}): {stderr_tail}",
            error_string=stderr_tail,
        )

    return _fail(
        "none",
        "no slicer binary found (looked for QIDIStudio, OrcaSlicer, PrusaSlicer; "
        "set QIDI_SLICER_BIN / ORCA_SLICER_BIN / PRUSA_SLICER_BIN to pin one)",
        error_string="no slicer available",
    )
