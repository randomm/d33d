# AGENTS.md

## Operator choices

- **Review-blocking severity:** Only HIGH/CRITICAL blocks merge
- **Merge authority:** Agents can merge
- **Project-specific constraints:** omit


# Minimalist Engineering

Every line of code is a liability. Before creating anything:

- **Is this explicitly required** by the GitHub issue?
- **Can existing code/tools** solve this instead?
- **What's the SIMPLEST** way to meet the requirement?
- **Am I building for hypothetical** future needs?

If you cannot justify necessity, DO NOT CREATE IT.

- **Keep source files under 500 lines.** A file past that is almost always doing more than one thing — it is the point where a reader stops holding it in their head. Prompt to split by responsibility, not a hard gate.
- **Existing files:** 41 files were over 500 lines at commit b68fb20 (2026-02-26): 14 production (d33d/, web/src/, scripts/), 27 test. No retrofit required. A file being edited should be split when the change would push it further over; never create a new file over the limit.

# Git Workflow

## Conventional commits

```
<type>(<scope>): <description>
```

Types: `feat` | `fix` | `refactor` | `docs` | `test` | `chore`

## Branch naming

```
feature/issue-{N}-brief-description
```

## Branch protection

- ❌ NO direct commits to `main`
- ✅ All work on feature branches → PR
- ✅ PRs squash-merged

# Documentation Policy

## The 200-PR test

Before adding documentation: *"Will this be true in 200 PRs?"*

- **YES** (enduring principle) → Document the principle (WHY)
- **NO** (implementation detail) → Skip, or use code comments (WHAT/HOW)

## Forbidden documentation

- ❌ Issue drafts, implementation summaries, fix notes, scratch files
- ❌ `TODO` comments — create GitHub issues instead

# Issue-Driven Development

## Before starting

1. GitHub issue exists for the work
2. Issue clearly describes the requirement
3. Your approach matches issue scope exactly
4. No scope expansion without updating the issue

## Linking

Link PRs to issues via `Closes #N` in the PR body. Use the issue number
in the branch name, never in the commit scope.

# Code Review Doctrine

## Quality gates (blocking)

All checks must pass locally before push:

- [ ] Tests passing (0 failures)
- [ ] Coverage meets threshold
- [ ] Linting passing (0 errors)
- [ ] Type checking passing (0 errors)

## Zero technical debt

- ❌ No `# noqa`, `@ts-ignore`, `# type: ignore`
- ❌ No `// biome-ignore` without explicit justification
- ❌ No suppressions in the diff

# Context7 Protocol

Before writing ANY code, check Context7 for current documentation:
- Library APIs and syntax
- Framework patterns and best practices
- Configuration options

Training data is often months out of date. Context7 provides
authoritative, up-to-date docs. Skip it for the project's own code
standard-library features, or meta-questions about the project.

# Testing Standards

- TDD preferred: write the failing test first, then the minimal
  implementation that passes it; refactor with the tests green.
- Coverage threshold: **>80%** for new code.
- Coverage for lower-risk areas (documentation, config, formatting)
  may be lower; the threshold is the floor for logic, not a target for
  boilerplate.
- A bug fix ships with its regression test — a fix without a test that
  failed first is an incomplete fix.

## Commands

| kind | command |
| --- | --- |
| test (Python) | `pytest -m "not slow and not live"` |
| test (Python) | `pytest` |
| test (frontend) | `cd web && npm test` |
| type-check (frontend) | `cd web && npm run type-check` |

**Frontend provenance (issue #151):** `npm test` prints two `gate:` lines — the resolved project root and the resolved vitest module path. If a worktree is the target, both paths name that worktree. A path that names the main checkout instead of your worktree means the run measured the wrong tree and the result is a null result.

## Quality Gates

Run these before pushing. All must pass locally:

- **Test (fast, no Docker)** — `pytest -m "not slow and not live"`
- **Test (full incl. slow/Docker)** — `pytest`
- **Frontend test** — `cd web && npm test` — 394 tests; the `gate:` lines in the output state which tree was measured
- **Frontend type-check** — `cd web && npm run type-check` — 0 errors
- **Frontend lint** — `cd web && npm run lint` — 0 errors, 1 known warning (inert `eslint-disable` in `ModelViewer.tsx`)
- **Live e2e (NOT a PR gate)** — `uv run pytest -m live` — drives the real LLM + real Docker render worker (`tests/live_e2e/`, issue #108); needs Docker + `TRAIL_OPENERS_LLM_KEY`; not in CI (no repository secret); fails (never skips) when a prerequisite is missing; runtime is reported per case + total

## Code Style

- All Docker images pin `--platform=linux/amd64` (target x86-64; any arm64 reference is stale).
- Model is configured, never hardcoded: OpenAI-compatible endpoints only; day-1 default `RedHatAI/Qwen3.8-27B-INT4` at `llm.trailopeners.com/v1`, env `TRAIL_OPENERS_LLM_KEY`.
- API keys never reach the browser — all LLM calls are server-side; keys Fernet-encrypted under MASTER_KEY, list endpoint returns names only.
- Every container running LLM-generated code runs with `--network none` (highest-value control, no bypass surface); never `--privileged`, never mount `/var/run/docker.sock`.
- Render worker container: `--cap-drop ALL --read-only --tmpfs /tmp --security-opt no-new-privileges --memory --cpus --pids-limit`; exactly two mounts (rw /work volume, tmpfs /tmp); no `--rm` (caller removes after `docker inspect`).
- error_class is a closed enum (Literal, not bare str): ok | syntax_error | empty_model | artifact_error | timeout | oom | container_error; classification first-match-wins and the table must be TOTAL.
- Output is 3MF, not STL (unambiguous mm units); QIDI accepts STL/OBJ/3MF, STEP not supported. Render worker NEVER emits 3MF — #3 is sole 3MF owner.
- Model config: YAML is source of truth, hot-reloaded on save, UI is a thin editor (never a DB); alias/provider-id split; override cascade runtime flags > model params > provider defaults.
- Mid-conversation model switch: never rewrite the stored transcript — sanitise the outgoing request per target (drop tool messages / strip image_url / truncate oldest with marker).
- Region-selection markers must be RED / high-contrast warm (VLMs are marker-colour-fragile); resolve client-side via CSG module registry + three.js Raycaster, never server-side OpenSCAD colour ID pass.
- No component displays a confident value it has not established. A blank, a dash, or an explicit "not established" control is correct; an invented number, a placeholder model, or a stale value shown as fresh is not.
- #FF3300 is the region marker and nothing else — not errors, not destructive actions, not badges. Blocked / failed / disagreeing states use #D2A63C.
- d33d owns geometry (correct mm, watertight, fits the volume, exports 3MF). Orca owns printing. No slicing setting appears in the UI and none is implied. The only printer fact the UI holds is the build volume it must fit inside.
- All user-facing strings live in web/src/copy.ts. Components never inline prose.
- Measurements, version ids and raw detail render in the mono face; prose in the UI face — so a number can never hide inside a sentence.

## Architecture Notes

- Language: Python (planned 3.12+, pyproject.toml declaring trimesh + pytest); frontend planned React SPA. Pre-first-commit — not yet built.
- Package layout (planned): d33d/ (source) + tests/ + tests/security/ (containment suite, from ticket #1).
- Ticket #1 render worker: caller API render(scad_source, params) -> RenderResult; per-render ephemeral named Docker volume (no host bind mounts); 8 sequential openscad invocations (STL, CSG, 6 views), never parallel; STL failure aborts remaining seven.
- model.csg emitted by #1 (preserves module tree) but consumed only by ticket #6 as the authoritative module registry for region selection.
- Ticket #2 backend: FastAPI on 8080; per-project git repo; reference-photo upload; SSE for progress + token streaming; serves the SPA build; provider_credentials + request_logs tables (prompt canonical hash for model-vs-model diff).
- Capability tiers (declared in config, probed at startup, cached by base_url+model hash): T0 native tool calling+json_schema / T1 fenced-JSON regex+corrective retry (build from day one) / T2 no vision / T3 regex-parsed fixed format.
- Ticket #3 print validation (sole 3MF owner): trimesh load -> mm -> centre -> merge_vertices -> decimate BEFORE repair -> pymeshfix -> fix_normals AFTER repair -> assert watertight AND winding-consistent separately -> thin-feature audit -> auto-orient -> 3MF; 7 deterministic gates in fixed order; QIDI envelope is a named constant read by both centring and the gate.
- Validation return type shaped for N parts now: {parts: [Part], assembly: {joints, layout} (reserved, empty in v1), export: {3mf}}.
- Ticket #4 design loop: photo + chat + stated mm dimensions -> OpenSCAD -> 6 renders -> vision critique vs photo -> patch, capped at 3 auto-iterations; CLARIFY->PROPOSE->SHOW->ITERATE->FINALIZE protocol order is mandatory.
- Ticket #5 SPA: custom React (not Gradio — must host react-konva); one shared React canvas for dimension lines and lasso (the photo-overlay lasso survived #98's re-base to single-point picking); viewer is three.js (not model-viewer — needs Raycaster for #6); 3MF is a download artifact, never rendered inline.
- Ticket #8 evals: promptfoo harness, cases git-tracked under evals/ with hash-pinned prompt files; 7 deterministic gates before any vision judge; golden set floor 20 cases; production gate failures auto-archived to evals/failures.jsonl.
- Deployment: the server runs wherever `python -m d33d.main` is invoked — in practice the operator's Mac; the LLM is a remote OpenAI-compatible endpoint (`llm.trailopeners.com/v1`, key `TRAIL_OPENERS_LLM_KEY`), not a locally hosted model; render containers run in whatever Docker daemon is available (Docker Desktop on macOS in practice). The container boundary in the render worker is the actual defence; the design's second-machine backstop — a dedicated Linux box so a container escape hits a sacrificial machine, not the operator's laptop — is NOT in place today: model-authored OpenSCAD executes in Docker Desktop on the operator's own Mac. Running everything on an AMD Strix Halo Linux box is an optional future isolation boundary, not a current fact. gVisor/Firecracker/Kata/AppArmor were deemed not worth doing in the context of a two-machine split that is not currently in place. Render temp dirs live under `$HOME` (default `~/d33d/render-tmp`) because Docker Desktop on macOS cannot see `/tmp`.
