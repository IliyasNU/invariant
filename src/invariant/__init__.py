"""Deterministic architecture checks for Python code."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("invariant")
except PackageNotFoundError:  # imported from a source tree that is not installed
    __version__ = "0.0.0"
