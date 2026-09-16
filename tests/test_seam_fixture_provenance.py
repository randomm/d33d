"""The fixture provenance test (issue #102): every committed wire fixture
carries its provenance metadata (the commit it was recorded at, the date,
the model/endpoint where relevant) so an undated, un-attributed fixture
cannot silently accumulate. Consumes the SAME loader as the replay tests
(``tests.fixtures.e2e.load_all_fixtures``) — one loader, one format.
"""

from __future__ import annotations

import re

from tests.fixtures.e2e import E2E_DIR, load_all_fixtures

#: The expected seams (the four recorded wire fixtures).
EXPECTED_SEAMS = {"A", "B", "C", "D"}


def _all_fixtures():
    return load_all_fixtures()


def test_fixtures_directory_contains_all_four_seams():
    """The ``tests/fixtures/e2e/`` directory contains all four seam
    fixtures (A/B/C/D) — a missing seam is a gap in the verification
    layer (the ticket exists to close gaps, not create new ones)."""
    fixtures = _all_fixtures()
    seams = {f.seam for f in fixtures}
    missing = EXPECTED_SEAMS - seams
    assert not missing, f"missing seam fixtures: {sorted(missing)}"


def test_each_fixture_carries_provenance_commit_and_date():
    """Every fixture carries ``provenance.commit`` (the ``git rev-parse
    HEAD`` at record time — a 40-char hex SHA) and ``provenance.date``
    (an ISO date). A fixture without provenance is the exact fiction this
    ticket exists to prevent (a hand-built stub with no attribution)."""
    for fixture in _all_fixtures():
        prov = fixture.provenance
        # commit: a 40-char hex SHA (the git rev-parse HEAD output).
        assert "commit" in prov, f"SEAM {fixture.seam}: provenance missing 'commit'"
        commit = prov["commit"]
        assert re.match(r"^[0-9a-f]{40}$", commit), (
            f"SEAM {fixture.seam}: provenance commit {commit!r} is not a 40-char hex SHA"
        )
        # date: an ISO date (YYYY-MM-DD).
        assert "date" in prov, f"SEAM {fixture.seam}: provenance missing 'date'"
        date = prov["date"]
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", date), (
            f"SEAM {fixture.seam}: provenance date {date!r} is not an ISO date"
        )


def test_each_fixture_carries_model_or_endpoint():
    """Every fixture carries ``provenance.model`` or ``provenance.endpoint``
    (where relevant — the model/endpoint that produced the payload). The
    fixtures record from stubs, but the stub's model/endpoint IS the
    provenance (it names the shape recorded)."""
    for fixture in _all_fixtures():
        prov = fixture.provenance
        assert "model" in prov or "endpoint" in prov, (
            f"SEAM {fixture.seam}: provenance missing both 'model' and 'endpoint'"
        )


def test_each_fixture_has_normalisation_note():
    """Every fixture carries a ``provenance.normalised`` list (what was
    normalised away for portability — absolute paths, uuids, temp dirs).
    A fixture with no normalisation note is either fully portable (the
    payload carries no machine-specific values) or the normalisation was
    not documented — both are acceptable, but the note is REQUIRED to be
    present (an empty list is a valid 'nothing normalised' note)."""
    for fixture in _all_fixtures():
        assert "normalised" in fixture.provenance, (
            f"SEAM {fixture.seam}: provenance missing 'normalised' list"
        )
        assert isinstance(fixture.provenance["normalised"], list), (
            f"SEAM {fixture.seam}: provenance 'normalised' is not a list"
        )


def test_fixtures_have_no_secrets():
    """The fixtures carry NO secrets: no API keys, no tokens, no
    ``Authorization`` headers. A grep for common secret PATTERNS (an API
    key is a 20+ char base64 token, a bearer token is a 20+ char string
    after ``Bearer``) across all fixture JSON must find nothing. The word
    'secret' alone is not a secret (it appears in the normalisation notes
    as 'not a secret'), so only the PATTERNS are checked."""
    secret_patterns = [
        r"sk-[A-Za-z0-9]{20,}",  # OpenAI-style API key
        r"Bearer\s+[A-Za-z0-9_.-]{20,}",  # Authorization bearer token
    ]
    for path in sorted(E2E_DIR.glob("*.json")):
        content = path.read_text(encoding="utf-8")
        for pattern in secret_patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            assert not match, (
                f"{path.name}: potential secret pattern {pattern!r} found"
            )


def test_fixture_payload_is_not_empty():
    """Every fixture carries a non-empty ``payload`` (the normalised wire
    payload) and a non-empty ``expected`` (the output the replay asserts
    on). An empty payload/expected is a fixture that proves nothing."""
    for fixture in _all_fixtures():
        assert fixture.payload, f"SEAM {fixture.seam}: payload is empty"
        assert fixture.expected, f"SEAM {fixture.seam}: expected is empty"
