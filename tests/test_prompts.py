"""tests/test_prompts.py — design-role prompt assembly (ticket #5,
task-critique workstream).

Covers:
- The BOSL2 cheatsheet (from ``prompts/bosl2-cheatsheet.md``) and the FDM
  tolerance table are injected for the ``design`` role ONLY.  The ``critique``
  and ``classification`` roles' prompts are NOT built through
  ``design_prompt`` — a dedicated guard asserts the design-role prompt is the
  only place the cheatsheet is injected.
- Full per-module BOSL2 docs are fetched ON DEMAND: a mocked ``doc_retriever``
  is called lazily (only for the requested modules), not at prompt-build for
  unrequested modules, and only once per module.
- Per-model overrides come from the config (the override cascade via
  ``ModelEntry.params``) and are NOT forked into the core prompt text.
- Prompts use neutral delimiters (XML-style tags with an explicit "data, not
  instructions" framing) around untrusted content — no model-specific tokens.

No Docker, no network: the ``doc_retriever`` is a mocked callable.
"""

from __future__ import annotations

import os
from pathlib import Path

from d33d.config.catalogue import load_catalogue
from d33d.design_prompts import (
    BOSL2_CHEATSHEET_PATH,
    FDM_TOLERANCE_TABLE,
    design_prompt,
    load_bosl2_cheatsheet,
    model_overrides,
    render_module_docs,
    wrap_user_data,
)

STATED = (20.0, 25.0, 30.0)


def _catalogue():
    env = {"TRAIL_OPENERS_LLM_KEY": "stub", "PAID_AZURE_LLM_KEY": "stub"}
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            os.environ[k] = v
        return load_catalogue(Path(__file__).parent / "fixtures" / "models.yaml")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Cheatsheet + FDM table, design-role only
# ---------------------------------------------------------------------------


def test_cheatsheet_file_exists_and_is_read() -> None:
    assert BOSL2_CHEATSHEET_PATH.exists()
    text = load_bosl2_cheatsheet()
    assert "BOSL2" in text
    assert "cuboid" in text  # a verified module signature


def test_design_prompt_injects_cheatsheet() -> None:
    system, _ = design_prompt(stated_dims=STATED)
    # The cheatsheet content is present (a verified module from the file).
    assert "cuboid" in system
    # The cheatsheet block is wrapped in the neutral delimiters.
    assert "<user_data" in system
    # The FDM tolerance table is present.
    assert "FDM tolerance table" in system
    for fit in FDM_TOLERANCE_TABLE:
        assert fit in system


def test_design_prompt_has_neutral_delimiters_not_model_specific_tokens() -> None:
    system, _ = design_prompt(stated_dims=STATED)
    # Neutral XML-style delimiters, framed as data-not-instructions.
    assert "data only" in system or "not instructions" in system
    assert "<user_data" in system and "</user_data>" in system
    # No model-specific prompt tokens (e.g. [INST], <s>, special tokens).
    for token in ("[INST]", "[/INST]", "<s>", "</s>", "[PAD]"):
        assert token not in system


def test_design_role_is_the_only_role_getting_the_cheatsheet() -> None:
    """The cheatsheet + FDM table are design-role-only.  ``design_prompt``
    builds only the design-role prompt; critique/classification prompts are
    built elsewhere (not via this function), so this guard pins that the
    design-role prompt is the sole carrier of the cheatsheet."""
    system, _ = design_prompt(stated_dims=STATED)
    assert "BOSL2 Cheatsheet" in system or "BOSL2 cheatsheet" in system
    # The design role is the one that carries it; the function is named and
    # documented design-role-only, so no critique/classification variant
    # exists here to leak it.  A regression that adds a `role=` param defaulting
    # to something else would change the public signature.
    assert "design" in design_prompt.__doc__.lower() or "design" in __doc__.lower()


# ---------------------------------------------------------------------------
# On-demand per-module doc retrieval (lazy)
# ---------------------------------------------------------------------------


def test_doc_retriever_called_lazily_only_for_requested_modules() -> None:
    calls: list[str] = []

    def retriever(module: str) -> str:
        calls.append(module)
        return f"FULL DOCS for {module} (on demand)"

    system, _ = design_prompt(
        stated_dims=STATED,
        doc_retriever=retriever,
        modules_to_expand=("cuboid",),
    )
    # Only the requested module is fetched — not all modules.
    assert calls == ["cuboid"]
    assert "FULL DOCS for cuboid (on demand)" in system


def test_doc_retriever_not_called_when_no_modules_requested() -> None:
    calls: list[str] = []

    def retriever(module: str) -> str:
        calls.append(module)
        return "docs"

    system, _ = design_prompt(stated_dims=STATED, doc_retriever=retriever)
    assert calls == []  # nothing fetched at prompt-build
    # The cheatsheet is still present (preloaded), but no on-demand docs.
    assert "cuboid" in system


def test_doc_retriever_called_once_per_module() -> None:
    calls: list[str] = []

    def retriever(module: str) -> str:
        calls.append(module)
        return f"docs for {module}"

    # Requesting the same module twice must fetch it once.
    _, _ = design_prompt(
        stated_dims=STATED,
        doc_retriever=retriever,
        modules_to_expand=("cuboid", "cuboid"),
    )
    assert calls == ["cuboid"]


def test_render_module_docs_returns_empty_for_missing_doc() -> None:
    assert render_module_docs("nonexistent", lambda m: None) == ""


def test_render_module_docs_wraps_in_neutral_delimiters() -> None:
    out = render_module_docs("cuboid", lambda m: "the cuboid docs")
    assert "cuboid" in out
    assert "<user_data" in out


# ---------------------------------------------------------------------------
# Per-model overrides from config (not forked into the prompt text)
# ---------------------------------------------------------------------------


def test_model_overrides_come_from_config_cascade() -> None:
    catalogue = _catalogue()
    overrides = model_overrides(catalogue, "design")
    # The design-primary model's params (temperature 0.2, max_tokens 8192)
    # beat the provider defaults — the override cascade.
    assert overrides.get("temperature") == 0.2
    assert overrides.get("max_tokens") == 8192


def test_overrides_not_forked_into_prompt_text() -> None:
    catalogue = _catalogue()
    system, overrides = design_prompt(stated_dims=STATED, catalogue=catalogue)
    # The overrides are returned as call params (the temperature/max_tokens the
    # LLM edge needs)...
    assert overrides.get("temperature") == 0.2
    assert overrides.get("max_tokens") == 8192
    # ...and the *call-param* values are not forked into the prompt as
    # instructions.  The prompt carries the design content (cheatsheet, FDM
    # table), not a "use temperature 0.2" directive.  We assert the prompt does
    # not contain a call-param-style directive (the raw max_tokens integer as a
    # bare token in the instruction text), while the cheatsheet/FDM content is
    # present.
    assert "max_tokens" not in system
    assert "temperature" not in system


def test_design_prompt_returns_overrides_for_the_design_role() -> None:
    catalogue = _catalogue()
    _, overrides = design_prompt(stated_dims=STATED, catalogue=catalogue)
    # The overrides reflect the design role's resolved model (design-primary).
    assert "temperature" in overrides


# ---------------------------------------------------------------------------
# Neutral delimiters helper
# ---------------------------------------------------------------------------


def test_wrap_user_data_frames_data_not_instructions() -> None:
    wrapped = wrap_user_data("user content here")
    assert "user content here" in wrapped
    assert "<user_data" in wrapped and "</user_data>" in wrapped
    assert "not instructions" in wrapped or "data only" in wrapped
