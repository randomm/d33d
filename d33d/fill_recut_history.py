"""The design-loop chat history after the fill-and-recut pre-route (issue #437).

A fill-and-recut request ("make the top left hole 38 mm") on an imported,
usable part is answered by the pre-route and never reaches the design loop.
The SPA still echoes it in ``chat_history`` (user turns only), so a later
design request would hand the loop the declined size as if it were a design
request. :func:`loop_chat_history` drops those turns before the loop sees
them. A request whose noun the design itself owns (``own_names``) is not
pre-routed, so it stays.
"""

from __future__ import annotations

from collections.abc import Iterable

from d33d.fill_recut import fill_recut_trigger


def loop_chat_history(
    history: Iterable[str] | None,
    *,
    own_names: set[str] | frozenset[str] | None,
    part_usable: bool,
) -> tuple[str, ...]:
    """``history`` minus the fill-and-recut requests the pre-route answers.

    ``part_usable`` is False when no part is imported (the pre-route never
    runs, so nothing is dropped). ``own_names`` are the design's own feature
    nouns (a request on one of them is a design request and is kept).
    """
    turns = tuple(str(t) for t in (history or ()))
    if not part_usable:
        return turns
    own = own_names or frozenset()
    kept: list[str] = []
    for turn in turns:
        trigger = fill_recut_trigger(turn)
        if trigger is not None and trigger["noun"] not in own:
            continue
        kept.append(turn)
    return tuple(kept)
