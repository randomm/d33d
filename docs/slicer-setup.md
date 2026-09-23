# Slicer setup for 3MF export (gate 6)

The slice dry run (gate 6 of 7 in `d33d/print_validation.py`) invokes an
Orca-family slicer binary headlessly to prove the mesh slices cleanly for
the QIDI X-Plus 5 (320×320×300 mm, 0.4 mm nozzle). This page is the single
place that documents which binary is required, where its profiles must
live, and how to pin the binary path.

## Documented happy path: QIDIStudio

**Binary:** `/Applications/QIDIStudio.app/Contents/MacOS/QIDIStudio`

No environment variable is required — the binary is found via the
app-bundle discovery path (`/Applications/QIDIStudio.app/Contents/MacOS`)
which is first in the driver's search order. If you install QIDIStudio to
a non-standard location, set:

```
export QIDI_SLICER_BIN=/path/to/QIDIStudio
```

**Profile files** (resolved to absolute paths inside the app bundle at
slice time — ticket #238):

| Preset | File | Location in bundle |
|--------|------|--------------------|
| Machine | `Qidi X-Plus 5 0.4 nozzle.json` | `Contents/Resources/profiles/X 5 Series/machine/` |
| Process | `0.20mm Standard @X-Plus 5.json` | `Contents/Resources/profiles/X 5 Series/process/` |

The driver walks the bundle's `Contents/Resources/profiles/` tree and
passes the resolved absolute paths to `--load-settings`. If either file is
missing (e.g. a different QIDI product line is installed), the driver
falls back to bare profile names and the slicer reports its own
"can not find setting file" error — the 502 response will name the
missing profile.

## Alternative: OrcaSlicer

**Binary:** `/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer`

OrcaSlicer is used only when QIDIStudio is not available (it is second in
discovery precedence). For it to slice the QIDI X-Plus 5, the same two
profile files must be resolvable inside its bundle:

```
Contents/Resources/profiles/Qidi/machine/Qidi X-Plus 5 0.4 nozzle.json
Contents/Resources/profiles/Qidi/process/0.20mm Standard @X-Plus 5.json
```

The stock OrcaSlicer bundle does **not** ship these files (its `profiles/Qidi/`
directory contains only cover images and buildplate textures). To use
OrcaSlicer, copy the two files from the QIDIStudio bundle (or from
`qidi-plus5/upstream-orca/machine/` and the QIDIStudio `process/` directory)
into the OrcaSlicer bundle's `profiles/Qidi/` tree, creating the `machine/`
and `process/` subdirectories.

## Environment variable pins

| Variable | Purpose |
|----------|---------|
| `QIDI_SLICER_BIN` | Pin the QIDI Studio binary (overrides PATH and app-dir search) |
| `ORCA_SLICER_BIN` | Pin the OrcaSlicer binary |
| `PRUSA_SLICER_BIN` | Pin the PrusaSlicer fallback binary |

A pin pointing at a non-existent file is silently ignored (the driver
falls through to PATH / app-dir search). The log line emitted at slice time
names the binary that was actually resolved.

## Readiness check

From the repo root, run:

```bash
python3 -c "from d33d.slicer import available_slicers, slice_dry_run; \
  print(available_slicers()); \
  r = slice_dry_run('tests/fixtures/stl/box_20mm.stl'); \
  print(r.ok, r.slicer, r.error_string)"
```

Expected on a correctly set-up Mac:

```
{'qidi': '/Applications/QIDIStudio.app/Contents/MacOS/QIDIStudio', 'orca': '/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer'}
True qidi
```

## Logging

The driver logs one INFO line per `slice_dry_run` call via
`logging.getLogger("d33d.slicer")`:

```
slice_dry_run: using qidi binary at /Applications/QIDIStudio.app/Contents/MacOS/QIDIStudio
slice_orca_family: binary=... argv=['...', '--load-settings', '/abs/path/machine.json;/abs/path/process.json']
```

When running `python -m d33d.main`, Python's logging default (WARNING
level on the root logger) suppresses INFO lines. To see them, pass
`--log-level info` to uvicorn (e.g. run the module under `uvicorn d33d.main:app
--log-level info`) or add a `logging.basicConfig(level=logging.INFO)` call
before the server starts. The FastAPI app (`d33d/app.py`) already
configures its own logger; the slicer logger is independent and follows
the root logger's level.
