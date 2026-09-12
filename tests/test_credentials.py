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

import logging
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


def test_get_key_multi_alias_provider_does_not_crash(tmp_path: Path) -> None:
    """Regression (HIGH): a provider with 2+ stored aliases (UNIQUE is
    provider_id, model_alias) must not crash get_key — exact-alias lookups
    return the right key, and the no-alias fallback deterministically
    returns the alphabetically first alias's key."""
    store, _ = _store(tmp_path)
    store.store_key("trailopeners", "model-b", "sk-b-secret")
    store.store_key("trailopeners", "model-a", "sk-a-secret")

    # No alias: deterministic first-alias ("model-a" < "model-b"), no crash
    assert store.get_key("trailopeners") == "sk-a-secret"

    # Exact alias lookups
    assert store.get_key("trailopeners", "model-a") == "sk-a-secret"
    assert store.get_key("trailopeners", "model-b") == "sk-b-secret"

    # Unknown alias on a known provider: None, no crash
    assert store.get_key("trailopeners", "nonexistent-alias") is None


def test_get_key_missing_provider_returns_none(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    assert store.get_key("does-not-exist") is None


def test_get_key_missing_provider_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A simple missing-row lookup is normal/expected — it must not emit a
    WARNING (that's reserved for the decrypt-failure / rotated-key case, so
    on-call triage can distinguish the two by log level alone)."""
    store, _ = _store(tmp_path)
    with caplog.at_level(logging.WARNING, logger="d33d.security.credentials"):
        assert store.get_key("does-not-exist") is None
    assert not caplog.records


def test_get_key_multi_alias_fallback_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """model_alias=None on a multi-alias provider silently picks the
    alphabetically-first alias — that selection must be logged at WARNING
    (naming provider and the alias chosen, never key material) so a
    cross-model key mix-up isn't silent."""
    store, _ = _store(tmp_path)
    store.store_key("trailopeners", "model-b", "sk-b-secret")
    store.store_key("trailopeners", "model-a", "sk-a-secret")

    with caplog.at_level(logging.WARNING, logger="d33d.security.credentials"):
        assert store.get_key("trailopeners") == "sk-a-secret"

    assert len(caplog.records) == 1
    msg = caplog.records[0].message
    assert "trailopeners" in msg
    assert "model-a" in msg
    assert "sk-a-secret" not in msg


def test_get_key_single_alias_no_fallback_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A single-alias provider looked up with model_alias=None is the
    common case (not an ambiguous fallback) — no warning expected."""
    store, _ = _store(tmp_path)
    store.store_key("trailopeners", "only-model", "sk-secret")

    with caplog.at_level(logging.WARNING, logger="d33d.security.credentials"):
        assert store.get_key("trailopeners") == "sk-secret"

    assert not caplog.records


def test_get_key_exact_alias_lookup_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Passing an explicit model_alias is never a fallback — no warning."""
    store, _ = _store(tmp_path)
    store.store_key("trailopeners", "model-a", "sk-a-secret")
    store.store_key("trailopeners", "model-b", "sk-b-secret")

    with caplog.at_level(logging.WARNING, logger="d33d.security.credentials"):
        assert store.get_key("trailopeners", "model-a") == "sk-a-secret"

    assert not caplog.records


def test_master_key_rotation_reports_unusable_not_silent_loss(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    conn = _mkdb()
    key1 = cred.get_or_create_master_key(tmp_path / "master.key")
    store1 = cred.CredentialStore(conn=conn, master_key=key1)
    store1.store_key("p", "m", "some-secret")
    # Rotate: a fresh master key cannot read the old ciphertext; the row
    # must surface as unusable (None), not raise, not silently disappear.
    key2 = Fernet.generate_key()
    store2 = cred.CredentialStore(conn=conn, master_key=key2)
    with caplog.at_level(logging.WARNING, logger="d33d.security.credentials"):
        assert store2.get_key("p") is None
    # The row is still there — not silently lost
    rows = store2.list_credentials()
    assert any(r["provider_id"] == "p" for r in rows)

    # A decrypt failure (rotated/tampered MASTER_KEY) must be distinguishable
    # from a simple missing-row lookup via a WARNING naming provider/alias —
    # and must never contain the ciphertext or plaintext key material.
    assert len(caplog.records) == 1
    msg = caplog.records[0].message
    assert caplog.records[0].levelno == logging.WARNING
    assert "p" in msg
    assert "m" in msg
    assert "some-secret" not in msg
    assert "gAAA" not in msg


def test_master_key_file_permissions_restricted(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("chmod is POSIX-only")
    key_file = tmp_path / "master.key"
    cred.get_or_create_master_key(key_file)
    mode = key_file.stat().st_mode & 0o777
    assert mode == 0o600, f"MASTER_KEY file must be 0o600, got {oct(mode)}"


def test_master_key_no_leftover_tmp_file_after_success(tmp_path: Path) -> None:
    """The tmp-then-replace flow must not leave the .tmp sibling behind
    after a successful get_or_create_master_key call."""
    if os.name != "posix":
        pytest.skip("chmod is POSIX-only")
    key_file = tmp_path / "master.key"
    cred.get_or_create_master_key(key_file)
    tmp_file = key_file.with_suffix(key_file.suffix + ".tmp")
    assert not tmp_file.exists()


def test_master_key_survives_stale_tmp_file_from_crashed_run(tmp_path: Path) -> None:
    """A leftover .tmp file from a crashed prior run must not turn key
    generation into a hard failure (the O_EXCL create must tolerate/replace
    a stale tmp, not raise FileExistsError)."""
    if os.name != "posix":
        pytest.skip("chmod is POSIX-only")
    key_file = tmp_path / "master.key"
    tmp_file = key_file.with_suffix(key_file.suffix + ".tmp")
    tmp_file.write_bytes(b"stale-partial-write-from-a-crash")

    key = cred.get_or_create_master_key(key_file)

    assert key_file.exists()
    assert not tmp_file.exists()
    mode = key_file.stat().st_mode & 0o777
    assert mode == 0o600
    # Re-reading returns the same persisted key
    assert cred.get_or_create_master_key(key_file) == key


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
