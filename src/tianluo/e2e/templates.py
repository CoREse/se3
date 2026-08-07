"""tianluo.e2e.templates — Dockerfile templates for the three service shapes.

An e2e service's image is always *base image + project environment layer*. This
module owns the base half: three parameterized Dockerfile templates covering the
shapes tianluo has to support, plus the renderer that expands a service's
declarative build steps into layers on top.

* ``base`` — plain CLI / web / API projects on a public base image
  (``python:3.12-slim``, ``node:22-bookworm-slim``, ``debian:stable-slim``).
* ``playwright`` — browser e2e, built on Playwright's official image so the
  browsers, system libraries and fonts are pinned.
* ``gui-xvfb`` — desktop GUI applications: a general base plus Xvfb, a light
  window manager, ``scrot`` and ``xdotool``.

WHY ``string.Template`` and not jinja2: these templates ship inside the wheel and
are rendered on a *core-only* install (the ``tianluo[e2e]`` extra isolates
third-party dependencies, never tianluo's own code — see
:mod:`tianluo.e2e.errors`). Pulling a template engine in for what amounts to
``$name`` substitution would put a third-party import on the core path for no
expressive gain.
"""

from __future__ import annotations

import logging
from string import Template
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from tianluo.i18n import t

from .errors import E2EConfigError

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_WORKDIR",
    "TEMPLATE_KINDS",
    "available_kinds",
    "expand_build_steps",
    "load_template",
    "render_dockerfile",
    "render_for_service",
]

# Package that owns the templates directory. Resolved through
# importlib.resources so a wheel and a source checkout take the same path — the
# templates are package data, not files sitting next to a module.
#
# WHY anchor at the *package* and traverse into ``templates/`` rather than
# treating ``tianluo.e2e.templates`` as an importable package: this module is
# itself named ``templates``, so a sibling directory carrying an ``__init__.py``
# would shadow it. The directory deliberately stays data-only.
_ANCHOR_PACKAGE = "tianluo.e2e"
_TEMPLATE_DIR = "templates"
_TEMPLATE_SUFFIX = ".Dockerfile.tmpl"

TEMPLATE_KINDS = ("base", "playwright", "gui-xvfb")

# Where the project source is bind-mounted inside every service container.
DEFAULT_WORKDIR = "/workspace"

# Dockerfile instructions a build step may start with to mean "emit me verbatim
# as a layer". Anything else is wrapped in `RUN`, which is what the overwhelming
# majority of declarative steps ("apt-get install -y curl") want.
_DOCKERFILE_INSTRUCTIONS = frozenset(
    {
        "ADD",
        "ARG",
        "CMD",
        "COPY",
        "ENTRYPOINT",
        "ENV",
        "EXPOSE",
        "HEALTHCHECK",
        "LABEL",
        "ONBUILD",
        "RUN",
        "SHELL",
        "STOPSIGNAL",
        "USER",
        "VOLUME",
        "WORKDIR",
    }
)

# Per-kind defaults, applied under the caller's context. A kind whose base image
# is part of its identity (playwright) carries a usable default; `base` and
# `gui-xvfb` are generic and require the project's stack to be named explicitly.
_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "base": {
        "workdir": DEFAULT_WORKDIR,
    },
    "playwright": {
        # Pinned rather than `:latest`: the browser build *and* the font set
        # decide what a screenshot looks like, and a floating tag would move a
        # visual baseline out from under the project silently.
        "base_image": "mcr.microsoft.com/playwright:v1.47.0-jammy",
        "workdir": DEFAULT_WORKDIR,
    },
    "gui-xvfb": {
        "workdir": DEFAULT_WORKDIR,
        "display": ":99",
        "screen": "1280x1024x24",
        "debug_tools": False,
    },
}

# The optional human-observation layer for the GUI template. Off by default:
# x11vnc/noVNC are useful when a person wants to watch a failing GUI scenario,
# but they are dead weight (and an open port) in an unattended fix loop.
_GUI_DEBUG_LAYER = """
# Optional human-observation stack: attach a VNC/noVNC viewer to watch a
# scenario run. Enabled via `debug_tools: true` in the service's template
# context; never on by default.
RUN apt-get update \\
    && apt-get install -y --no-install-recommends x11vnc novnc websockify \\
    && rm -rf /var/lib/apt/lists/*
EXPOSE 5900 6080
""".strip()

_GUI_DEBUG_LAYER_DISABLED = (
    "# Human-observation stack (x11vnc/noVNC) not requested: set\n"
    "# `debug_tools: true` on this service to build it in."
)


def available_kinds() -> List[str]:
    """Return the template kinds :func:`render_dockerfile` accepts."""
    return list(TEMPLATE_KINDS)


def _read_resource(name: str) -> Optional[str]:
    """Read ``templates/<name>`` as package data, or ``None`` if unreadable."""
    try:
        from importlib.resources import files as _files
    except ImportError:  # pragma: no cover - Python 3.8 only
        _files = None  # type: ignore[assignment]

    if _files is not None:
        try:
            resource = _files(_ANCHOR_PACKAGE).joinpath(_TEMPLATE_DIR, name)
            return resource.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except Exception as exc:  # pragma: no cover - depends on interpreter
            # files() has interpreter-dependent failure modes for a package it
            # cannot resolve; degrade to the filesystem scan rather than
            # enumerating them (same reasoning as tianluo.i18n.loader).
            logger.debug("importlib.resources could not resolve templates: %s", exc)

    # Fallback for Python 3.8 (no `files()`) and for a resolution failure above.
    from pathlib import Path  # pragma: no cover - fallback path

    candidate = Path(__file__).resolve().parent / _TEMPLATE_DIR / name
    if candidate.is_file():  # pragma: no cover - fallback path
        return candidate.read_text(encoding="utf-8")
    return None  # pragma: no cover - fallback path


def load_template(kind: str) -> str:
    """Return the raw template text for ``kind``.

    Raises :class:`~tianluo.e2e.errors.E2EConfigError` for an unknown kind, and
    for a known kind whose resource is missing from the installation (a broken
    wheel — the templates are supposed to ship unconditionally).
    """
    if kind not in TEMPLATE_KINDS:
        raise E2EConfigError(
            t(
                "e2e.templates.unknown_kind",
                kind=kind,
                known=", ".join(TEMPLATE_KINDS),
            )
        )
    text = _read_resource(kind + _TEMPLATE_SUFFIX)
    if text is None:
        raise E2EConfigError(t("e2e.templates.resource_missing", kind=kind))
    return text


def expand_build_steps(steps: Optional[Iterable[Any]]) -> str:
    """Expand declarative build steps into Dockerfile layers.

    INVARIANT: the declared order is preserved verbatim. BuildKit's cache is
    positional — invalidating step *n* invalidates every layer after it — so
    reordering (or "optimizing") steps here would silently turn an incremental
    rebuild into a full one and, worse, make two runs of the same configuration
    produce different caches.
    """
    if not steps:
        return ""
    lines: List[str] = []
    for raw in steps:
        step = str(raw).strip()
        if not step:
            continue
        head = step.split(None, 1)[0].upper()
        if head in _DOCKERFILE_INSTRUCTIONS:
            lines.append(step)
        else:
            lines.append("RUN " + step)
    return "\n".join(lines)


def _build_context(kind: str, context: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(_DEFAULTS.get(kind, {}))
    if context:
        merged.update(context)

    merged["build_steps"] = expand_build_steps(merged.get("build_steps"))
    if kind == "gui-xvfb":
        merged["debug_tools_layer"] = (
            _GUI_DEBUG_LAYER
            if merged.get("debug_tools")
            else _GUI_DEBUG_LAYER_DISABLED
        )
    return merged


def render_dockerfile(kind: str, context: Optional[Mapping[str, Any]] = None) -> str:
    """Render the ``kind`` template with ``context`` into Dockerfile text.

    ``context`` supplies at minimum ``base_image`` (except for ``playwright``,
    which defaults to a pinned official image) and optionally ``workdir`` and
    ``build_steps``; the GUI template additionally accepts ``display``,
    ``screen`` and ``debug_tools``.

    Raises :class:`~tianluo.e2e.errors.E2EConfigError` for an unknown kind or a
    context missing a placeholder the template requires — a build that failed
    at ``docker build`` time on an unsubstituted ``$base_image`` would be far
    harder to trace back to the configuration.
    """
    template = Template(load_template(kind))
    values = _build_context(kind, context)
    try:
        return template.substitute(values)
    except KeyError as exc:
        raise E2EConfigError(
            t("e2e.templates.missing_context", kind=kind, name=exc.args[0])
        ) from exc
    except ValueError as exc:
        raise E2EConfigError(
            t("e2e.templates.render_failed", kind=kind, detail=str(exc))
        ) from exc


def render_for_service(
    template: str,
    base_image: str,
    *,
    workdir: Optional[str] = None,
    build_steps: Optional[Sequence[str]] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> str:
    """Convenience wrapper mapping a :class:`~tianluo.e2e.backend.ServiceSpec`'s
    fields onto :func:`render_dockerfile`."""
    context: Dict[str, Any] = {
        "base_image": base_image,
        "build_steps": tuple(build_steps or ()),
    }
    if workdir:
        context["workdir"] = workdir
    if extra:
        context.update(extra)
    return render_dockerfile(template, context)
