"""Brain Core public package metadata."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("brain-core")
except PackageNotFoundError:  # pragma: no cover - only an unpackaged source import
    __version__ = "0+uninstalled"

__all__ = ["__version__"]
