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

import os
import sqlite3
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


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
    # Write atomically: write to temp then rename (avoids partial writes)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(key)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    os.replace(tmp, path)
    return key


class CredentialStore:
    """Encrypted credential storage backed by a ``provider_credentials`` table.

    The table schema (owned by ticket #3-task-d):
    ```sql
    CREATE TABLE provider_credentials (
      provider TEXT PRIMARY KEY,
      model_name TEXT NOT NULL DEFAULT '',
      key_ciphertext BLOB NOT NULL
    )
    ```

    Usage (server-side only — never expose to browser-facing code):
    ```python
    key = get_or_create_master_key(Path("/volume/d33d/master.key"))
    store = CredentialStore(conn=sqlite3_conn, master_key=key)
    store.store_key("trailopeners", "RedHatAI/Qwen3.8-27B-INT4", "sk-...")
    rows = store.list_credentials()  # [{"provider": "trailopeners", "model_name": "..."}]
    secret = store.get_key("trailopeners")  # "sk-..." (server-internal)
    ```
    """

    def __init__(self, conn: sqlite3.Connection, master_key: bytes) -> None:
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
            "INSERT INTO provider_credentials (provider, model_name, key_ciphertext) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET model_name=excluded.model_name, "
            "key_ciphertext=excluded.key_ciphertext",
            (provider, model_name, ciphertext),
        )
        self._conn.commit()

    def get_key(self, provider: str) -> str | None:
        """Decrypt and return the key for ``provider``.

        Returns ``None`` if the provider has no row, or if the ciphertext
        cannot be decrypted (rotated/tampered MASTER_KEY). Never raises on
        decryption failure — the invariant is that a bad key reports
        "unusable", not "crash".
        """
        row = self._conn.execute(
            "SELECT key_ciphertext FROM provider_credentials WHERE provider = ?",
            (provider,),
        ).fetchone()
        if row is None:
            return None
        try:
            return self._fernet.decrypt(row[0]).decode("utf-8")
        except (InvalidToken, ValueError):
            # InvalidToken (ciphertext not decryptable with current key)
            # or ValueError (malformed ciphertext) → unusable row, report as None
            return None

    def list_credentials(self) -> list[dict[str, str]]:
        """Return provider and model names only — never key material.

        This is the endpoint-boundary method. The HTTP handler serialises
        the return value to JSON and sends it to the browser. No field in
        the returned rows contains the plaintext key or the ciphertext.
        """
        rows = self._conn.execute(
            "SELECT provider, model_name FROM provider_credentials ORDER BY provider"
        ).fetchall()
        return [{"provider": r[0], "model_name": r[1]} for r in rows]
