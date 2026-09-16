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
| test | `pytest -m "not slow"` |
| test | `pytest` |


## Quality Gates

Run these before pushing. All must pass locally:

- **Test (fast, no Docker)** — `pytest -m "not slow"`
- **Test (full incl. slow/Docker)** — `pytest`


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
- Deployment split: runs entirely on the AMD Strix Halo Linux box (Docker, models, keys); the Mac is a browser only — the second machine is a primary security control; gVisor/Firecracker/Kata/AppArmor deemed not worth doing.
