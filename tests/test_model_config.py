"""Model-config YAML core (ticket #3, workstream task-a).

Covers: catalogue load/validate, ${ENV} key interpolation, alias vs
provider-id split, role map, ordered fallbacks, the override cascade
(runtime flags > model params > provider defaults), and atomic hot-reload
(bad YAML never displaces the last-known-good catalogue).

All tests are non-slow: no Docker, no network, no real providers.
"""

from __future__ import annotations

import copy
import textwrap
from pathlib import Path

import pytest
import yaml

from d33d.config import (
    Catalogue,
    CatalogueError,
    ModelCatalogueLoader,
    ModelEntry,
    ResolutionError,
    apply_fallbacks,
    hot_reload,
    load_catalogue,
    resolve_call_params,
)
from d33d.config.catalogue import resolve

MODELS_YAML = textwrap.dedent(
    """\
    providers:
      trailopeners: { base: "https://llm.trailopeners.com/v1", key: "${TRAIL_OPENERS_LLM_KEY}" }
      openai-compat: { base: "https://api.example.com/v1", key: static-secret }

    models:
      - id: vision-primary
        provider: trailopeners
        model: RedHatAI/Qwen3.8-27B-INT4
        context_window: 262144
        params: { temperature: 0.2, max_tokens: 8192 }
        fallbacks: [vision-backup]
        retries: { count: 2, on: [timeout, 5xx] }

      - id: vision-backup
        provider: openai-compat
        model: some/backup-model
        context_window: 32768

    roles:
      design: vision-primary
      critique: vision-primary
      classification: vision-primary
    """
)

ROLE_TO_ALIAS = {
    "design": "vision-primary",
    "critique": "vision-primary",
    "classification": "vision-primary",
}


@pytest.fixture
def models_yaml_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TRAIL_OPENERS_LLM_KEY", "env-key-value")
    p = tmp_path / "models.yaml"
    p.write_text(MODELS_YAML)
    return p


# ---------------------------------------------------------------------------
# load + validation
# ---------------------------------------------------------------------------


def test_load_catalogue_parses_providers_models_roles(models_yaml_file: Path) -> None:
    cat = load_catalogue(models_yaml_file)

    assert isinstance(cat, Catalogue)
    assert cat.providers.keys() == {"trailopeners", "openai-compat"}
    assert list(cat.models.keys()) == ["vision-primary", "vision-backup"]
    assert cat.models_order == ("vision-primary", "vision-backup")
    assert cat.roles == ROLE_TO_ALIAS

    trail = cat.providers["trailopeners"]
    assert trail.base == "https://llm.trailopeners.com/v1"
    assert cat.models["vision-primary"].provider == "trailopeners"
    assert cat.models["vision-primary"].model == "RedHatAI/Qwen3.8-27B-INT4"
    assert cat.models["vision-primary"].context_window == 262144
    assert cat.models["vision-primary"].params == {
        "temperature": 0.2,
        "max_tokens": 8192,
    }
    assert cat.models["vision-primary"].fallbacks == ("vision-backup",)
    assert cat.models["vision-primary"].retries == {
        "count": 2,
        "on": ["timeout", "5xx"],
    }


def test_env_interpolation_resolves_provider_key_from_env(
    models_yaml_file: Path,
) -> None:
    cat = load_catalogue(models_yaml_file)
    assert cat.providers["trailopeners"].key == "env-key-value"
    # static keys pass through untouched
    assert cat.providers["openai-compat"].key == "static-secret"


def test_provider_repr_and_str_never_leak_key_value() -> None:
    """Regression: a live API key must not surface via repr()/str() of a
    Provider (or a RoleResolution embedding one). HIGH #1 — no repr leak."""
    from d33d.config.catalogue import ModelEntry, Provider, RoleResolution

    p = Provider(
        name="p",
        base="https://x/v1",
        key="test-secret-key-xyz123",
        defaults={"temperature": 0.5},
    )
    assert "test-secret-key-xyz123" not in repr(p)
    assert "test-secret-key-xyz123" not in str(p)
    # The redaction marker should be present
    assert "redacted" in repr(p)

    # A RoleResolution embedding a Provider must also not leak via repr
    r = RoleResolution(
        role="design",
        entry=ModelEntry(id="alias", provider="p", model="m/1"),
        provider=p,
        via_fallback=False,
    )
    assert "test-secret-key-xyz123" not in repr(r)


def test_env_interpolation_unset_var_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRAIL_OPENERS_LLM_KEY", raising=False)
    p = tmp_path / "models.yaml"
    p.write_text(MODELS_YAML)
    with pytest.raises(CatalogueError, match="TRAIL_OPENERS_LLM_KEY"):
        load_catalogue(p)


def test_unknown_provider_referenced_by_model_rejected(tmp_path: Path) -> None:
    doc = {
        "providers": {"p": {"base": "https://x/v1"}},
        "models": [{"id": "a", "provider": "missing", "model": "m/1"}],
        "roles": {"design": "a"},
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(CatalogueError, match="unknown provider"):
        load_catalogue(p)


def test_unknown_alias_in_fallbacks_rejected(tmp_path: Path) -> None:
    doc = {
        "providers": {"p": {"base": "https://x/v1"}},
        "models": [
            {"id": "a", "provider": "p", "model": "m/1", "fallbacks": ["ghost"]},
            {"id": "b", "provider": "p", "model": "m/2"},
        ],
        "roles": {"design": "a", "critique": "b", "classification": "b"},
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(CatalogueError, match="unknown alias"):
        load_catalogue(p)


def test_fallback_cycle_rejected(tmp_path: Path) -> None:
    doc = {
        "providers": {"p": {"base": "https://x/v1"}},
        "models": [
            {"id": "a", "provider": "p", "model": "m/1", "fallbacks": ["b"]},
            {"id": "b", "provider": "p", "model": "m/2", "fallbacks": ["a"]},
        ],
        "roles": {"design": "a", "critique": "a", "classification": "a"},
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(CatalogueError, match="cycle"):
        load_catalogue(p)


def test_role_pointing_at_missing_alias_rejected(tmp_path: Path) -> None:
    doc = {
        "providers": {"p": {"base": "https://x/v1"}},
        "models": [{"id": "a", "provider": "p", "model": "m/1"}],
        "roles": {"design": "ghost", "critique": "a", "classification": "a"},
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(CatalogueError, match="unknown model alias"):
        load_catalogue(p)


def test_missing_required_role_rejected(tmp_path: Path) -> None:
    doc = {
        "providers": {"p": {"base": "https://x/v1"}},
        "models": [{"id": "a", "provider": "p", "model": "m/1"}],
        "roles": {"design": "a"},  # critique + classification missing
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(CatalogueError, match="missing required role"):
        load_catalogue(p)


def test_duplicate_model_alias_rejected(tmp_path: Path) -> None:
    doc = {
        "providers": {"p": {"base": "https://x/v1"}},
        "models": [
            {"id": "a", "provider": "p", "model": "m/1"},
            {"id": "a", "provider": "p", "model": "m/2"},
        ],
        "roles": {"design": "a", "critique": "a", "classification": "a"},
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(CatalogueError, match="duplicate"):
        load_catalogue(p)


def test_invalid_yaml_file_rejected(tmp_path: Path) -> None:
    p = tmp_path / "m.yaml"
    p.write_text("providers: [unclosed\n  models: ")
    with pytest.raises(CatalogueError):
        load_catalogue(p)


def test_missing_providers_section_rejected(tmp_path: Path) -> None:
    p = tmp_path / "m.yaml"
    p.write_text("models: []\nroles: {}\n")
    with pytest.raises(CatalogueError, match="providers"):
        load_catalogue(p)


# ---------------------------------------------------------------------------
# role -> alias -> provider resolution
# ---------------------------------------------------------------------------


def test_resolve_role_to_alias_to_provider(models_yaml_file: Path) -> None:
    cat = load_catalogue(models_yaml_file)
    r = resolve(cat, "design")

    assert r.role == "design"
    assert r.entry.id == "vision-primary"
    assert r.entry.model == "RedHatAI/Qwen3.8-27B-INT4"
    assert r.provider.base == "https://llm.trailopeners.com/v1"
    assert r.provider.key == "env-key-value"


def test_resolve_unknown_role_raises(models_yaml_file: Path) -> None:
    cat = load_catalogue(models_yaml_file)
    with pytest.raises(CatalogueError, match="unknown role"):
        resolve(cat, "mystery")


def test_alias_vs_provider_id_split(models_yaml_file: Path) -> None:
    """The alias (id) is what roles reference; the volatile provider model id
    lives in ``model`` — swapping one does not touch the other."""
    cat = load_catalogue(models_yaml_file)
    entry = cat.model("vision-primary")
    assert entry.id == "vision-primary"
    assert entry.model == "RedHatAI/Qwen3.8-27B-INT4"
    # a provider id swap is a one-line edit; roles and alias are untouched
    cat2 = _swap_model_id(
        models_yaml_file, "RedHatAI/Qwen3.8-27B-INT4", "Other/New-Model"
    )
    assert cat2.model("vision-primary").model == "Other/New-Model"
    assert cat2.roles == cat.roles


def _swap_model_id(models_yaml_file: Path, old: str, new: str) -> Catalogue:
    doc = yaml.safe_load(models_yaml_file.read_text())
    for m in doc["models"]:
        if m.get("model") == old:
            m["model"] = new
    models_yaml_file.write_text(yaml.safe_dump(doc))
    return load_catalogue(models_yaml_file)


# ---------------------------------------------------------------------------
# override cascade: runtime flags > model params > provider defaults
# ---------------------------------------------------------------------------


def test_cascade_model_params_beat_provider_defaults(models_yaml_file: Path) -> None:
    cat = load_catalogue(models_yaml_file)
    params = resolve_call_params(cat, "design")
    # model params present -> they win over any provider default
    assert params == {"temperature": 0.2, "max_tokens": 8192}


def test_cascade_provider_defaults_fill_gaps(
    models_yaml_file: Path, tmp_path: Path
) -> None:
    doc = yaml.safe_load(models_yaml_file.read_text())
    doc["models"][0]["params"] = {"temperature": 0.9}  # drop max_tokens
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(doc))
    cat = load_catalogue(p)
    params = resolve_call_params(cat, "design", provider_defaults={"max_tokens": 4096})
    assert params == {"temperature": 0.9, "max_tokens": 4096}


def test_cascade_no_params_no_defaults_gives_empty(tmp_path: Path) -> None:
    doc = {
        "providers": {"p": {"base": "https://x/v1"}},
        "models": [{"id": "a", "provider": "p", "model": "m/1"}],
        "roles": {"design": "a", "critique": "a", "classification": "a"},
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(doc))
    cat = load_catalogue(p)
    assert resolve_call_params(cat, "design") == {}


def test_cascade_runtime_flags_beat_model_params(models_yaml_file: Path) -> None:
    cat = load_catalogue(models_yaml_file)
    params = resolve_call_params(
        cat, "design", runtime_flags={"temperature": 1.0, "top_p": 0.99}
    )
    # runtime flags override the model's own params
    assert params["temperature"] == 1.0
    assert params["top_p"] == 0.99
    # non-conflicting model params survive
    assert params["max_tokens"] == 8192


def test_cascade_runtime_flags_beat_everything(models_yaml_file: Path) -> None:
    cat = load_catalogue(models_yaml_file)
    params = resolve_call_params(
        cat,
        "design",
        runtime_flags={"max_tokens": 128},
        provider_defaults={"max_tokens": 4096, "top_p": 0.1},
    )
    assert params["max_tokens"] == 128
    # a provider default fills a gap the model params don't cover
    assert params["top_p"] == 0.1
    # the model's own temperature beats the provider default
    assert params["temperature"] == 0.2


def test_cascade_does_not_mutate_catalogue(models_yaml_file: Path) -> None:
    cat = load_catalogue(models_yaml_file)
    before = copy.deepcopy(cat.models["vision-primary"].params)
    resolve_call_params(cat, "design", runtime_flags={"temperature": 9.9})
    assert cat.models["vision-primary"].params == before


# ---------------------------------------------------------------------------
# ordered fallbacks
# ---------------------------------------------------------------------------


def test_apply_fallbacks_ordered_first_available_wins(models_yaml_file: Path) -> None:
    cat = load_catalogue(models_yaml_file)
    r = resolve(cat, "design")
    # primary is available -> it is returned, fallbacks not consulted
    assert r.entry.id == "vision-primary"
    assert r.via_fallback is False


def test_apply_fallbacks_skips_unavailable_primary(models_yaml_file: Path) -> None:
    cat = load_catalogue(models_yaml_file)
    # simulate: primary's provider key is unresolved (unavailable)
    primary = cat.model("vision-primary")
    unavailable = {primary.provider + "/" + primary.model}
    r = resolve(cat, "design", unavailable=unavailable)
    assert r.entry.id == "vision-backup"
    assert r.via_fallback is True
    assert r.provider.base == "https://api.example.com/v1"


def test_apply_fallbacks_order_is_respected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = {
        "providers": {
            "p1": {"base": "https://a/v1"},
            "p2": {"base": "https://b/v1"},
            "p3": {"base": "https://c/v1"},
        },
        "models": [
            {"id": "a", "provider": "p1", "model": "m/1", "fallbacks": ["b", "c"]},
            {"id": "b", "provider": "p2", "model": "m/2", "fallbacks": ["c"]},
            {"id": "c", "provider": "p3", "model": "m/3"},
        ],
        "roles": {"design": "a", "critique": "a", "classification": "a"},
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(doc))
    cat = load_catalogue(p)
    r = apply_fallbacks(cat, "a", unavailable={"p1/m/1", "p2/m/2"})
    assert r.id == "c"
    # and with only the primary down, the FIRST fallback wins, not the last
    r2 = apply_fallbacks(cat, "a", unavailable={"p1/m/1"})
    assert r2.id == "b"


def test_apply_fallbacks_all_unavailable_raises(models_yaml_file: Path) -> None:
    cat = load_catalogue(models_yaml_file)
    unavailable = {
        "trailopeners/RedHatAI/Qwen3.8-27B-INT4",
        "openai-compat/some/backup-model",
    }
    with pytest.raises(ResolutionError, match="no available model"):
        resolve(cat, "design", unavailable=unavailable)


def test_fallback_chain_follows_transitively(tmp_path: Path) -> None:
    doc = {
        "providers": {"p": {"base": "https://x/v1"}},
        "models": [
            {"id": "a", "provider": "p", "model": "m/1", "fallbacks": ["b"]},
            {"id": "b", "provider": "p", "model": "m/2", "fallbacks": ["c"]},
            {"id": "c", "provider": "p", "model": "m/3"},
        ],
        "roles": {"design": "a", "critique": "a", "classification": "a"},
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(doc))
    cat = load_catalogue(p)
    r = apply_fallbacks(cat, "a", unavailable={"p/m/1", "p/m/2"})
    assert r.id == "c"


# ---------------------------------------------------------------------------
# atomic hot-reload
# ---------------------------------------------------------------------------


def test_hot_reload_picks_up_role_swap_without_restart(models_yaml_file: Path) -> None:
    loader = ModelCatalogueLoader(models_yaml_file)
    cat = loader.load()
    assert cat.roles["design"] == "vision-primary"

    # the operator swaps the alias behind the design role (YAML edit)
    doc = yaml.safe_load(models_yaml_file.read_text())
    doc["roles"]["design"] = "vision-backup"
    models_yaml_file.write_text(yaml.safe_dump(doc))

    new_cat, _ = hot_reload(loader)
    assert new_cat.roles["design"] == "vision-backup"
    # and subsequent resolutions use the new alias
    r = resolve(new_cat, "design")
    assert r.entry.id == "vision-backup"
    assert r.entry.model == "some/backup-model"
    # untouched roles survive the reload
    assert new_cat.roles["critique"] == "vision-primary"


def test_hot_reload_rejects_bad_yaml_and_keeps_live_config(
    models_yaml_file: Path,
) -> None:
    loader = ModelCatalogueLoader(models_yaml_file)
    live = loader.load()

    # operator saves a syntactically broken file
    models_yaml_file.write_text("providers: [unclosed\n  roles: ")

    with pytest.raises(CatalogueError):
        hot_reload(loader)

    # the previously-live catalogue is still intact and usable
    r = resolve(live, "design")
    assert r.entry.id == "vision-primary"
    assert r.entry.model == "RedHatAI/Qwen3.8-27B-INT4"


def test_hot_reload_rejects_validation_failure_and_keeps_live_config(
    models_yaml_file: Path,
) -> None:
    loader = ModelCatalogueLoader(models_yaml_file)
    live = loader.load()

    # valid YAML, but a role now points at a missing alias
    doc = yaml.safe_load(models_yaml_file.read_text())
    doc["roles"]["design"] = "ghost"
    models_yaml_file.write_text(yaml.safe_dump(doc))

    with pytest.raises(CatalogueError, match="unknown model alias"):
        hot_reload(loader)

    assert resolve(live, "design").entry.id == "vision-primary"


def test_hot_reload_detects_no_change(models_yaml_file: Path) -> None:
    loader = ModelCatalogueLoader(models_yaml_file)
    first = loader.load()
    same, changed = hot_reload(loader)
    assert changed is False
    assert same is first


def test_hot_reload_after_repair_recovers(models_yaml_file: Path) -> None:
    loader = ModelCatalogueLoader(models_yaml_file)
    live = loader.load()

    models_yaml_file.write_text("not: [valid: yaml")
    with pytest.raises(CatalogueError):
        hot_reload(loader)

    # operator fixes the file -> the next reload succeeds and takes over
    fixed = MODELS_YAML.replace(
        "classification: vision-primary", "classification: vision-backup"
    )
    models_yaml_file.write_text(fixed)
    new_cat, changed = hot_reload(loader)
    assert changed is True
    assert new_cat is not live
    assert new_cat.roles["classification"] == "vision-backup"


# ---------------------------------------------------------------------------
# ModelCatalogue accessors
# ---------------------------------------------------------------------------


def test_model_catalogue_model_lookup(models_yaml_file: Path) -> None:
    cat = load_catalogue(models_yaml_file)
    assert isinstance(cat.model("vision-primary"), ModelEntry)
    with pytest.raises(CatalogueError, match="unknown model alias"):
        cat.model("nope")


def test_env_interpolation_in_nested_provider_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTHER_KEY", "abc123")
    doc = {
        "providers": {"p": {"base": "https://x/v1", "key": "${OTHER_KEY}"}},
        "models": [{"id": "a", "provider": "p", "model": "m/1"}],
        "roles": {"design": "a", "critique": "a", "classification": "a"},
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(doc))
    cat = load_catalogue(p)
    assert cat.providers["p"].key == "abc123"


def test_resolve_model_facade(models_yaml_file: Path) -> None:
    """``resolve_model`` is the public facade over role resolution."""
    from d33d.config import resolve_model

    cat = load_catalogue(models_yaml_file)
    r = resolve_model(cat, "design")
    assert r.entry.id == "vision-primary"
    assert r.provider.key == "env-key-value"


def test_load_catalogue_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(CatalogueError, match="not found"):
        load_catalogue(tmp_path / "nope.yaml")


def test_empty_roles_rejected(tmp_path: Path) -> None:
    doc = {
        "providers": {"p": {"base": "https://x/v1"}},
        "models": [{"id": "a", "provider": "p", "model": "m/1"}],
        "roles": {},
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(CatalogueError, match="roles"):
        load_catalogue(p)


def test_role_target_not_a_string_rejected(tmp_path: Path) -> None:
    doc = {
        "providers": {"p": {"base": "https://x/v1"}},
        "models": [{"id": "a", "provider": "p", "model": "m/1"}],
        "roles": {"design": ["a"], "critique": "a", "classification": "a"},
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(CatalogueError, match="string alias"):
        load_catalogue(p)
