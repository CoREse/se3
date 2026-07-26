"""SE 3.0 framework CLI tools."""


def _get_version() -> str:
    """Get version from package metadata (single source of truth: pyproject.toml)."""
    try:
        from importlib.metadata import version, PackageNotFoundError
        return version("tianluo")
    except (ImportError, PackageNotFoundError):
        # Fallback for Python < 3.8 or if package is not installed
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        try:
            from pathlib import Path
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
