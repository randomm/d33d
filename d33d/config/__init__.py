"""d33d.config: swappable, file-driven model configuration (ticket #3).

The YAML catalogue is the source of truth. This subpackage exposes the core:
load/validate and atomic hot-reload (:mod:`~d33d.config.catalogue`), and
role->alias->provider resolution with the override cascade
(:mod:`~d33d.config.resolve`), plus capability probing (T0-T3)
(:mod:`~d33d.config.probes`) and the T1 fenced-JSON protocol
(:mod:`~d33d.config.t1_protocol`).
"""

from __future__ import annotations

from d33d.config.catalogue import (
    REQUIRED_ROLES,
    Catalogue,
    CatalogueError,
    ModelCatalogueLoader,
    ModelEntry,
    Provider,
    ResolutionError,
    Role,
    RoleResolution,
    apply_fallbacks,
    hot_reload,
    load_catalogue,
    resolve_call_params,
)
from d33d.config.resolve import resolve_model

__all__ = [
    "REQUIRED_ROLES",
    "Catalogue",
    "CatalogueError",
    "ModelCatalogueLoader",
    "ModelEntry",
    "Provider",
    "ResolutionError",
    "Role",
    "RoleResolution",
    "apply_fallbacks",
    "hot_reload",
    "load_catalogue",
    "resolve_call_params",
    "resolve_model",
]
