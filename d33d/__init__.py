"""d33d: parametric 3D design loop (OpenSCAD-driven).

Shell package — no application code yet. Later tickets add the render
worker (ticket #1), FastAPI backend (ticket #2), 3MF validation
(ticket #3), the design loop (ticket #4), the SPA (ticket #5), and
the eval harness (ticket #8).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("d33d")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
