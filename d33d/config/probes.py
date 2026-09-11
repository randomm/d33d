"""Startup capability probes and tier selection for OpenAI-compatible endpoints.

"OpenAI-compatible" is a spectrum, not a boolean: image content-part format,
max images per request, native tool calling, and structured-output support all
vary per endpoint.  We *declare* capabilities in config, *validate* them with
three tiny startup probes (one 32x32 image, two images, one tool definition),
cache results by ``base_url`` + model hash, and drive tiered degradation:

- **T0** native tool calling + ``json_schema``
- **T1** constrained fenced-JSON text protocol with a corrective retry
- **T2** no vision
- **T3** regex-parsed fixed format

Probe failures never crash the app: an unreachable or erroring endpoint
degrades to the lowest viable tier (``T3``).
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

__all__ = [
    "CapabilityCache",
    "CapabilityResult",
    "Tier",
    "probe_capabilities",
    "select_tier",
    "tier_from_declared",
]


class Tier(str):
    """Degradation tiers, highest to lowest: T0 > T1 > T2 > T3."""

    T0 = "T0"
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


_TIER_ORDER = (Tier.T0, Tier.T1, Tier.T2, Tier.T3)


def tier_from_declared(
    *, tools: bool, json_schema: bool, vision: bool, fenced_json: bool = True
) -> str:
    """Map declared/validated capability axes to a tier.

    T0 needs native tool calling *and* json_schema.  T1 is the fenced-JSON
    text protocol, which must work even without native tools — but only when
    there is *any* validated capability to ride on.  T2 is text-only
    (vision without tools); T3 is the last-resort fixed format, selected when
    nothing was validated or enabled.
    """
    if tools and json_schema:
        return Tier.T0
    if fenced_json and (tools or vision):
        return Tier.T1
    if vision:
        return Tier.T2
    return Tier.T3


def _probe_image_b64() -> str:
    """A deterministic 32x32 RGB PNG encoded as a data URI (no PIL needed)."""
    import struct
    import zlib

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0)
    row = b"\x00" + b"\x00\x80\xff" * 32  # filter byte + 32 red pixels
    raw = zlib.compress(row * 32)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", raw)
        + _chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


@dataclass(frozen=True)
class CapabilityResult:
    """Validated capability axes for one ``base_url`` + model id pair.

    ``validated`` is True when at least the tool probe completed against a
    live endpoint; False when everything degraded (probe failure).
    """

    tools: bool
    json_schema: bool
    vision: bool
    max_images: int
    fenced_json: bool = True
    validated: bool = True

    @property
    def tier(self) -> str:
        if not self.validated and not (self.tools or self.vision):
            # Total probe failure: lowest viable tier, never raise.
            return Tier.T3
        return tier_from_declared(
            tools=self.tools,
            json_schema=self.json_schema,
            vision=self.vision,
            fenced_json=self.fenced_json,
        )


class CapabilityCache:
    """Probe results keyed by ``base_url`` + model hash.

    A model id change at the same base (or the same model under a new base)
    misses the cache and re-probes; a naive base_url-only cache would
    mis-tier new models.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], CapabilityResult] = {}

    def key(self, base_url: str, model_id: str) -> tuple[str, str]:
        return (base_url, hashlib.sha256(model_id.encode("utf-8")).hexdigest())

    def get(self, base_url: str, model_id: str) -> CapabilityResult | None:
        return self._entries.get(self.key(base_url, model_id))

    def put(self, base_url: str, model_id: str, result: CapabilityResult) -> None:
        self._entries[self.key(base_url, model_id)] = result

    def clear(self) -> None:
        self._entries.clear()


def select_tier(result: CapabilityResult) -> str:
    """Tier for a validated capability result (T0 > T1 > T2 > T3)."""
    return result.tier


async def probe_capabilities(
    *,
    base_url: str,
    model_id: str,
    declared: CapabilityResult | None = None,
    api_key: str = "",
    request_factory=None,
) -> CapabilityResult:
    """Run the three tiny probes and return validated capabilities.

    Probes: (1) one 32x32 image, (2) two images, (3) one tool definition.
    ``request_factory(request) -> response`` is the pluggable HTTP edge;
    ``response.ok`` / ``response.json()`` mirror httpx.  Any exception or
    non-OK response degrades that axis; total failure degrades to T3 without
    raising (a probe failure at startup must never crash the app).
    """
    if request_factory is None:
        raise TypeError("request_factory is required (no real HTTP in probes)")

    vision = False
    max_images = 0
    tools = False
    json_schema = False
    any_ok = False

    image_url = {"type": "image_url", "image_url": {"url": _probe_image_b64()}}

    # --- Probe 1: one image -------------------------------------------------
    try:
        resp = await request_factory(
            {
                "method": "POST",
                "url": f"{base_url.rstrip('/')}/chat/completions",
                "headers": {"Authorization": f"Bearer {api_key}"},
                "json": {
                    "model": model_id,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Describe this."},
                                image_url,
                            ],
                        }
                    ],
                },
            }
        )
        if resp.ok:
            any_ok = True
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            vision = content is not None and len(str(content).strip()) > 0
            max_images = 1 if vision else 0
    except Exception:
        logger.warning("probe 1 (one image) failed, degrading vision", exc_info=True)
        vision = False
        max_images = 0

    # --- Probe 2: two images (effective cap) --------------------------------
    if vision:
        try:
            resp = await request_factory(
                {
                    "method": "POST",
                    "url": f"{base_url.rstrip('/')}/chat/completions",
                    "headers": {"Authorization": f"Bearer {api_key}"},
                    "json": {
                        "model": model_id,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "Compare these."},
                                    image_url,
                                    image_url,
                                ],
                            }
                        ],
                    },
                }
            )
            if resp.ok:
                max_images = 2
            else:
                max_images = 1
        except Exception:
            logger.warning(
                "probe 2 (two images) failed, keeping cap at 1", exc_info=True
            )
            max_images = 1

    # --- Probe 3: one tool definition ---------------------------------------
    try:
        resp = await request_factory(
            {
                "method": "POST",
                "url": f"{base_url.rstrip('/')}/chat/completions",
                "headers": {"Authorization": f"Bearer {api_key}"},
                "json": {
                    "model": model_id,
                    "messages": [
                        {"role": "user", "content": "Call the tool."},
                    ],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "answer",
                                "description": "Return the answer",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                },
                            },
                        }
                    ],
                },
            }
        )
        if resp.ok:
            any_ok = True
            data = resp.json()
            message = data["choices"][0]["message"]
            if message.get("tool_calls"):
                tools = True
                json_schema = True
    except Exception:
        logger.warning("probe 3 (tool calling) failed, degrading", exc_info=True)

    result = CapabilityResult(
        tools=tools,
        json_schema=json_schema,
        vision=vision,
        max_images=max_images,
        validated=any_ok,
    )

    if declared is not None:
        # Declaration is the ceiling: a probe can only confirm or lower an
        # axis, never exceed what the operator declared.
        result = CapabilityResult(
            tools=tools and declared.tools,
            json_schema=json_schema and declared.json_schema,
            vision=vision and declared.vision,
            max_images=min(max_images, declared.max_images),
            fenced_json=declared.fenced_json,
            validated=any_ok,
        )

    return result
