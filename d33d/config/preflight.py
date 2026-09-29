"""Model pre-flight: can the configured model actually be called?

Unlike the renderer pre-flight (a network-ish Docker probe), the model
pre-flight is a *pure config-resolution* check: a fresh
:func:`~d33d.config.catalogue.load_catalogue` + ``resolve_model`` for the
role + the resolved provider's key is non-empty. It deliberately runs no
network calls and keeps no cache, so an edited ``models.yaml`` is picked up
without a server restart — it runs before every design loop and every
model-backed question stage.

The key VALUE never appears in the result, in any exception message, or in
any log line; only the ``${ENV}`` variable NAME is reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d33d.config.catalogue import (
    Catalogue,
    CatalogueError,
    MissingEnvVarError,
    ResolutionError,
    load_catalogue,
)
from d33d.config.resolve import resolve_model


@dataclass(frozen=True)
class ModelPreflight:
    """The outcome of the model pre-flight for one role.

    ``ok`` is True only when the catalogue loaded, the role resolved to a
    model, and that model's provider has a non-empty key. ``env_var`` is the
    name of the missing (or empty) ``${ENV}`` variable when one is known —
    e.g. ``"TRAIL_OPENERS_LLM_KEY"`` — and ``None`` for an unresolved
    alias/role or a catalogue-level failure, where there is no single
    variable to name.
    """

    ok: bool
    env_var: str | None = None


def model_preflight(catalogue_path: Path | str, role: str) -> ModelPreflight:
    """Check that ``role`` resolves to a keyed model. No network, no cache.

    Fresh ``load_catalogue`` every call (no ``ModelCatalogueLoader`` reuse),
    so edits to the YAML file are visible without a restart.

    * missing or invalid catalogue file -> ``ok=False, env_var=None``
    * unset/empty ``${ENV}`` provider key -> ``ok=False`` with that name
    * role or alias that does not resolve -> ``ok=False, env_var=None``
    """
    try:
        cat = load_catalogue(catalogue_path)
    except MissingEnvVarError as e:
        return ModelPreflight(ok=False, env_var=e.var_name)
    except CatalogueError:
        return ModelPreflight(ok=False)
    return model_preflight_loaded(cat, role)


def model_preflight_loaded(cat: Catalogue | Any, role: str) -> ModelPreflight:
    """Check that ``role`` resolves to a keyed model on an ALREADY-LOADED
    catalogue (no fresh ``load_catalogue`` — the caller loaded it, so a
    monkeypatched ``load_catalogue`` is respected and no duplicate file
    read occurs).

    * unset/empty ``${ENV}`` provider key -> ``ok=False`` with that name
    * role or alias that does not resolve -> ``ok=False, env_var=None``
    """
    try:
        res = resolve_model(cat, role)
    except (CatalogueError, ResolutionError):
        return ModelPreflight(ok=False)
    if not res.provider.key:
        return ModelPreflight(ok=False)
    return ModelPreflight(ok=True)


__all__ = ["ModelPreflight", "MissingEnvVarError", "model_preflight", "model_preflight_loaded"]
