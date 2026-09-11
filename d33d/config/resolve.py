"""Role -> alias -> provider resolution and the override cascade (A1).

This module is the swappability boundary: the rest of the backend references a
*role* (``design`` / ``critique`` / ``classification``) or a stable *alias*,
and this module maps it to a concrete, volatile provider model id + endpoint.
Swapping the model behind a role is a YAML edit + hot-reload — no code change.

The override cascade is explicit and unit-testable in isolation:

    runtime flags  >  model ``params``  >  provider defaults
"""

from __future__ import annotations

from d33d.config.catalogue import Catalogue, RoleResolution, resolve

__all__ = ["resolve_model"]


def resolve_model(
    catalogue: Catalogue,
    role: str,
    *,
    unavailable: set[str] | None = None,
) -> RoleResolution:
    """Resolve a role to a concrete model + provider.

    Alias/role resolution with ordered fallbacks. ``unavailable`` is an
    optional set of ``provider_key`` strings to treat as down.

    This is the public entry point the rest of the backend (and the SPA
    contract) should call. It is a thin, stable facade over
    :func:`d33d.config.catalogue.resolve` so the resolution contract has a
    single home module.
    """
    return resolve(catalogue, role, unavailable=unavailable)
