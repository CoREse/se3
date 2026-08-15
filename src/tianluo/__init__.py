"""SE 3.0 framework CLI tools."""


def _get_version() -> str:
    """Get version from package metadata (single source of truth: pyproject.toml)."""
    try:
        from importlib.metadata import version, PackageNotFoundError
        return version("tianluo")
    except (ImportError, PackageNotFoundError):
        # Fallback for a source checkout where the package is not installed.
        # WHY: the TOML reader lookup lives inside the same guarded block as the
        # file read — tomllib is stdlib only on 3.11+ and tomli is not a declared
        # dependency, so on a supported older interpreter (requires-python >=3.9)
        # having neither must degrade to "unknown" rather than let
        # ModuleNotFoundError escape and break `import tianluo` entirely.
        try:
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib

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
