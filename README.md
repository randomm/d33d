# d33d

A 3D design tool: give it a reference photo and dimensions, get back printable
OpenSCAD you can inspect, edit by region and export as 3MF for a QIDI printer.

## Spec of record

The project spec lives in the GitHub issues, not in this repo. Each issue
below is one deliverable; the dependency order is the build order:

| Issue | Deliverable |
| --- | --- |
| [#1](https://github.com/randomm/d33d/issues/1) | Repo bootstrap: Python package skeleton, pytest `slow` marker, CI |
| [#2](https://github.com/randomm/d33d/issues/2) | Sandboxed OpenSCAD render worker (Docker, STL + CSG + six views) |
| [#3](https://github.com/randomm/d33d/issues/3) | FastAPI backend and file-driven model config |
| [#4](https://github.com/randomm/d33d/issues/4) | 3MF export and printability gating for QIDI |
| [#5](https://github.com/randomm/d33d/issues/5) | Design agent: photo + chat to OpenSCAD with vision critique |
| [#6](https://github.com/randomm/d33d/issues/6) | React single-page app |
| [#7](https://github.com/randomm/d33d/issues/7) | Region selection (single-point pick) |
| [#8](https://github.com/randomm/d33d/issues/8) | Version timeline, restore, variant gallery, compare |
| [#9](https://github.com/randomm/d33d/issues/9) | Eval harness and golden set |

## Status

The project is built and running. 3MF export requires a slicer binary —
see [Slicer setup](docs/slicer-setup.md) for the binary, profile files,
and env-var pins.
