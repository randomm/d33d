"""Runtime entrypoint — bind the FastAPI app to a TCP port.

The default port is ``8080`` (per the ticket's "FastAPI app runs on port
8080" commitment); both port and host are env-configurable
(``D33D_HTTP_PORT`` / ``D33D_HOST``). The DB path, master-key path, and
catalogue path come from ``D33D_DATA_DIR`` (default ``~/.d33d``).

No test should exercise this module: ASGITransport runs the app in-process
without a socket, and a real ``uvicorn`` bind on 8080 in a test fails on
shared/CI machines. This is the process entrypoint only.
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from d33d.app import create_app

_DEFAULT_DATA_DIR = Path.home() / ".d33d"


def _resolve_data_dir() -> Path:
    """``D33D_DATA_DIR`` (default ``~/.d33d``), created if missing."""
    p = Path(os.environ.get("D33D_DATA_DIR", _DEFAULT_DATA_DIR))
    p.mkdir(parents=True, exist_ok=True)
    return p


def main() -> None:
    """Run the d33d FastAPI app on the configured port (default 8080)."""
    data_dir = _resolve_data_dir()
    app = create_app(
        data_dir / "d33d.sqlite3",
        master_key_path=data_dir / "master.key",
        catalogue_path=data_dir / "models.yaml",
    )
    port = int(os.environ.get("D33D_HTTP_PORT", "8080"))
    host = os.environ.get("D33D_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()


__all__ = ["main"]
