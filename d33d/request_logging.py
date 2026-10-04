"""Design-path ``request_logs`` writer (issue #355).

One home for the production design closure's request-log wrapper: every
design-path LLM invocation writes exactly one ``request_logs`` row. The
question path's ``conn.log_request`` call in ``d33d.app`` is the reference
row shape; the design path differs in one load-bearing way — the loop may
run on a worker thread (``asyncio.to_thread`` in
``d33d.design_loop_events``), and ``app.state.conn`` is a
``check_same_thread=True`` handle: a direct call from the worker raises
``sqlite3.ProgrammingError`` and would silently zero every design row. So
the write ALWAYS goes through a short-lived connection to ``db_path``
(closed after the insert).

The row's model fields come from ``resolve_model`` for the call's OWN role
(a critique row carries the critique model, not the design one);
``provider`` is the provider NAME — the key never reaches the row. A
resolution or write failure logs a warning and returns: a logging failure
never fails the design loop.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# The thread that owns ``app.state.conn`` — created on the main thread at
# startup / lifespan. Cached at module import (the import always runs on
# main); stable for the process lifetime.
_MAIN_THREAD_IDENT = threading.get_ident()


def log_design_request(
    app_state: Any,
    catalogue: Any,
    role: str,
    *,
    status: str,
    project_id: Any,
    latency_ms: int,
    prompt_hash: str,
    usage: Any,
) -> None:
    """Write one ``request_logs`` row for a design-path LLM call.

    ``status`` is the row's verbatim status — ``result.status`` for a
    returned ``LLMResult`` (``"ok"``/``"error"``) or ``SenderError.status``
    (``"error"`` / ``"no_tools_supported"`` — never a fabricated value).
    ``usage`` is the ``LLMResult.usage`` dict (``None``/absent fields
    land as zero tokens).
    """
    from d33d import db as db_mod
    from d33d.config.catalogue import ResolutionError
    from d33d.config.resolve import resolve_model

    try:
        res = resolve_model(catalogue, role)
    except (ResolutionError, KeyError, AttributeError):
        logger.warning(
            "design request_logs: could not resolve role %r — row "
            "not written",
            role,
        )
        return
    prompt_tokens = usage.get("prompt_tokens", 0) if usage else 0
    completion_tokens = usage.get("completion_tokens", 0) if usage else 0
    if not isinstance(prompt_tokens, int):
        prompt_tokens = 0
    if not isinstance(completion_tokens, int):
        completion_tokens = 0
    db_path = getattr(app_state, "db_path", None)
    if db_path is None:
        return

    def _write(conn: Any) -> None:
        conn.log_request(
            project_id=project_id,
            model_alias=res.entry.id,
            model_id=res.entry.model,
            provider=res.provider.name,
            role=role,
            status=status,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            prompt_hash=prompt_hash,
        )

    # The app's own handle is only safe on the thread that created it
    # (``check_same_thread=True`` is the default): an open, same-thread,
    # file-backed ``app.state.conn`` is reused (the question-path
    # pattern); anything else — a cross-thread call (the design loop's
    # worker thread), a closed/dead handle, or a ":memory:" db (whose
    # short-lived connection would be an empty database) — takes the
    # short-lived file connection (the operator decision's default for
    # the design path).
    own = getattr(app_state, "conn", None)
    own_is_file = not (isinstance(db_path, str) and db_path == ":memory:")
    if (
        own is not None
        and not own.closed
        and own_is_file
        and threading.get_ident() == _MAIN_THREAD_IDENT
    ):
        # Same thread as the connection was created on — safe.
        try:
            _write(own)
        except Exception:  # noqa: BLE001 — a logging failure must
            # never fail the design loop; the warning IS the
            # observability.
            logger.warning(
                "design request_logs: failed to write row for role %r "
                "status %s",
                role,
                status,
            )
        return
    if not own_is_file:
        # A ":memory:" DB the short-lived path cannot reach and the
        # app's handle is unusable (closed / cross-thread): skip the
        # row (test-seam-only shape — production is always
        # file-backed).
        return
    conn = db_mod.connect(db_path)
    try:
        _write(conn)
    except Exception:  # noqa: BLE001 — a logging failure must never
        # fail the design loop; the warning IS the observability.
        logger.warning(
            "design request_logs: failed to write row for role %r "
            "status %s",
            role,
            status,
        )
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110 — a failed close
            # carries no new information (the write failure, if
            # any, is already warned above).
            pass


def make_logged_llm_fn(llm_fn: Any, app_state: Any, catalogue: Any, project_id: Any) -> Any:
    """Wrap a design-path ``llm_fn`` so every call writes one row.

    A ``SenderError`` writes a row with the exception's ``status`` and an
    empty hash, then re-raises — the loop's behaviour on the error is
    unchanged (degrade or fail-fast path).
    """
    from d33d.design_llm import SenderError

    async def _logged_llm_fn(
        role: str, messages: list[dict[str, Any]], system: str | None
    ) -> Any:
        import time

        t0 = time.monotonic()
        try:
            result = await llm_fn(role, messages, system)
        except SenderError as exc:
            log_design_request(
                app_state,
                catalogue,
                role,
                status=exc.status,
                project_id=project_id,
                latency_ms=int((time.monotonic() - t0) * 1000),
                prompt_hash="",
                usage=None,
            )
            raise
        usage = getattr(result, "usage", None) or {}
        log_design_request(
            app_state,
            catalogue,
            role,
            status=result.status or "ok",
            project_id=project_id,
            latency_ms=int((time.monotonic() - t0) * 1000),
            prompt_hash=getattr(result, "prompt_hash", None) or "",
            usage=usage,
        )
        return result

    return _logged_llm_fn
