"""Project runtime-directory resolution with legacy-name fallback.

Since the 12.0.0 rename (issue #270) the canonical runtime directory is
``tianluo/`` and the legacy one is ``se3/``. Resolution is per project
root, on every call:

1. ``<root>/tianluo/`` exists → use it (canonical);
2. otherwise ``<root>/se3/`` exists → use it (legacy layout keeps working
   through 12.x; a one-line migration hint is logged once per root per
   process — ``luo migrate run rename-to-tianluo`` moves a project over);
3. neither exists (fresh project) → the canonical ``tianluo``.

Deliberately *not* cached: ``luo migrate run rename-to-tianluo`` and tests mutate layouts
mid-process, and two ``is_dir()`` probes are far too cheap to earn a
staleness bug. Legacy fallback is removed in 13.0.0.
"""

import logging
from pathlib import Path
from typing import Set, Union

logger = logging.getLogger(__name__)

RUNTIME_DIR_NAME = "tianluo"
LEGACY_RUNTIME_DIR_NAME = "se3"

#: Runtime sub-directory the daemon lands web-UI attachments in. It lives here,
#: in the one module every layer may import cheaply, because three unrelated
#: layers need it: the daemon writes into it, ``luo init`` writes its gitignore
#: rule, and worktree creation seeds it into a new sandbox. Importing
#: ``tianluo.daemon.uploads`` for it would drag the resident control plane into
#: plain ``luo`` CLI startup, and a per-layer copy of the literal would drift.
UPLOADS_DIR_NAME = "uploads"

# Roots we already logged the migration hint for (avoid log spam from the
# daemon's polling loops). Process-local by design.
_HINTED_ROOTS: Set[str] = set()


def runtime_dir_name(project_root: Union[str, Path]) -> str:
    """Return the runtime directory *name* in effect under *project_root*."""
    root = Path(project_root)
    if (root / RUNTIME_DIR_NAME).is_dir():
        return RUNTIME_DIR_NAME
    if (root / LEGACY_RUNTIME_DIR_NAME).is_dir():
        key = str(root)
        if key not in _HINTED_ROOTS:
            _HINTED_ROOTS.add(key)
            logger.info(
                "using legacy runtime directory %s/ under %s — run "
                "`luo migrate run rename-to-tianluo` to move to %s/ (legacy fallback is "
                "removed in 13.0.0)",
                LEGACY_RUNTIME_DIR_NAME,
                root,
                RUNTIME_DIR_NAME,
            )
        return LEGACY_RUNTIME_DIR_NAME
    return RUNTIME_DIR_NAME


def runtime_dir(project_root: Union[str, Path]) -> Path:
    """Return the runtime directory *path* in effect under *project_root*."""
    root = Path(project_root)
    return root / runtime_dir_name(root)


def uploads_dir(project_root: Union[str, Path]) -> Path:
    """Return the web-UI attachments directory in effect under *project_root*.

    Resolved through :func:`runtime_dir` rather than hard-coding ``tianluo/``:
    a project still on the legacy ``se3/`` layout would otherwise get a stray
    top-level directory that no gitignore rule covers, and that stray directory
    would then be committed by accident.
    """
    return runtime_dir(project_root) / UPLOADS_DIR_NAME


def runtime_relpath(project_root: Union[str, Path], *parts: str) -> Path:
    """Return a *relative* runtime path (``tianluo/...`` or ``se3/...``).

    For call sites that hand relative paths to git or compare against
    ``git status`` output, where the leading directory name must match the
    project's actual layout.
    """
    return Path(runtime_dir_name(project_root)).joinpath(*parts)


def dual_runtime_glob(base: Path, prefix: str, tail: str):
    """Glob ``<prefix><runtime>/<tail>`` for both runtime dir names.

    Canonical ``tianluo/`` results first, then legacy ``se3/`` — used where a
    scan must see runtime state regardless of which layout a (worktree)
    checkout carries during the 12.x transition window.
    """
    results = list(base.glob(f"{prefix}{RUNTIME_DIR_NAME}/{tail}"))
    results.extend(base.glob(f"{prefix}{LEGACY_RUNTIME_DIR_NAME}/{tail}"))
    return results
