"""d33d.security: server-side credential management and request sanitisation.

This subpackage owns the security invariants from issue #3:

- **Fernet credentials** under a volume-persisted ``MASTER_KEY`` — the API key
  never reaches any browser-reachable payload; ``list_credentials`` returns
  provider and model names only.
- **Outgoing-request sanitiser** for mid-conversation model switching — drops
  ``role:tool`` messages and ``tool_calls``/``tool_call_id`` for non-tool
  targets, strips ``image_url`` blocks to a text placeholder for non-vision
  targets, truncates oldest messages with a marker for smaller context windows.
  The stored transcript is never mutated.

No external dependencies beyond ``cryptography`` (Fernet) and the standard
library. No network, no Docker.
"""

from d33d.security.credentials import (
    CredentialStore,
    get_or_create_master_key,
)
from d33d.security.sanitize import sanitize_outgoing

__all__ = [
    "CredentialStore",
    "get_or_create_master_key",
    "sanitize_outgoing",
]
