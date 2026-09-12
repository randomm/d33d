"""Server-side Fernet credentials under a volume-persisted MASTER_KEY.

Issue #3 acceptance #3: "API keys are never present in any HTTP response
body or browser-reachable payload." This module enforces that invariant at
the data layer.

Security invariants:
- ``MASTER_KEY`` is generated once and persisted to a volume-mounted file.
  The file is created with ``0o600`` permissions so only the owner can read it.
- ``store_key`` encrypts the plaintext secret with Fernet under ``MASTER_KEY``
  and stores the ciphertext in the ``provider_credentials`` table.
- ``list_credentials`` returns provider and model names only — never the key,
  never the ciphertext. This is the endpoint-boundary method; any HTTP
  handler built on it cannot leak key material.
- ``get_key`` is server-internal only (used by the provider adapter to make
  authenticated LLM calls). It is not an endpoint — it is not in the
  credential store's public API surface for browser-facing code.
- A rotated or tampered ``MASTER_KEY`` reports the affected row as ``None``
  (unusable), never raises, never silently deletes the row.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class _CursorLike(Protocol):
    """Duck type for the object ``execute()`` returns: a cursor exposing
    row access. Both ``sqlite3.Cursor`` and ``d33d.db.Connection``'s
    ``execute()`` return value satisfy this structurally.
    """

    def fetchone(self) -> tuple | None: ...
    def fetchall(self) -> list[tuple]: ...


class _DBLike(Protocol):
    """Duck type: ``d33d.db.Connection`` or a bare ``sqlite3.Connection``.

    Both expose ``execute(sql, params) -> _CursorLike`` and ``commit()``,
    which is all ``CredentialStore`` needs. ``CredentialStore`` takes this
    protocol so tests can pass a raw ``sqlite3.Connection`` without a
    wrapper, and the production path passes the real ``d33d.db.Connection``.
    No isinstance check — the contract is the two methods.
    """

    def execute(self, sql: str, params: tuple) -> _CursorLike: ...
    def commit(self) -> None: ...


def get_or_create_master_key(path: Path) -> bytes:
    """Generate and persist a Fernet MASTER_KEY if the file does not exist.

    On first call: generates a new key, writes it to ``path`` with ``0o600``
    permissions, and returns it. On subsequent calls: reads and returns the
    existing key. The file is the volume-persistence boundary — the Docker
    compose file maps this path to a named volume so the key survives
    container rebuilds.

    The key is a 44-char url-safe base64 string (Fernet's native format).
    """
    if path.exists():
        raw = path.read_bytes()
        # Strip any trailing newline (some editors add one)
        raw = raw.strip()
        if not raw:
            raise ValueError(f"MASTER_KEY file {path} exists but is empty")
        return raw

    key = Fernet.generate_key()
    # Write atomically: write to temp then rename (avoids partial writes).
    # The temp file is created with 0o600 from the very first byte on disk
    # (O_CREAT|O_WRONLY, mode 0o600) rather than chmod-after-write, so the
    # Fernet key is never briefly world-readable under a permissive umask.
    # A stale tmp file from a crashed prior run is truncated and reused
    # rather than causing key generation to fail.
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(
        tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
    )
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # belt-and-suspenders vs. umask
    os.replace(tmp, path)
    return key


class CredentialStore:
    """Encrypted credential storage on top of ``d33d.db``'s ``provider_credentials`` table.

    Operates on the SAME table that ``d33d.db.Connection`` creates (schema owned by
    ticket #3-task-d, columns ``id, provider_id, model_alias, key_ciphertext``).
    ``CredentialStore`` does not invent its own schema or create any tables — it
    assumes the table already exists (created by ``d33d.db.connect()``). Pointing
    it at a database without that table raises ``sqlite3.OperationalError``.

    Usage (server-side only — never expose to browser-facing code):
    ```python
    from d33d import db
    conn = db.connect("/volume/d33d/d33d.sqlite3")
    store = CredentialStore(conn, master_key)   # conn is db.Connection
    store.store_key("trailopeners", "design-primary", "sk-...")
    rows = store.list_credentials()  # [{"provider_id": "trailopeners", "model_alias": "..."}]
    secret = store.get_key("trailopeners", "design-primary")  # "sk-..." (server-internal)
    ```

    ``model_alias`` may be omitted for a single-alias provider. On a
    multi-alias provider, omitting it falls back to the alphabetically
    first alias and logs a warning — pass ``model_alias`` explicitly
    whenever more than one alias may be stored for the same provider.

    The ``provider`` argument to ``store_key`` / ``get_key`` is the value stored
    in ``provider_id`` (the provider *name*, e.g. ``"trailopeners"``).
    ``model_name`` in the old API maps to ``model_alias`` in the real schema.
    """

    def __init__(self, conn: _DBLike, master_key: bytes) -> None:
        # _DBLike accepts db.Connection (production) or sqlite3.Connection (tests);
        # both expose execute/commit, which is all CredentialStore uses.
        self._conn = conn
        self._fernet = Fernet(master_key)

    def store_key(
        self,
        provider: str,
        model_name: str,
        secret: str,
    ) -> None:
        """Encrypt ``secret`` and store it for ``provider``.

        Raises ``ValueError`` if ``secret`` is empty (an empty key is
        meaningless and indicates a UI bug).
        """
        if not secret:
            raise ValueError("secret must be non-empty")
        ciphertext = self._fernet.encrypt(secret.encode("utf-8"))
        self._conn.execute(
            "INSERT INTO provider_credentials (provider_id, model_alias, key_ciphertext) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(provider_id, model_alias) DO UPDATE SET "
            "model_alias=excluded.model_alias, "
            "key_ciphertext=excluded.key_ciphertext",
            (provider, model_name, ciphertext),
        )
        self._conn.commit()

    def get_key(self, provider: str, model_alias: str | None = None) -> str | None:
        """Decrypt and return the key for ``provider``.

        If ``model_alias`` is given, the exact ``(provider, model_alias)`` row
        is looked up (0 or 1 row, guaranteed by the UNIQUE constraint).
        If ``model_alias`` is omitted, returns an arbitrary-but-deterministic
        (alphabetically first) alias's key, intended for the common
        single-alias-per-provider case. If the provider has more than one
        stored alias, a WARNING is logged naming the provider and the alias
        selected — never the key material.

        Returns ``None`` if the provider has no row, or if the ciphertext
        cannot be decrypted (rotated/tampered MASTER_KEY). Never raises on
        decryption failure — the invariant is that a bad key reports
        "unusable", not "crash". A decrypt failure logs a WARNING (naming
        provider/alias, never ciphertext or plaintext) so it's distinguishable
        from a simple missing-row lookup (which stays silent) during on-call
        triage.
        """
        if model_alias is None:
            rows = self._conn.execute(
                "SELECT model_alias, key_ciphertext FROM provider_credentials "
                "WHERE provider_id = ? ORDER BY model_alias LIMIT 2",
                (provider,),
            ).fetchall()
            row: tuple | None = rows[0] if rows else None
            if row is not None and len(rows) > 1:
                # Multiple aliases exist for this provider; the caller omitted
                # model_alias, so we deterministically picked the alphabetically
                # first one. Log which alias was selected — never the ciphertext —
                # so a cross-model key mix-up isn't silent.
                logger.warning(
                    "get_key(%r) called without model_alias on a provider with "
                    "multiple aliases; falling back to alphabetically-first "
                    "alias %r",
                    provider,
                    row[0],
                )
            resolved_alias = row[0] if row is not None else None
            ciphertext = row[1] if row is not None else None
        else:
            row = self._conn.execute(
                "SELECT key_ciphertext FROM provider_credentials "
                "WHERE provider_id = ? AND model_alias = ?",
                (provider, model_alias),
            ).fetchone()
            resolved_alias = model_alias
            ciphertext = row[0] if row is not None else None
        if ciphertext is None:
            return None
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except (InvalidToken, ValueError):
            # InvalidToken (ciphertext not decryptable with current key)
            # or ValueError (malformed ciphertext) → unusable row, report as None.
            # Log at WARNING (naming provider/alias, never ciphertext/plaintext)
            # so a rotated/tampered MASTER_KEY incident is distinguishable from
            # simple misconfiguration (missing row, which stays silent) during
            # on-call triage.
            logger.warning(
                "get_key(%r, %r) found a stored credential that could not be "
                "decrypted under the current MASTER_KEY (rotated or tampered "
                "key); reporting as unusable",
                provider,
                resolved_alias,
            )
            return None

    def list_credentials(self) -> list[dict[str, str]]:
        """Return provider and model names only — never key material.

        This is the endpoint-boundary method. The HTTP handler serialises
        the return value to JSON and sends it to the browser. No field in
        the returned rows contains the plaintext key or the ciphertext.

        Keys are named ``provider_id`` and ``model_alias`` to match the
        real ``d33d.db`` schema columns.
        """
        rows = self._conn.execute(
            "SELECT provider_id, model_alias FROM provider_credentials ORDER BY provider_id"
        ).fetchall()
        return [{"provider_id": r[0], "model_alias": r[1]} for r in rows]
