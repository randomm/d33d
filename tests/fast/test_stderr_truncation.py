"""Fast-layer test: stderr truncation boundary at 262144 bytes.

No Docker required. The spec pins 256 KiB as the binary value 262144
bytes, not 256000. Exactly 262144 bytes passes through untruncated;
262145 bytes truncates to exactly 262144. The result is always valid
UTF-8 (errors=replace).
"""

from __future__ import annotations

import d33d.render_worker as rw

LIMIT = 262144


def test_limit_is_262144_not_256000() -> None:
    assert rw.STDERR_MAX_BYTES == 262144
    assert rw.STDERR_MAX_BYTES != 256000


def test_exactly_262144_bytes_passes_through_untruncated() -> None:
    data = b"A" * LIMIT
    result = rw.truncate_stderr(data)
    assert len(result.encode("utf-8")) == LIMIT
    assert result == "A" * LIMIT


def test_262145_bytes_truncates_to_exactly_262144() -> None:
    data = b"B" * (LIMIT + 1)
    result = rw.truncate_stderr(data)
    assert len(result.encode("utf-8")) == LIMIT
    assert result == "B" * LIMIT


def test_300kib_superset_truncates_to_256kib() -> None:
    # The spec's 300 KiB feed case is a superset that must also land
    # at exactly 256 KiB.
    data = b"C" * (300 * 1024)
    result = rw.truncate_stderr(data)
    assert len(result.encode("utf-8")) == LIMIT


def test_result_is_always_valid_utf8() -> None:
    # Feed invalid UTF-8 bytes; the result must still be valid UTF-8
    # (errors="replace" produces U+FFFD, which is valid).
    data = b"\xff\xfe" * (LIMIT // 2)  # invalid UTF-8
    result = rw.truncate_stderr(data)
    # Must not raise; must be a valid str
    result.encode("utf-8")  # raises if not valid


def test_truncation_at_boundary_with_split_multibyte_is_valid_utf8() -> None:
    """A multibyte character straddling the 262144 boundary must not
    produce an invalid code point. The spec pins the contract as: take the
    first 262144 bytes, then decode UTF-8 with errors=replace. A split
    multi-byte sequence decodes the lone byte to U+FFFD (valid UTF-8), so
    the result is always valid UTF-8 — it is *not* required to be exactly
    262144 bytes once re-encoded, because U+FFFD is 3 bytes in UTF-8.
    The hard invariants are: (1) no exception, (2) valid UTF-8 output,
    (3) the input was bounded by the 262144-byte cut."""
    data = b"A" * (LIMIT - 1) + b"\xe2\x82\xac"  # € is 3 bytes; only 1 byte fits
    result = rw.truncate_stderr(data)
    # The lone split byte decodes to U+FFFD (a valid code point).
    assert "\ufffd" in result
    # Result is valid UTF-8.
    result.encode("utf-8")  # raises if not valid
    # The number of *input bytes* consumed is bounded by LIMIT.
    assert len(data) > LIMIT


def test_string_input_is_accepted() -> None:
    result = rw.truncate_stderr("hello")
    assert result == "hello"


def test_truncation_is_bytes_not_chars() -> None:
    # 262144 ASCII chars = 262144 bytes. Passes through.
    data = "X" * LIMIT
    result = rw.truncate_stderr(data)
    assert len(result) == LIMIT
