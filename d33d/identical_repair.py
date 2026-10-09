"""Identical-repair stop state for the design loop (issue #432).

A post-check repair whose rendered mesh equals the PREVIOUS rendered
attempt's (same post-check reason) cannot move, so the loop stops instead
of repairing to an identical mesh until the budget runs out. The tracker
owns the two pieces of state that decision needs:

* the previous attempt's ``(post-check reason, geometry fingerprint)``;
* the last RENDERED attempt's record (the N-1 best reported on a repeat —
  a non-render attempt never overwrites it).

``reset()`` is called on a non-render attempt (the consecutive-renders
premise breaks) and clears only the post-check state, never the
last-rendered record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from d33d.evals.failure_capture import POST_CHECK_REASONS

if TYPE_CHECKING:
    from d33d.design_loop import IterationRecord

__all__ = ["IdenticalRepairTracker"]


class IdenticalRepairTracker:
    """Detect a post-check repeat across consecutive rendered attempts."""

    def __init__(self) -> None:
        self._prev_post: tuple[str, tuple[str, int]] | None = None
        self._prev_rendered: IterationRecord | None = None

    def observe_render(
        self,
        reason: str | None,
        fingerprint: tuple[str, int] | None,
        record: IterationRecord,
    ) -> IterationRecord | None:
        """Record a rendered attempt; return the N-1 record when it repeats.

        ``reason`` is the attempt's post-check reason (``None`` when no
        post-check fired) and ``fingerprint`` its rendered mesh's geometry
        fingerprint (only meaningful for a post-check reason). Returns the
        previous rendered record when this attempt repeats the previous
        post-check on an identical mesh, else ``None``.
        """
        repeat = False
        if reason in POST_CHECK_REASONS and fingerprint is not None:
            post: tuple[str, tuple[str, int]] = (reason, fingerprint)
            repeat = self._prev_post == post
            self._prev_post = post
        else:
            self._prev_post = None
        repeated = self._prev_rendered if repeat else None
        self._prev_rendered = record
        return repeated

    def reset(self) -> None:
        """A non-render attempt: clear the post-check state only."""
        self._prev_post = None
