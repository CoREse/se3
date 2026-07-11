"""Locale resource discovery, loading, and language-code normalization.

Language resources are flat ``key -> string`` JSON catalogs, one file per
locale, living in the sibling ``locales/`` directory. Adding a language is
therefore a pure data change: drop ``<code>.json`` into ``locales/`` and it is
auto-discovered by :func:`supported_languages` — no business code edit.

``en-US`` is the base language: it is expected to hold the full key set and is
used as the per-key fallback source by :func:`se3.i18n.t`.

Discovery/loading go through :mod:`importlib.resources` so they work identically
from a source checkout and from an installed wheel (the locales ship as package
data). Loading is fail-safe: a missing or malformed catalog yields ``{}`` rather
than raising, because UI-text lookup sits on every console output path and must
never break the ``se3 run`` state machine.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# The base/fallback language. Holds the full key set; every other locale falls
# back to it per-missing-key, and an unknown/unsupported code resolves here.
BASE_LANGUAGE = "en-US"

# Package that owns the JSON catalogs, resolved via importlib.resources so the
# same code path serves source checkouts and installed wheels.
_LOCALES_PACKAGE = "se3.i18n.locales"

_JSON_SUFFIX = ".json"


def _iter_locale_resources() -> Iterator[Tuple[str, object]]:
    """Yield ``(code, traversable)`` for every ``<code>.json`` in locales/.

    Uses ``importlib.resources.files`` (Python 3.9+) and falls back to a
    filesystem scan next to this module on 3.8, so discovery works both when
    installed as a wheel and from a source tree.
    """
    try:
        from importlib.resources import files as _files
    except ImportError:  # pragma: no cover - Python 3.8 only
        _files = None  # type: ignore[assignment]

    if _files is not None:
        try:
            root = _files(_LOCALES_PACKAGE)
        except (ModuleNotFoundError, FileNotFoundError):
            return
        for entry in root.iterdir():
            name = entry.name
            if name.endswith(_JSON_SUFFIX):
                yield name[: -len(_JSON_SUFFIX)], entry
        return

    # pragma: no cover - Python 3.8 fallback
    from pathlib import Path

    locales_dir = Path(__file__).resolve().parent / "locales"
    if locales_dir.is_dir():
        for path in sorted(locales_dir.glob("*" + _JSON_SUFFIX)):
            yield path.stem, path


def supported_languages() -> List[str]:
    """Return the sorted list of locale codes discovered in locales/.

    Intentionally uncached so that a language file added to ``locales/`` at
    runtime (or in a test) is picked up without a process restart — the
    zero-business-code extension contract.
    """
    return sorted({code for code, _res in _iter_locale_resources()})


@lru_cache(maxsize=None)
def load_catalog(code: str) -> Dict[str, str]:
    """Load the flat ``key -> string`` catalog for ``code``.

    Returns ``{}`` when the resource is absent, unreadable, malformed, or not a
    JSON object — never raises. Values are coerced to ``str`` so a stray
    non-string value in a catalog cannot blow up downstream ``.format`` calls.

    Cached per code; call :func:`clear_caches` after mutating locale files in a
    test.
    """
    for c, res in _iter_locale_resources():
        if c != code:
            continue
        try:
            text = res.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, ValueError) as exc:
            logger.warning("failed to load locale %r: %s", code, exc)
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "locale %r is not a JSON object (got %s); ignoring",
                code, type(data).__name__,
            )
            return {}
        return {str(k): str(v) for k, v in data.items()}
    return {}


def normalize_language(code: Optional[str]) -> Optional[str]:
    """Normalize a raw language/locale code to a supported code, or ``None``.

    Handles the shapes that reach us from config values and the system locale:
    ``zh_CN.UTF-8`` → ``zh-CN``, ``ZH-cn`` → ``zh-CN``, and a bare primary
    subtag ``zh`` → ``zh-CN`` via prefix match. Returns ``None`` for an
    unknown/unsupported code so callers can fall through the resolution chain
    (and ultimately to :data:`BASE_LANGUAGE`).
    """
    if not code:
        return None
    # Strip POSIX locale encoding / modifier suffixes: zh_CN.UTF-8@x -> zh_CN.
    raw = code.strip().split(".")[0].split("@")[0].replace("_", "-")
    if not raw:
        return None

    supported = supported_languages()
    lower_map = {s.lower(): s for s in supported}

    # Exact case-insensitive match (zh-CN, ZH-cn).
    if raw.lower() in lower_map:
        return lower_map[raw.lower()]

    # Prefix match on the primary subtag: a bare or foreign region code (zh,
    # zh-Hans) maps to the first supported locale sharing that primary subtag.
    primary = raw.split("-")[0].lower()
    for s in supported:
        if s.split("-")[0].lower() == primary:
            return s
    return None


def clear_caches() -> None:
    """Drop cached catalogs (test seam for locale-file mutations)."""
    load_catalog.cache_clear()
