"""Single test-mode guard for the operator's real ``~/.d33d`` data dir.

Every resolver of a ``~/.d33d``-rooted path (the ``projects/`` base, the
``main`` data dir, the render-persistence base) routes through
:func:`real_data_dir` / :func:`guard_real_data_path` so the refusal
logic lives in exactly one place (issue #310).

The guard refuses a write into the real ``~/.d33d`` *while a pytest run
is in progress* (``PYTEST_CURRENT_TEST`` is set) — resolution is itself
the write for these resolvers (each ``mkdir``s on resolve, and the
``projects_dir()`` ``mkdir`` is the exact call chain that created the
546 orphan repos in the live data dir). Outside a pytest run the guard
is a no-op and production behaviour is unchanged.

The containment check targets the real user data dir specifically (the
default ``~/.d33d``, resolved via the real ``Path.home()`` at call
time, not a literal string), so:

* a test's isolated tmp data dir (the autouse ``_isolate_data_dir``
  fixture's ``tmp_path``/``d33d-data``) never trips it, and
* the render-worker host temp base ``~/d33d/render-tmp`` — a sibling
  dir that is NOT under ``~/.d33d`` — is never refused.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["default_data_dir", "guard_real_data_path", "real_data_dir"]


def real_data_dir() -> Path:
    """The operator's real default data dir: ``~/.d33d``.

    Resolved via the real ``Path.home()`` at call time. This is the
    containment target of :func:`guard_real_data_path` and the default
    for :func:`default_data_dir` — the single place the ``".d33d"``
    default is defined.
    """
    return Path.home() / ".d33d"


def guard_real_data_path(path: str | Path) -> None:
    """Refuse a resolved ``~/.d33d``-rooted path while pytest runs.

    Raises ``RuntimeError`` (naming the path and ``PYTEST_CURRENT_TEST``)
    when ``path`` resolves inside :func:`real_data_dir` and
    ``PYTEST_CURRENT_TEST`` is set. No-op otherwise — a resolved path
    outside the real data dir (the isolation fixture's tmp dir, an
    explicit test-owned path) and every non-pytest run pass through
    untouched.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    resolved = Path(path).resolve()
    real = real_data_dir().resolve()
    try:
        resolved.relative_to(real)
    except ValueError:
        return
    raise RuntimeError(
        f"refusing to resolve data dir {resolved}: it is inside the "
        f"operator's real {real} while a test run is in progress "
        f"(PYTEST_CURRENT_TEST={os.environ['PYTEST_CURRENT_TEST']!r}). "
        f"Point D33D_DATA_DIR at an isolated tmp dir "
        f"(the autouse _isolate_data_dir fixture in tests/conftest.py "
        f"does this)."
    )


def default_data_dir() -> Path:
    """``D33D_DATA_DIR`` (default ``~/.d33d``), expanded.

    The shared resolver for the env-defaulted data dir. Callers
    (``d33d.db.projects_dir``, ``d33d.main._resolve_data_dir``) steer it
    with an explicit ``data_dir`` argument before calling; this is only
    the env/default fallback path that the guard must cover.
    """
    p = Path(os.environ.get("D33D_DATA_DIR", str(real_data_dir()))).expanduser()
    guard_real_data_path(p)
    return p
