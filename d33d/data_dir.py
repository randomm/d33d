"""Single test-mode guard for the operator's real ``~/.d33d`` data dir.

Every resolver of a ``~/.d3d``-rooted path (the ``projects/`` base, the
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

Escape hatch
------------
A test that INTENTIONALLY exercises a resolver against the real
``~/.d33d`` default (e.g. asserting the guard fires, or exercising the
env-default fallback in a spawned server) sets ``D33D_DATA_DIR_GUARD=off``
in the process (or the spawned child's) environment. The guard then
passes through untouched and the test's assertions on the guard's
``RuntimeError`` (or the resulting directory) are the pin. The
per-resolver "fires" tests in ``tests/test_db.py`` do exactly this
implicitly — they set ``D33D_DATA_DIR`` to the real home and expect the
guard to raise, which only happens because the escape hatch is unset.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["default_data_dir", "guard_real_data_path", "real_data_dir"]


def _pytest_run_in_progress() -> bool:
    """True while a pytest run is in progress and the guard is not
    explicitly disabled (``D33D_DATA_DIR_GUARD=off``)."""
    if os.environ.get("D33D_DATA_DIR_GUARD") == "off":
        return False
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _resolved(path: str | Path) -> Path | None:
    """``Path.resolve()`` on a path that does not yet exist is the norm for
    these resolvers (they ``mkdir`` after resolving), but on some
    platform configurations (a dangling symlink chain, an unreadable
    parent) it can raise ``OSError``. An unresolvable path cannot
    demonstrate containment in the real data dir, so treat it as
    "not inside" and pass through — the subsequent ``mkdir`` will
    surface the real error if the path is genuinely broken.
    """
    try:
        return Path(path).resolve()
    except OSError:
        return None


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
    when ``path`` resolves inside :func:`real_data_dir` and a pytest run
    is in progress (and the guard is not disabled via
    ``D33D_DATA_DIR_GUARD=off``). No-op otherwise — a resolved path
    outside the real data dir (the isolation fixture's tmp dir, an
    explicit test-owned path), a guard-disabled run, and every
    non-pytest run pass through untouched.
    """
    if not _pytest_run_in_progress():
        return
    resolved = _resolved(path)
    if resolved is None:
        return
    real = _resolved(real_data_dir())
    if real is None:
        return
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
        f"does this), or set D33D_DATA_DIR_GUARD=off if the test "
        f"intentionally exercises the real default."
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
