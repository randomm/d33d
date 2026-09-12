"""Smoke test: package import + version constant.

Unmarked so it runs in the fast layer (`pytest -m "not slow"`) on any
machine including a laptop with no Docker installed. The `slow` marker
is declared in pyproject.toml; ticket #1 adds the Docker-dependent
tests that will carry it.
"""


def test_import_and_version() -> None:
    import importlib.metadata

    import d33d

    assert hasattr(d33d, "__version__")
    assert d33d.__version__ == importlib.metadata.version("d33d")
