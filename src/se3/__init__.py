"""Deprecated compatibility shim: the ``se3`` package was renamed ``tianluo``.

Importing ``se3`` aliases the package onto :mod:`tianluo`, and a meta-path
finder maps every ``se3.<submodule>`` import to the *same* module object as
its ``tianluo.<submodule>`` counterpart — not a re-executed copy — so
module-level state (caches, locks, registries) is never duplicated between
the two names during the 12.x transition window. Removed in 13.0.0.
"""

import importlib
import importlib.abc
import importlib.util
import sys
import warnings

_REAL_PACKAGE = "tianluo"


class _AliasLoader(importlib.abc.Loader):
    """Loader that resolves an aliased name to the already-real module.

    ``create_module`` returns the imported ``tianluo.*`` module itself;
    ``exec_module`` is a no-op because that module is already executed.
    ``module_from_spec`` only fills missing attributes, so the real module's
    ``__name__`` / ``__spec__`` stay canonical (``tianluo.*``).
    """

    def __init__(self, real_name):
        self._real_name = real_name

    def create_module(self, spec):
        return importlib.import_module(self._real_name)

    def exec_module(self, module):
        pass


class _AliasFinder(importlib.abc.MetaPathFinder):
    """Meta-path finder aliasing ``se3.*`` imports onto ``tianluo.*``."""

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("se3."):
            return None
        real_name = _REAL_PACKAGE + fullname[len("se3"):]
        try:
            if importlib.util.find_spec(real_name) is None:
                return None
        except (ImportError, AttributeError, ValueError):
            return None
        return importlib.util.spec_from_loader(fullname, _AliasLoader(real_name))


warnings.warn(
    "the 'se3' package has been renamed to 'tianluo'; update imports — "
    "this compatibility shim is removed in 13.0.0",
    DeprecationWarning,
    stacklevel=2,
)

if not any(isinstance(finder, _AliasFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())

sys.modules[__name__] = importlib.import_module(_REAL_PACKAGE)
