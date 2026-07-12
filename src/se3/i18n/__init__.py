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


def _decide_explicit(raw: Optional[str]) -> Optional[str]:
    """Resolve an *explicit* se3 language request (env var / config value).

    Distinguishes "unset" from "set but unsupported": an unset/empty value
    returns ``None`` so resolution falls through to the next tier, but a value
    that is *present yet unsupported* is an explicit request for a language we
    do not ship, so it resolves to :data:`BASE_LANGUAGE` (en-US) here rather
    than leaking through to a lower-priority tier. Otherwise the higher-priority
    ``SE3_LANG=fr-FR`` would be silently overridden by a project ``zh-CN``.

    A non-string value counts as *set but unsupported* (YAML types ``language:
    NO`` as the boolean ``False``, and a bare code like ``language: 100`` as an
    int): it resolves to :data:`BASE_LANGUAGE` rather than falling through, so an
    explicit-but-invalid config value never lets a lower tier (e.g. the system
    locale) pick the language behind the user's back.
    """
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    return normalize_language(raw) or BASE_LANGUAGE


def _safe_cwd() -> Optional[Path]:
    """Return the process cwd, or ``None`` when it cannot be read.

    WHY: ``os.getcwd()`` raises ``FileNotFoundError`` on Linux once the process'
    cwd has been deleted (e.g. a worktree removed under a running flow by the
    merge orchestrator). :func:`t` must never raise on an output path, so an
    unreadable cwd is treated as 'no project root' and the resolution chain
    simply falls through to the global-config / system-locale / en-US tiers.
    """
    try:
        return Path.cwd()
    except OSError as exc:
        logger.debug("i18n: cwd unavailable: %s", exc)
        return None


def resolve_language(project_root: Optional[Path]) -> str:
    """Resolve the effective UI language via the precedence chain.

    The two explicit se3 tiers (``SE3_LANG`` and the merged config
    ``language.language``) are authoritative when *set*: a set-but-unsupported
    value resolves to :data:`BASE_LANGUAGE` rather than falling through, because
    the user explicitly requested a language. Only an unset value at a tier
    falls through to the next. When nothing is set, returns
    :data:`BASE_LANGUAGE`.

    ``SE3_LANG`` sits at the top so it transparently propagates the chosen
    language into ``--worktree`` subprocesses and daemon-spawned flows via
    normal environment inheritance, with no new CLI flag.
    """
    # 1. SE3_LANG explicit override (set-but-unsupported -> en-US).
    decided = _decide_explicit(os.environ.get("SE3_LANG"))
    if decided:
        return decided

    # 2/3. Project se3.yaml (merged with ~/.se3/config.yaml, project-first).
    # Import lazily to keep this module import-side-effect free and avoid any
    # import cycle with se3.config.
    try:
        from se3.config import LanguageConfig

        root = project_root if project_root is not None else _safe_cwd()
        if root is not None:
            decided = _decide_explicit(LanguageConfig.load(root).language)
            if decided:
                return decided
    except Exception as exc:  # config read must never break language resolution
        logger.debug("i18n: language config load failed: %s", exc)

    # 4. System locale — an OS hint, not an explicit se3 request, so an
    # unsupported locale falls through (to en-US below) rather than locking on.
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
        _current_language = resolve_language(_safe_cwd())
    return _current_language


def bind_project_root(project_root: Optional[Path]) -> str:
    """Re-resolve the active language against the project a command operates on.

    WHY: Typer renders command help strings through :func:`t` while it builds the
    command tree at *import* time, which fills the lazy singleton from
    ``Path.cwd()`` long before ``--project-root`` has been parsed. A command that
    explicitly targets another project would then keep rendering its runtime
    output in the cwd's language. CLI commands call this once they know their
    project root so the project's ``language.language`` tier wins over the lower
    global/system-locale tiers. ``SE3_LANG`` still outranks it — the chain is
    re-run in full, only with the correct root.
    """
    global _current_language
    root = Path(project_root) if project_root is not None else _safe_cwd()
    _current_language = resolve_language(root)
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
    except Exception:
        # Catalogs are translator-editable data: a template may carry attribute
        # or subscript field access ('{a.b}' / '{a[0]}'), which str.format turns
        # into AttributeError / TypeError rather than the KeyError-family errors
        # a plain placeholder mismatch raises. Any format failure degrades to the
        # raw template — the never-raises contract is what lets t() sit on every
        # output path of the flow engine.
        return template


def status_key(value) -> str:
    """Map a status token (or ``Enum``) to its catalog key.

    Status values are data tokens, not identifiers: they carry hyphens,
    apostrophes and spaces (``in-progress``, ``won't-fix``). Normalizing them to
    a single ``status.<snake_case>`` shape lets one catalog namespace serve the
    issue, flow and step status vocabularies, which overlap (``completed``,
    ``failed``, ``paused``) and mean the same thing in each.
    """
    raw = value.value if hasattr(value, "value") else str(value)
    slug = "".join(
        ch if ch.isalnum() else "_" if ch in "-_ " else "" for ch in raw.strip().lower()
    )
    return f"status.{slug}"


def t_status(value) -> str:
    """Translate a status token for display, falling back to the raw token.

    WHY the fallback differs from :func:`t`: a status value comes from data (an
    engine.json field, an issue YAML), not from a fixed set of call sites, so an
    unknown token has no catalog entry by design. Rendering the raw token
    ("unknown", a status added by a newer engine) is meaningful to the user,
    whereas t()'s key-echo fallback would print the useless literal
    ``status.unknown``.
    """
    raw = value.value if hasattr(value, "value") else str(value)
    key = status_key(value)
    rendered = t(key)
    return raw if rendered == key else rendered


__all__ = [
    "t",
    "t_status",
    "status_key",
    "set_language",
    "get_language",
    "bind_project_root",
    "reset_language",
    "resolve_language",
    "supported_languages",
    "clear_caches",
    "BASE_LANGUAGE",
]
