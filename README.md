# d33d

Parametric 3D design loop: photo + chat + stated dimensions in, print-ready
3MF out. OpenSCAD-driven (parametric, inspectable, patchable) rather than
diffusion-based.

## Spec of record

The full ticket backlog lives in [`docs/backlog/`](docs/backlog/) —
`README.md` plus `00-bootstrap.md` through `08-evals.md`. Each later ticket
builds on this shell.

> **Note:** `docs/backlog/` is the spec of record. The research notes under
> [`docs/archive/parallel-research/`](docs/archive/parallel-research/)
> document an **abandoned** diffusion-on-Apple-Silicon direction and are
> NOT the spec of record.

## Layout

- `d33d/` — source (shell package; application code lands in later tickets)
- `tests/` — tests (unmarked tests run in the fast layer; `@pytest.mark.slow`
  tests need Docker and run only on the linux/amd64 target box)
- `tests/security/` — containment suite (filled in by ticket #1)
- `docs/backlog/` — spec of record
- `docs/archive/parallel-research/` — archived research (abandoned direction)

## Quick start

```bash
pip install -e .
pytest -m "not slow"
```

The `slow` marker is declared in `pyproject.toml` ([`[tool.pytest.ini_options]`]);
Docker-dependent tests use `@pytest.mark.slow`. CI runs only the fast layer
(`.github/workflows/ci.yml`); the slow layer runs on the target box.
