"""Package entry point so ``python -m se3`` runs the CLI.

The daemon's spawner uses ``python -m se3 run ...`` as a robust fallback when
the ``se3`` console script is not discoverable on ``PATH``.
"""

from .cli import app

if __name__ == "__main__":  # pragma: no cover - thin shim
    app()
