"""The part-import error classes (the leaf module).

Both error types live HERE (issue #395, lens-review cleanup): this is the
ONE place the classes are defined, so every module that raises or catches
them imports from here and the class identity is a single object (a
``try/except`` or ``isinstance`` in one module always matches a raise in
another — no re-binding, no ``sys.modules`` mutation, no ``__bases__``
re-base).

``d33d.part_mesh`` re-exports ``PartUploadError`` (``from d33d.part_errors
import PartUploadError``), so the historical
``from d33d.part_mesh import PartUploadError`` import keeps working
unchanged.
"""

from __future__ import annotations


class PartUploadError(ValueError):
    """A part upload failed the decode gate (unparseable, empty,
    non-finite, over the face cap, zip-bomb, or an unconvertible 3MF
    unit). The route maps it to the 422 with the verbatim
    ``partUpload.unparseable`` detail — nothing is persisted on the way
    out."""


class RepairTimeoutError(PartUploadError):
    """A typed repair-timeout signal (issue #395).

    The timeout is detected by TYPE, not by message: the route and the
    decode gate check ``isinstance(error, RepairTimeoutError)`` instead of
    matching the substring ``"timed out"`` in the message (a
    ``PartUploadError`` whose message happens to contain "timed out" is
    NOT a repair timeout — the type is the sole signal).
    """


__all__ = ["PartUploadError", "RepairTimeoutError"]
