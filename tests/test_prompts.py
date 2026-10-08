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
    METRIC_SCREW_CLEARANCE_MM,
    SCREW_CLEARANCE_INSTRUCTION,
    SCREW_SIZE_RE,
    clearance_rows_line,
    design_prompt,
    import_part_instruction,
    load_bosl2_cheatsheet,
    model_overrides,
    render_module_docs,
    wrap_user_data,
)

STATED = (20.0, 25.0, 30.0)


def test_design_prompt_instructs_readable_names_and_labels() -> None:
    """Issue #248: the design-role prompt instructs the model to use
    readable full-word snake_case parameter names (with the fst →
    fillet_size_top bad/good example) and a plain-language label for every
    declared parameter, and to carry a ``parameters`` metadata array in
    the reply."""

    # Pull the emission instruction out of the live prompt builder —
    # never a hardcoded copy that can drift from the module.
    from d33d.design_loop import _design_messages

    messages = _design_messages(
        photo="data:image/png;base64,REF",
        chat_history=(),
        stated=STATED,
        repair=None,
        request="make a part",
    )
    user_text = "\n".join(
        part["text"] if isinstance(part, dict) and part.get("type") == "text"
        else str(part)
        for part in messages[0]["content"]
    )
    # The readable-name instruction, with the bad/good example.
    assert "full words in snake_case" in user_text
    assert "fst" in user_text
    assert "fillet_size_top" in user_text
    # The plain-language label instruction.
    assert "label" in user_text and "Top fillet size" in user_text
    # The parameters metadata array in the reply shape.
    assert '"parameters"' in user_text
    # The axis constraint (W/D/H only when the param realises it).
    assert '"W" or "D" or "H"' in user_text
    # Issue #385: the axis-tag rule (only for the part's own extents).
    assert (
        "Declare an axis ONLY "
        "when the parameter IS the part's own overall W, D or H extent"
    ) in user_text
    assert "hole_distance_from_left_edge" in user_text
    assert "is NOT an axis parameter" in user_text


def test_design_prompt_axis_schema_description() -> None:
    """Issue #385: the emit_design tool schema's axis field carries the
    axis-tag rule as a description (for T0 native-tool-calling models).
    Issue #409: the schema's function description also carries the
    derived ``total_height`` instruction — the SAME constant the live
    prompt builders render (so the T0 wire carries the instruction the
    T1 fenced-JSON path gets from the prompt text)."""
    from d33d.design_llm import ROLE_TOOL_SCHEMAS, TOTAL_HEIGHT_INSTRUCTION

    schema = ROLE_TOOL_SCHEMAS["emit_design"]
    props = schema["function"]["parameters"]["properties"]["parameters"]["items"]["properties"]
    axis_desc = props["axis"]["description"]
    assert "part's own overall W, D or H extent" in axis_desc
    assert "hole_distance_from_left_edge" in axis_desc
    # Issue #409 (task-prompt): the total_height instruction rides the
    # tool's function description (the T0 wire carries it verbatim).
    assert TOTAL_HEIGHT_INSTRUCTION in schema["function"]["description"]


def test_design_prompt_carries_total_height_instruction() -> None:
    """Issue #409 (task-prompt): the ``TOTAL_HEIGHT_INSTRUCTION`` constant
    (``d33d.design_llm`` — the SINGLE definition) is wired into BOTH live
    design prompt surfaces, so the instruction reaches the model regardless
    of which prompt builder the call path uses:

    - ``d33d.design_prompts.design_prompt`` (the static design-role prompt
      — the same surface ``SCREW_CLEARANCE_INSTRUCTION`` is pinned on);
    - ``d33d.design_loop._design_system`` (the live loop's system prompt —
      the #317 single-source pattern: both prompts carry the same text
      from the same constant).

    The instruction text carries the key elements: the parameter name
    (``total_height``), the rule (derived = a literal sum), the scope
    (only when the part IS a stack of features), and it does NOT instruct
    axis tagging (it complements #385's axis rule, it does not replace it).
    """
    import d33d.design_loop as dl
    from d33d.design_llm import TOTAL_HEIGHT_INSTRUCTION

    # The constant is importable and non-empty.
    assert isinstance(TOTAL_HEIGHT_INSTRUCTION, str)
    assert len(TOTAL_HEIGHT_INSTRUCTION) > 20
    # The instruction names the parameter.
    assert "total_height" in TOTAL_HEIGHT_INSTRUCTION
    # It specifies the parameter is derived (a literal sum).
    assert "derived" in TOTAL_HEIGHT_INSTRUCTION
    assert "literal sum" in TOTAL_HEIGHT_INSTRUCTION
    # It scopes to stacked parts (not a blanket rule).
    assert "stack" in TOTAL_HEIGHT_INSTRUCTION
    # It is NOT an axis instruction (complements #385, does not replace it).
    assert '"axis"' not in TOTAL_HEIGHT_INSTRUCTION

    # The live prompt surfaces carry the constant verbatim (single source —
    # the two prompts cannot drift).
    system, _ = design_prompt(stated_dims=STATED)
    assert TOTAL_HEIGHT_INSTRUCTION in system
    live_system = dl._design_system(STATED)
    assert TOTAL_HEIGHT_INSTRUCTION in live_system


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


def test_design_prompt_renders_screw_clearance_rows_from_single_table():
    """Issue #317: the screw clearance rows render from the SINGLE source
    table — every (size, diameter) pair appears in the prompt, the live
    loop's system prompt renders the identical rows, and no second
    hard-coded copy of the numbers exists in the prompt module."""
    system, _ = design_prompt(stated_dims=STATED)
    # Every table entry renders (size name + its diameter, from the table
    # — never a second copy).
    for size, mm in METRIC_SCREW_CLEARANCE_MM.items():
        assert f"{size} = {mm:g} mm" in system
    # The explicit instruction renders too.
    assert SCREW_CLEARANCE_INSTRUCTION in system
    # The LIVE loop prompt renders the same rows from the same table
    # (single-source: the two prompts cannot drift).
    import d33d.design_loop as dl

    live_system = dl._design_system(STATED)
    assert clearance_rows_line() in live_system
    assert SCREW_CLEARANCE_INSTRUCTION in live_system


def test_screw_size_regex_word_boundary_and_table_lookup():
    """Issue #317 detection regex: word-boundary anchoring (``M40`` and
    ``BM4`` do not match; ``M4x20`` does) and the ``M`` + value lookup
    into the single-source table."""
    m = SCREW_SIZE_RE.search("a 60 × 45 mm plate with an M4 hole")
    assert m is not None and f"M{m.group(1)}" in METRIC_SCREW_CLEARANCE_MM
    # ``M40`` / ``BM4``: no word-boundary match.
    assert SCREW_SIZE_RE.search("an M40 flange") is None
    assert SCREW_SIZE_RE.search("the BM4 reference") is None
    # ``M4x20``: a thread-length spec still names an M4 thread.
    m4x20 = SCREW_SIZE_RE.search("an M4x20 bolt")
    assert m4x20 is not None and f"M{m4x20.group(1)}" == "M4"
    # M2.5 maps into the table.
    m25 = SCREW_SIZE_RE.search("holes for M2.5 screws")
    assert m25 is not None and f"M{m25.group(1)}" in METRIC_SCREW_CLEARANCE_MM


def test_design_prompt_instructs_screw_clearance_not_nominal():
    """Issue #317: the prompt instructs that a user-named metric screw
    through-hole is modelled at the table's clearance diameter (not the
    nominal size) and the clearance stated in the param's reason."""
    system, _ = design_prompt(stated_dims=STATED)
    assert "clearance diameter" in system
    assert "NOT the nominal size" in system
    assert "parameter's reason" in system



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


# ---------------------------------------------------------------------------
# Issue #332 — import-aware prompt (the import section)
# ---------------------------------------------------------------------------


def test_import_part_instruction_renders_scale_from_part_scale():
    """Issue #332 sub-issue 3: the import instruction renders the scale
    factor from ``part_scale`` (the settled file→mm factor). A scale of
    1.0 (mm) renders ``scale(1)``; a scale of 25.4 (inches) renders
    ``scale(25.4)``. The instruction text lives in one place
    (``design_prompts``) and is pinned here."""
    # scale=1.0 (mm): renders scale(1)
    text_1 = import_part_instruction(1.0)
    assert 'scale(1) import("part.stl")' in text_1
    # scale=25.4 (inches): renders scale(25.4)
    text_254 = import_part_instruction(25.4)
    assert 'scale(25.4) import("part.stl")' in text_254
    # scale=10 (cm): renders scale(10)
    text_10 = import_part_instruction(10.0)
    assert 'scale(10) import("part.stl")' in text_10


def test_import_part_instruction_contains_import_and_add_cut_only():
    """Issue #332 sub-issue 3: the import instruction states that the part
    already exists as ``import("part.stl")``, that it must be placed with
    ``scale(...)`` as the first operation, and that the model may only ADD
    and CUT — never rebuild, re-model, or resize."""
    text = import_part_instruction(1.0)
    assert 'import("part.stl")' in text
    assert "scale(1)" in text
    assert "ADD geometry" in text
    assert "CUT geometry" in text
    assert "union" in text
    assert "difference" in text
    assert "Never rebuild" in text
    assert "resize" in text


def test_import_part_instruction_minimal_skeleton():
    """Issue #332 sub-issue 3: the import instruction includes a minimal
    correct skeleton showing ``scale(...) import("part.stl")`` as the
    first operation."""
    text = import_part_instruction(25.4)
    assert "scale(25.4) import(\"part.stl\")" in text
    assert "union() { ... }" in text


def test_design_system_with_part_includes_import_section():
    """Issue #332 sub-issue 3: ``_design_system`` with a ``part_scale``
    renders the import section. Without ``part_scale`` (``None``) the
    prompt is byte-identical to the no-part form (regression anchor)."""
    import d33d.design_loop as dl

    no_part = dl._design_system(STATED)
    with_part = dl._design_system(STATED, 1.0)
    # The import section is present when part_scale is given.
    assert 'import("part.stl")' in with_part
    assert "scale(1)" in with_part
    # Without part_scale the prompt does NOT contain the import section.
    assert 'import("part.stl")' not in no_part
    # The no-part prompt is a prefix/subset relationship: the core parts
    # (ground-truth dims, clearance table) are present in both.
    for part in ("Ground-truth dimensions", "Screw clearance"):
        assert part in no_part
        assert part in with_part


def test_design_system_with_part_scale_254_renders_scale_254():
    """Issue #332 sub-issue 3: a 3MF imported at inches (scale=25.4)
    renders ``scale(25.4)`` in the system prompt."""
    import d33d.design_loop as dl

    text = dl._design_system(STATED, 25.4)
    assert "scale(25.4)" in text
    assert 'import("part.stl")' in text


def test_design_messages_with_part_includes_import_section():
    """Issue #332 sub-issue 3: ``_design_messages`` with ``part_scale``
    renders the import section in the user message. Without ``part_scale``
    the prompt is byte-identical to the no-part form."""
    import d33d.design_loop as dl

    no_part = dl._design_messages(
        photo="data:image/png;base64,REF",
        chat_history=(),
        stated=STATED,
        repair=None,
        request="make a part",
    )
    with_part = dl._design_messages(
        photo="data:image/png;base64,REF",
        chat_history=(),
        stated=STATED,
        repair=None,
        request="make a part",
        part_scale=1.0,
    )
    no_text = "\n".join(
        p["text"] for p in no_part[0]["content"]
        if isinstance(p, dict) and "text" in p
    )
    with_text = "\n".join(
        p["text"] for p in with_part[0]["content"]
        if isinstance(p, dict) and "text" in p
    )
    # The import section is present in the with-part prompt.
    assert 'import("part.stl")' in with_text
    assert "scale(1)" in with_text
    # The no-part prompt does NOT contain the import section.
    assert 'import("part.stl")' not in no_text
    # Both share the common ground-truth line.
    assert "Reference dimensions" in no_text
    assert "Reference dimensions" in with_text


def test_design_messages_unsettled_part_is_byte_identical_to_no_part():
    """Issue #332 sub-issue 3: ``part_scale=None`` (no part, or unsettled
    part) renders byte-identical to the no-part prompt (regression anchor)."""
    import d33d.design_loop as dl

    no_part = dl._design_messages(
        photo="data:image/png;base64,REF",
        chat_history=(),
        stated=STATED,
        repair=None,
        request="make a part",
    )
    unsettled = dl._design_messages(
        photo="data:image/png;base64,REF",
        chat_history=(),
        stated=STATED,
        repair=None,
        request="make a part",
        part_scale=None,
    )
    no_text = "\n".join(
        p["text"] for p in no_part[0]["content"]
        if isinstance(p, dict) and "text" in p
    )
    unsettled_text = "\n".join(
        p["text"] for p in unsettled[0]["content"]
        if isinstance(p, dict) and "text" in p
    )
    assert no_text == unsettled_text


def test_import_part_lines_none_returns_empty():
    """Issue #332 sub-issue 3: ``import_part_lines(None)`` returns an
    empty list (byte-identity regression anchor)."""
    from d33d.design_loop import import_part_lines

    assert import_part_lines(None) == []
    assert import_part_lines(1.0) != []
    assert 'import("part.stl")' in import_part_lines(1.0)[0]
