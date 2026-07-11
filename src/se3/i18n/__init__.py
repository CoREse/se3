"""se3.i18n — internationalization of CLI / console UI text.

This package holds the *user-facing* text of the CLI behind flat ``key ->
string`` JSON catalogs (one per locale, under ``locales/``). Business code calls
:func:`t` with a stable key; the catalog for the active language supplies the
string, falling back per-key to ``en-US`` and finally to the key itself so a
missing translation is visible-and-fixable rather than a crash.

The active language is a lazily-resolved module-level singleton: the first
:func:`t`/:func:`get_language` call runs :func:`resolve_language` against the
current working directory and caches the answer. Resolution order is::

    SE3_LANG env
      > project se3.yaml / se3.local.yaml language.language (merged w/ global)
      > ~/.se3/config.yaml language.language
      > system locale (LC_ALL / LC_MESSAGES / LANG)
      > en-US

Resolution is lazy on purpose: cli.py's Typer callbacks run before any
project_root is derived, so reading config at import time would break test
isolation and the core/server import boundary. It also keeps ``import se3.i18n``
side-effect free — no config/filesystem read until the first render.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional

from .loader import (
    BASE_LANGUAGE,
    clear_caches,
    load_catalog,
    normalize_language,
    supported_languages,
)

logger = logging.getLogger(__name__)

# None means "not yet resolved" — the lazy singleton is filled on first use.
_current_language: Optional[str] = None

# System-locale env vars, most-specific first, consulted as the last config-less
# fallback before en-US.
_LOCALE_ENV_VARS = ("LC_ALL", "LC_MESSAGES", "LANG")


def resolve_language(project_root: Optional[Path]) -> str:
    """Resolve the effective UI language via the precedence chain.

    Each level contributes a value only when it resolves to a *supported*
    locale (via :func:`normalize_language`); an unset or unrecognized value at
    one level falls through to the next. When nothing matches, returns
    :data:`BASE_LANGUAGE`.

    ``SE3_LANG`` sits at the top so it transparently propagates the chosen
    language into ``--worktree`` subprocesses and daemon-spawned flows via
    normal environment inheritance, with no new CLI flag.
    """
    # 1. SE3_LANG explicit override.
    picked = normalize_language(os.environ.get("SE3_LANG"))
    if picked:
        return picked

    # 2/3. Project se3.yaml (merged with ~/.se3/config.yaml, project-first).
    # Import lazily to keep this module import-side-effect free and avoid any
    # import cycle with se3.config.
    try:
        from se3.config import LanguageConfig

        root = project_root if project_root is not None else Path.cwd()
        picked = normalize_language(LanguageConfig.load(root).language)
        if picked:
            return picked
    except Exception as exc:  # config read must never break language resolution
        logger.debug("i18n: language config load failed: %s", exc)

    # 4. System locale.
    for var in _LOCALE_ENV_VARS:
        picked = normalize_language(os.environ.get(var))
        if picked:
            return picked

    # 5. Base language.
    return BASE_LANGUAGE


def set_language(code: Optional[str]) -> str:
    """Explicitly set the active UI language; returns the effective code.

    A ``None``/unknown ``code`` selects :data:`BASE_LANGUAGE`. Primarily an
    override/reset seam for tests and for an eventual explicit-selection path.
    """
    global _current_language
    _current_language = normalize_language(code) or BASE_LANGUAGE
    return _current_language


def get_language() -> str:
    """Return the active UI language, resolving lazily on first access."""
    global _current_language
    if _current_language is None:
        _current_language = resolve_language(Path.cwd())
    return _current_language


def reset_language() -> None:
    """Clear the cached language selection so the next call re-resolves.

    Test seam: lets a test exercise the resolution chain from a clean state
    (pairs with :func:`se3.i18n.loader.clear_caches` for catalog mutations).
    """
    global _current_language
    _current_language = None


def t(key: str, **kwargs) -> str:
    """Translate ``key`` into the active language, formatting with ``kwargs``.

    Fallback chain: active-language catalog → ``en-US`` catalog → the key
    itself. If placeholder formatting fails (missing/extra ``kwargs``, bad
    format spec) the unformatted template is returned. This function never
    raises — UI-text rendering is on every output path and must not be able to
    interrupt the flow engine.
    """
    lang = get_language()
    template = load_catalog(lang).get(key)
    if template is None and lang != BASE_LANGUAGE:
        template = load_catalog(BASE_LANGUAGE).get(key)
    if template is None:
        # Neither the selected language nor the base has the key: surface the
        # raw key so the gap is visible in output and easy to grep/fix.
        template = key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


__all__ = [
    "t",
    "set_language",
    "get_language",
    "reset_language",
    "resolve_language",
    "supported_languages",
    "clear_caches",
    "BASE_LANGUAGE",
]
