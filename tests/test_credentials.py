"""Unit tests: server-side Fernet credentials under a volume-persisted MASTER_KEY.

Security invariant (issue #3 acceptance #3): the provider API key never
reaches any browser-reachable payload. ``CredentialStore.list_credentials``
returns provider and model names only — key material must be impossible to
surface. The MASTER_KEY bootstrap must generate-and-persist once; a rotated
or tampered key must report the affected row as unusable, not silently lose
it and not raise.

No Docker, no network — pure local file/DB semantics (sqlite3 :memory:).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from d33d import db as _db
from d33d.security import credentials as cred


def _mkdb() -> _db.Connection:
    """A real ``db.connect()``-created in-memory database (the SAME table
    CredentialStore operates on — not a hand-rolled schema)."""
    return _db.connect(":memory:")


def _master_key_file(tmp_path: Path) -> Path:
    key_file = tmp_path / "master.key"
    cred.get_or_create_master_key(key_file)
    return key_file


def _store(tmp_path: Path) -> tuple[cred.CredentialStore, bytes]:
    key_file = tmp_path / "master.key"
    key = cred.get_or_create_master_key(key_file)
    return cred.CredentialStore(conn=_mkdb(), master_key=key), key


def test_master_key_generates_and_persists_once(tmp_path: Path) -> None:
    key_file = tmp_path / "master.key"
    k1 = cred.get_or_create_master_key(key_file)
    k2 = cred.get_or_create_master_key(key_file)
    assert k1 == k2
    assert len(k1) > 0
    assert key_file.exists()


def test_store_and_round_trip_decrypt(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    secret = "sk-trailopeners-super-secret-12345"
    store.store_key("trailopeners", "RedHatAI/Qwen3.8-27B-INT4", secret)
    assert store.get_key("trailopeners") == secret


def test_list_returns_names_only_no_key_material(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    secret = "sk-super-secret-key-abc"
    store.store_key("trailopeners", "RedHatAI/Qwen3.8-27B-INT4", secret)
    rows = store.list_credentials()
    assert secret not in str(rows)
    assert rows[0]["provider_id"] == "trailopeners"
    assert rows[0]["model_alias"] == "RedHatAI/Qwen3.8-27B-INT4"
    # No field name containing "key" in the row
    for field_name in rows[0]:
        assert "key" not in field_name.lower(), (
            f"key material field name in list response: {field_name!r}"
        )


def test_overwrite_replaces_prior_ciphertext(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    store.store_key("p", "m", "first-key")
    store.store_key("p", "m", "second-key")
    assert store.get_key("p") == "second-key"


def test_get_key_missing_provider_returns_none(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    assert store.get_key("does-not-exist") is None


def test_master_key_rotation_reports_unusable_not_silent_loss(tmp_path: Path) -> None:
    conn = _mkdb()
    key1 = cred.get_or_create_master_key(tmp_path / "master.key")
    store1 = cred.CredentialStore(conn=conn, master_key=key1)
    store1.store_key("p", "m", "some-secret")
    # Rotate: a fresh master key cannot read the old ciphertext; the row
    # must surface as unusable (None), not raise, not silently disappear.
    key2 = Fernet.generate_key()
    store2 = cred.CredentialStore(conn=conn, master_key=key2)
    assert store2.get_key("p") is None
    # The row is still there — not silently lost
    rows = store2.list_credentials()
    assert any(r["provider_id"] == "p" for r in rows)


def test_master_key_file_permissions_restricted(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("chmod is POSIX-only")
    key_file = tmp_path / "master.key"
    cred.get_or_create_master_key(key_file)
    mode = key_file.stat().st_mode & 0o777
    assert mode == 0o600, f"MASTER_KEY file must be 0o600, got {oct(mode)}"


def test_store_key_with_empty_secret_rejected(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    with pytest.raises(ValueError):
        store.store_key("p", "m", "")


def test_round_trip_through_real_db_connection_schema(tmp_path: Path) -> None:
    """Regression (HIGH #3): store_key → get_key → list_credentials round-trips
    through a REAL ``db.connect()``-created database (not a hand-rolled schema),
    proving CredentialStore and d33d.db agree on the ``provider_credentials``
    table shape (columns ``id, provider_id, model_alias, key_ciphertext``).
    """
    conn = _db.connect(":memory:")
    try:
        key = cred.get_or_create_master_key(tmp_path / "master.key")
        store = cred.CredentialStore(conn=conn, master_key=key)

        # Round-trip through the real schema
        secret = "sk-real-round-trip-abc123"
        store.store_key("trailopeners", "RedHatAI/Qwen3.8-27B-INT4", secret)
        assert store.get_key("trailopeners") == secret

        # Overwrite (ON CONFLICT on the real UNIQUE(provider_id, model_alias))
        store.store_key("trailopeners", "RedHatAI/Qwen3.8-27B-INT4", "sk-rotated")
        assert store.get_key("trailopeners") == "sk-rotated"

        # Multiple providers
        store.store_key("provider-b", "model-b", "sk-b")

        # list_credentials returns names only, no key material
        rows = store.list_credentials()
        assert len(rows) == 2
        # No field name containing "key" in any row
        for row in rows:
            for field_name in row:
                assert "key" not in field_name.lower()
            # Fields match the real schema column names
            assert "provider_id" in row
            assert "model_alias" in row

        # The names-only view must not leak ciphertext
        for row in rows:
            for v in row.values():
                assert not (isinstance(v, str) and v.startswith("gAAA"))
    finally:
        conn.close()
