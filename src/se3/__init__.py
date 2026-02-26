"""SE 3.0 framework CLI tools."""

from pathlib import Path


def _get_version() -> str:
    """Read version from pyproject.toml - single source of truth."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    
    try:
        pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)
        return pyproject.get("project", {}).get("version", "unknown")
    except Exception:
        return "unknown"


# Single source of truth: pyproject.toml
__version__ = _get_version()

# Backward compatibility alias
SE3_FRAMEWORK_VERSION = __version__
