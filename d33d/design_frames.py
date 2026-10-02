"""The design-loop's fixed copy.ts-verbatim notice strings (issue #332
fix round — the design_frames module).

``d33d.projects.post_chat`` (the missing-source pre-routes),
``d33d.design_loop_events`` (the lost-photo notice + the design-source
header), and the versioning tests all read these constants. They used to
live in ``d33d.projects``, which forced a lazy
``from d33d.projects import ...`` inside ``d33d.design_loop_events`` (a
module-load cycle risk: ``projects`` imports ``design_loop_events``).
This module is the neutral home — ``d33d.projects`` and
``d33d.design_loop_events`` import from it at module level, and
``d33d.projects`` re-exports the two reply constants under their
historical names (the versioning tests import them from
``d33d.projects``).
"""

from __future__ import annotations

# Issue #295 — the two fixed copy.ts strings the missing-storage chat
# pre-routes reply with (verbatim copies of the SPA's copy deck — the
# design-contract tripwire pins the two-way agreement, the #260 way).
SAVED_DESIGN_MISSING_REPLY = (
    "The saved design for this project is missing, so I can't change it. "
    "Start a new design, or describe it again and I'll make it fresh"
)
PHOTO_MISSING_NOTICE = (
    "Your reference photo for this project is missing, so I'm designing "
    "from your words alone"
)

__all__ = ["PHOTO_MISSING_NOTICE", "SAVED_DESIGN_MISSING_REPLY"]
