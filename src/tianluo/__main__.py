"""Package entry point so ``python -m tianluo`` runs the CLI.

The daemon's spawner uses ``python -m tianluo run ...`` as a robust fallback
when no console script (``luo`` / ``tianluo`` / legacy ``se3``) is
discoverable on ``PATH``.
"""

from .cli import app

if __name__ == "__main__":  # pragma: no cover - thin shim
    app()
