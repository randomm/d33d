"""Model-config YAML catalogue: load, validate, resolve, atomic hot-reload.

The YAML file is the source of truth for every model the backend can call.
Hot-reload is atomic: a bad edit (syntax error, unknown alias, broken role,
fallback cycle) is rejected and the last-known-good catalogue stays live.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

Role = Literal["design", "critique", "classification"]

#: The minimum roles map. Every catalogue must bind at least these three.
REQUIRED_ROLES: frozenset[str] = frozenset({"design", "critique", "classification"})

_ENV_VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class CatalogueError(Exception):
    """A catalogue file failed to parse or failed validation."""

    def __init__(self, message: str, source: str = "") -> None:
        self.message = message
        self.source = source
        super().__init__(message)


class ResolutionError(Exception):
    """A role could not be resolved to a callable model."""


@dataclass(frozen=True)
class Provider:
    """One OpenAI-compatible provider endpoint."""

    name: str
    base: str
    #: Resolved key (from ``${ENV}`` interpolation or a literal). Never
    #: serialized into any response body — server-side only.
    key: str = ""
    #: Provider defaults. Lowest precedence in the override cascade.
    defaults: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelEntry:
    """One model in the catalogue.

    ``id`` is the stable client-facing alias; ``model`` is the volatile
    provider model id. Swapping providers touches only the ``model`` field —
    roles, fallbacks, and the rest of the codebase reference the alias.
    """

    id: str
    provider: str
    model: str
    context_window: int | None = None
    params: dict[str, Any] = field(default_factory=dict)
    fallbacks: tuple[str, ...] = ()
    retries: dict[str, Any] = field(default_factory=dict)

    def provider_key(self) -> str:
        """The cache/identity key for this model under its provider base."""
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class Catalogue:
    """A validated, immutable model catalogue."""

    source: Path
    providers: dict[str, Provider]
    models: dict[str, ModelEntry]
    roles: dict[str, str]
    models_order: tuple[str, ...] = ()

    def model(self, alias: str) -> ModelEntry:
        try:
            return self.models[alias]
        except KeyError:
            raise CatalogueError(f"unknown model alias: {alias!r}") from None

    def role(self, role: str) -> str:
        try:
            return self.roles[role]
        except KeyError:
            raise CatalogueError(f"unknown role: {role!r}") from None


@dataclass(frozen=True)
class RoleResolution:
    """The outcome of resolving a role to a concrete, callable model."""

    role: str
    entry: ModelEntry
    provider: Provider
    via_fallback: bool = False


def _interp_env(value: Any, where: str) -> str:
    """Resolve a ``${ENV_VAR}`` key reference. Unset vars fail loudly."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CatalogueError(
            f"{where}: key must be a string, got {type(value).__name__}"
        )
    m = _ENV_VAR_RE.match(value)
    if m:
        name = m.group(1)
        if name not in os.environ:
            raise CatalogueError(
                f"{where}: environment variable {name!r} (referenced by key {value!r}) is not set"
            )
        return os.environ[name]
    return value


def _require_str(d: dict[str, Any], field_name: str, where: str) -> str:
    v = d.get(field_name)
    if not isinstance(v, str) or not v:
        raise CatalogueError(f"{where}: missing required string field {field_name!r}")
    return v


def _require_int(d: dict[str, Any], field_name: str, where: str) -> int:
    v = d.get(field_name)
    if not isinstance(v, int) or isinstance(v, bool):
        raise CatalogueError(f"{where}: field {field_name!r} must be an integer")
    return v


def _optional_dict(d: dict[str, Any], field_name: str, where: str) -> dict[str, Any]:
    v = d.get(field_name)
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise CatalogueError(f"{where}: field {field_name!r} must be a mapping")
    return dict(v)


def _optional_list(d: dict[str, Any], field_name: str, where: str) -> list[str]:
    v = d.get(field_name)
    if v is None:
        return []
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise CatalogueError(f"{where}: field {field_name!r} must be a list of strings")
    return list(v)


def _build_providers(raw: Any, source: Path) -> dict[str, Provider]:
    if not isinstance(raw, dict) or not raw:
        raise CatalogueError(f"{source}: 'providers' must be a non-empty mapping")
    out: dict[str, Provider] = {}
    for name, spec in raw.items():
        where = f"provider {name!r}"
        if not isinstance(spec, dict):
            raise CatalogueError(f"{where}: must be a mapping")
        base = _require_str(spec, "base", where)
        key = _interp_env(spec.get("key"), where)
        defaults = _optional_dict(spec, "defaults", where)
        out[name] = Provider(name=str(name), base=base, key=key, defaults=defaults)
    return out


def _build_models(
    raw: Any, source: Path
) -> tuple[dict[str, ModelEntry], tuple[str, ...]]:
    if not isinstance(raw, list) or not raw:
        raise CatalogueError(f"{source}: 'models' must be a non-empty list")
    out: dict[str, ModelEntry] = {}
    order: list[str] = []
    for i, spec in enumerate(raw):
        where = f"model #{i}"
        if not isinstance(spec, dict):
            raise CatalogueError(f"{where}: must be a mapping")
        alias = _require_str(spec, "id", where)
        if alias in out:
            raise CatalogueError(f"{where}: duplicate model alias {alias!r}")
        provider = _require_str(spec, "provider", where)
        model_id = _require_str(spec, "model", where)
        where = f"model {alias!r}"
        context_window: int | None = None
        if "context_window" in spec and spec["context_window"] is not None:
            context_window = _require_int(spec, "context_window", where)
        params = _optional_dict(spec, "params", where)
        fallbacks = _optional_list(spec, "fallbacks", where)
        retries = _optional_dict(spec, "retries", where)
        # YAML parses the reserved word ``on:`` as the bool True key;
        # normalise it back to the string ``"on"`` the spec intends.
        retries = {("on" if k is True else k): v for k, v in retries.items()}
        out[alias] = ModelEntry(
            id=alias,
            provider=provider,
            model=model_id,
            context_window=context_window,
            params=params,
            fallbacks=tuple(fallbacks),
            retries=retries,
        )
        order.append(alias)
    return out, tuple(order)


def _validate_cross_refs(
    providers: dict[str, Provider],
    models: dict[str, ModelEntry],
    models_order: tuple[str, ...],
    raw_roles: Any,
    source: Path,
) -> dict[str, str]:
    # model -> provider
    for alias, entry in models.items():
        if entry.provider not in providers:
            raise CatalogueError(
                f"model {alias!r}: unknown provider {entry.provider!r} (defined: "
                f"{sorted(providers)})"
            )

    # role -> alias
    if not isinstance(raw_roles, dict) or not raw_roles:
        raise CatalogueError(f"{source}: 'roles' must be a non-empty mapping")
    roles: dict[str, str] = {}
    for role, alias in raw_roles.items():
        if not isinstance(alias, str):
            raise CatalogueError(f"role {role!r}: target must be a string alias")
        if alias not in models:
            raise CatalogueError(f"role {role!r}: unknown model alias {alias!r}")
        roles[str(role)] = alias
    missing = REQUIRED_ROLES - roles.keys()
    if missing:
        raise CatalogueError(f"roles: missing required role(s): {sorted(missing)}")

    # fallbacks -> alias + no cycles (each fallback chain must terminate)
    for alias, entry in models.items():
        for fb in entry.fallbacks:
            if fb not in models:
                raise CatalogueError(
                    f"model {alias!r}: fallback {fb!r} is an unknown alias"
                )
    for start in models_order:
        seen: set[str] = set()
        cur: str | None = start
        while cur is not None:
            if cur in seen:
                raise CatalogueError(f"fallback cycle detected involving model {cur!r}")
            seen.add(cur)
            cur = _first_fallback(models, cur)
    return roles


def _first_fallback(models: dict[str, ModelEntry], alias: str) -> str | None:
    fb = models[alias].fallbacks
    return fb[0] if fb else None


def _load_document(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise CatalogueError(f"invalid YAML in {path}: {e}", source=str(path)) from e
    if not isinstance(raw, dict):
        raise CatalogueError(f"{path}: top level must be a mapping", source=str(path))
    return raw


def load_catalogue(path: Path | str) -> Catalogue:
    """Load and validate a model catalogue YAML file.

    Raises :class:`CatalogueError` on any parse or validation failure. The
    resolved ``${ENV}`` keys are kept in memory only.
    """
    p = Path(path)
    if not p.is_file():
        raise CatalogueError(f"catalogue file not found: {p}")
    raw = _load_document(p)
    providers = _build_providers(raw.get("providers"), p)
    models, order = _build_models(raw.get("models"), p)
    roles = _validate_cross_refs(providers, models, order, raw.get("roles"), p)
    return Catalogue(
        source=p,
        providers=providers,
        models=models,
        roles=roles,
        models_order=order,
    )


def resolve(
    catalogue: Catalogue,
    role: str,
    *,
    unavailable: set[str] | None = None,
) -> RoleResolution:
    """Resolve ``role -> alias -> provider``, walking ordered fallbacks.

    ``unavailable`` is an optional set of ``provider_key`` strings
    (``ModelEntry.provider_key``) to treat as down, driving fallback.
    """
    entry = apply_fallbacks(catalogue, catalogue.role(role), unavailable=unavailable)
    provider = catalogue.providers[entry.provider]
    via = entry.id != catalogue.role(role)
    return RoleResolution(role=role, entry=entry, provider=provider, via_fallback=via)


def apply_fallbacks(
    catalogue: Catalogue,
    alias: str,
    *,
    unavailable: set[str] | None = None,
) -> ModelEntry:
    """Follow the ordered fallback chain from ``alias``, skipping unavailable models."""
    unavailable = unavailable or set()
    seen: set[str] = set()
    cur: str | None = alias
    while cur is not None:
        if cur in seen:
            raise ResolutionError(f"fallback cycle at {cur!r}")
        seen.add(cur)
        entry = catalogue.model(cur)
        if entry.provider_key() not in unavailable:
            return entry
        fb = entry.fallbacks
        cur = fb[0] if fb else None
    raise ResolutionError(
        f"no available model for alias {alias!r} (all fallbacks unavailable)"
    )


def resolve_call_params(
    catalogue: Catalogue,
    role: str,
    *,
    runtime_flags: dict[str, Any] | None = None,
    provider_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge call params by the explicit override cascade.

    Precedence (low to high): provider defaults < model ``params`` < runtime
    flags. The input mappings are never mutated; a fresh dict is returned.
    """
    entry = apply_fallbacks(catalogue, catalogue.role(role))
    provider = catalogue.providers[entry.provider]
    merged: dict[str, Any] = {}
    merged.update(provider_defaults or {})
    merged.update(provider.defaults)
    merged.update(entry.params)
    merged.update(runtime_flags or {})
    return merged


def hot_reload(loader: ModelCatalogueLoader) -> tuple[Catalogue, bool]:
    """Reload ``loader.path``; return ``(catalogue, changed)``.

    Atomic: a bad file raises :class:`CatalogueError` and the previous live
    catalogue is left untouched. ``changed`` is False when the file has not
    been modified since the last load.
    """
    candidate = load_catalogue(loader.path)  # raises CatalogueError on a bad file
    live = loader.live
    if live is not None and not loader._needs_reload(candidate):
        return live, False
    loader._live = candidate
    return candidate, True


class ModelCatalogueLoader:
    """Tracks a live catalogue for a single YAML path so hot-reload is atomic.

    ``load()`` performs the initial load. ``hot_reload()`` re-reads the file
    and only swaps the live catalogue when the file content has actually
    changed; a bad file leaves the live catalogue untouched.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._live: Catalogue | None = None
        self._last_bytes: bytes | None = None

    @property
    def live(self) -> Catalogue | None:
        return self._live

    def load(self) -> Catalogue:
        cat = load_catalogue(self.path)
        self._live = cat
        self._last_bytes = self.path.read_bytes()
        return cat

    def _needs_reload(self, candidate: Catalogue) -> bool:
        current_bytes = self.path.read_bytes()
        return current_bytes != self._last_bytes
