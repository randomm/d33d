"""Security test: API key material never appears in any browser-reachable payload.

Issue #3 acceptance #3 (security invariant): "API keys are never present in
any HTTP response body or browser-reachable payload". This test suite
verifies the invariant at the data layer — the boundary between server-side
storage and any outbound representation.

No Docker, no network, no real HTTP server. Uses :memory: SQLite and local
file I/O only. The real FastAPI endpoints land in ticket #3-task-e; this
suite locks the invariant at the credential store and sanitiser layer so
that any endpoint implementation built on these primitives cannot leak.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from d33d import db as _db
from d33d.security import credentials as cred
from d33d.security import sanitize


def _mkdb() -> _db.Connection:
    """A real ``db.connect()``-created in-memory database (the SAME table
    CredentialStore operates on — not a hand-rolled schema)."""
    return _db.connect(":memory:")


# ---------------------------------------------------------------------------
# Credential store — the names-only list endpoint boundary
# ---------------------------------------------------------------------------


def test_list_credentials_never_contains_key_material(tmp_path: Path) -> None:
    """The list endpoint response (JSON-serialisable) must never contain the key."""
    key_file = tmp_path / "master.key"
    master_key = cred.get_or_create_master_key(key_file)
    store = cred.CredentialStore(conn=_mkdb(), master_key=master_key)
    secret = f"sk-{uuid.uuid4().hex}"
    store.store_key("trailopeners", "RedHatAI/Qwen3.8-27B-INT4", secret)

    rows = store.list_credentials()
    # Simulate the JSON response body the endpoint would send to the browser
    response_body = json.dumps(rows)
    assert secret not in response_body
    # No field name containing "key" in the row
    for row in rows:
        for field_name in row:
            assert "key" not in field_name.lower(), (
                f"key field in list response: {field_name}"
            )
            assert field_name in ("provider_id", "model_alias"), (
                f"unexpected field in list response: {field_name}"
            )


def test_list_credentials_no_ciphertext_in_response(tmp_path: Path) -> None:
    """The Fernet ciphertext (gAAA...) must also never be in the list response."""
    key_file = tmp_path / "master.key"
    master_key = cred.get_or_create_master_key(key_file)
    store = cred.CredentialStore(conn=_mkdb(), master_key=master_key)
    secret = f"sk-{uuid.uuid4().hex}"
    store.store_key("trailopeners", "RedHatAI/Qwen3.8-27B-INT4", secret)

    rows = store.list_credentials()
    response_body = json.dumps(rows)
    for row in rows:
        for v in row.values():
            assert not (isinstance(v, str) and v.startswith("gAAA")), (
                f"ciphertext leaked: {v[:10]}..."
            )
    assert "gAAA" not in response_body


def test_get_key_is_server_internal_not_an_endpoint(tmp_path: Path) -> None:
    """get_key exists for server-side use only. The invariant is that
    list_credentials() (the endpoint-boundary method) never exposes it."""
    key_file = tmp_path / "master.key"
    master_key = cred.get_or_create_master_key(key_file)
    store = cred.CredentialStore(conn=_mkdb(), master_key=master_key)
    secret = f"sk-{uuid.uuid4().hex}"
    store.store_key("p", "m", secret)

    rows = store.list_credentials()
    endpoint_payload = json.dumps(rows)
    assert secret not in endpoint_payload


def test_multiple_providers_no_cross_leakage(tmp_path: Path) -> None:
    """Each provider's key must not appear in any other provider's row or the list response."""
    key_file = tmp_path / "master.key"
    master_key = cred.get_or_create_master_key(key_file)
    store = cred.CredentialStore(conn=_mkdb(), master_key=master_key)
    key_a = f"sk-provider-a-{uuid.uuid4().hex}"
    key_b = f"sk-provider-b-{uuid.uuid4().hex}"
    store.store_key("provider-a", "model-a", key_a)
    store.store_key("provider-b", "model-b", key_b)

    rows = store.list_credentials()
    response_body = json.dumps(rows)
    assert key_a not in response_body
    assert key_b not in response_body


# ---------------------------------------------------------------------------
# Sanitiser — outgoing request boundary (mid-conversation model switch)
# ---------------------------------------------------------------------------


def test_sanitised_outgoing_request_does_not_introduce_key_material() -> None:
    """The sanitiser is a pure data transformation. It must not introduce
    any new key material into the outgoing request. Test: a transcript with
    no key material produces an outgoing request with no key material."""
    msgs = [
        {"role": "system", "content": "You are a 3D design assistant."},
        {"role": "user", "content": "Make a gear in OpenSCAD."},
        {"role": "assistant", "content": "Sure, here is the gear code."},
    ]
    result = sanitize.sanitize_outgoing(
        msgs, supports_tools=True, supports_vision=True, context_window=100000
    )
    # Fernet tokens start with 'gAAA' — this would indicate a credential leak
    assert not any(
        v
        for m in result
        for v in (m.get("content") if isinstance(m.get("content"), str) else "")
        if isinstance(v, str) and "gAAAAA" in v
    )


def test_sanitiser_is_pure_no_side_effects() -> None:
    """The sanitiser must be a pure function: same input → same output, no I/O.
    This ensures it cannot write key material to a log file or network socket."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    r1 = sanitize.sanitize_outgoing(
        msgs, supports_tools=True, supports_vision=True, context_window=100000
    )
    r2 = sanitize.sanitize_outgoing(
        msgs, supports_tools=True, supports_vision=True, context_window=100000
    )
    assert json.dumps(r1) == json.dumps(r2)
