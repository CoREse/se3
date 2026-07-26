"""Preset prompt loader — dual-layer registry (built-in + project).

SE3 ships a small library of common-task *preset prompts* so that
recurring, standardized tasks (e.g. ``doc-sync``) need not be retyped
each time. Presets come from two layers:

- **Built-in layer** — markdown files under
  ``src/tianluo/templates/prompts/*.md``, shipped as package data and read
  at *runtime* from the installed package. They are NEVER copied into a
  project by ``luo init`` (which would re-introduce the copy-drift
  problem that plagued the legacy base-spec template); any project with
  luo installed gets the latest built-in presets with zero
  configuration. Their metadata (task type, etc.) is declared inside
  this module (:data:`_BUILTIN_PRESET_METADATA`).

- **Project layer** — markdown files under
  ``<project_root>/tianluo/prompts/*.md`` (committed with the project).
  Their metadata may be supplied / overridden via the ``presets:``
  section of ``tianluo.yaml``::

      presets:
        doc-sync:
          type: feature
          prompt_file: tianluo/prompts/doc-sync.md

The two layers are merged into a single registry. When the same preset
name exists in both layers, the **project layer overrides the built-in
layer**.

Lookup errors are explicit, never silent: requesting an unknown preset
raises with the list of available names, and a declared ``prompt_file``
that does not exist raises rather than being swallowed.
"""

from __future__ import annotations
from tianluo.runtime_paths import runtime_dir

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Union

# Default task type for a preset that declares no explicit type.
DEFAULT_PRESET_TYPE = "feature"

# Built-in preset metadata, keyed by preset name. The prompt *text* lives
# in ``src/tianluo/templates/prompts/<name>.md`` and is read at runtime; this
# dict only declares per-preset metadata (currently just the task type).
# A built-in markdown file with no entry here falls back to
# :data:`DEFAULT_PRESET_TYPE`.
_BUILTIN_PRESET_METADATA: Dict[str, Dict[str, str]] = {
    "doc-sync": {"type": "feature"},
}

LAYER_BUILTIN = "builtin"
LAYER_PROJECT = "project"

# Built-in prompts ship as package data alongside this module. Resolve
# the path relative to this file (NOT the cwd) so it works from an
# installed wheel — mirrors version_script_interface.py's template
# loading idiom and stays Python 3.8-safe.
_BUILTIN_PROMPTS_DIR = Path(__file__).parent / "templates" / "prompts"

# Project-local prompts live under this path, relative to project_root.
_PROJECT_PROMPTS_SUBDIR = "prompts"


class PresetError(Exception):
    """Base error for preset resolution problems."""


class PresetNotFoundError(PresetError):
    """Raised when a requested preset name is not present in the registry."""


@dataclass
class PresetEntry:
    """A single resolved preset registry entry.

    ``prompt_file`` is the on-disk path the prompt text will be read
    from; its existence is verified lazily by :func:`resolve` (so that
    :func:`list_presets` can enumerate declared-but-missing entries
    without raising).
    """

    name: str
    type: str
    prompt_file: Path
    layer: str


def _scan_builtin_presets() -> List[PresetEntry]:
    """Enumerate built-in presets from the packaged prompts directory."""
    entries: List[PresetEntry] = []
    if not _BUILTIN_PROMPTS_DIR.is_dir():
        return entries
    for md in sorted(_BUILTIN_PROMPTS_DIR.glob("*.md")):
        name = md.stem
        meta = _BUILTIN_PRESET_METADATA.get(name, {})
        entries.append(
            PresetEntry(
                name=name,
                type=meta.get("type", DEFAULT_PRESET_TYPE),
                prompt_file=md,
                layer=LAYER_BUILTIN,
            )
        )
    return entries


def _load_project_presets(project_root: Path) -> List[PresetEntry]:
    """Enumerate project-local presets.

    Built from two sources, in order:

    1. Markdown files under ``<project_root>/tianluo/prompts/*.md`` (the
       zero-config path — file stem is the preset name, default type).
    2. The ``presets:`` section of ``tianluo.yaml``, which overlays metadata
       (``type``) and may redirect a preset to a specific
       ``prompt_file`` path (relative to ``project_root``).
    """
    project_root = Path(project_root)
    entries: Dict[str, PresetEntry] = {}

    # 1. Scan tianluo/prompts/*.md for zero-config project presets.
    prompts_dir = runtime_dir(project_root) / _PROJECT_PROMPTS_SUBDIR
    if prompts_dir.is_dir():
        for md in sorted(prompts_dir.glob("*.md")):
            name = md.stem
            entries[name] = PresetEntry(
                name=name,
                type=DEFAULT_PRESET_TYPE,
                prompt_file=md,
                layer=LAYER_PROJECT,
            )

    # 2. Overlay tianluo.yaml presets: metadata.
    from .config import load_project_yaml

    data, _ = load_project_yaml(project_root)
    presets_cfg = data.get("presets") if isinstance(data, dict) else None
    if isinstance(presets_cfg, dict):
        for name, meta in presets_cfg.items():
            meta = meta if isinstance(meta, dict) else {}
            existing = entries.get(name)

            declared_type = meta.get("type")
            declared_file = meta.get("prompt_file")

            if declared_file:
                prompt_file = project_root / declared_file
            elif existing is not None:
                prompt_file = existing.prompt_file
            else:
                # Declared in tianluo.yaml with no prompt_file and no matching
                # tianluo/prompts/ file. Assume the conventional location;
                # resolve() will raise if it genuinely does not exist.
                prompt_file = prompts_dir / f"{name}.md"

            resolved_type = declared_type or (
                existing.type if existing is not None else DEFAULT_PRESET_TYPE
            )

            entries[name] = PresetEntry(
                name=name,
                type=resolved_type,
                prompt_file=prompt_file,
                layer=LAYER_PROJECT,
            )

    return list(entries.values())


def load_registry(project_root: Union[str, Path]) -> Dict[str, PresetEntry]:
    """Build the merged preset registry for ``project_root``.

    Built-in presets are loaded first, then project presets overlay
    them — so a project preset with the same name overrides the built-in
    one.
    """
    registry: Dict[str, PresetEntry] = {}
    for entry in _scan_builtin_presets():
        registry[entry.name] = entry
    for entry in _load_project_presets(Path(project_root)):
        registry[entry.name] = entry
    return registry


def list_presets(project_root: Union[str, Path]) -> List[PresetEntry]:
    """Return all available presets (built-in + project), sorted by name."""
    registry = load_registry(project_root)
    return [registry[name] for name in sorted(registry)]


def resolve(name: str, project_root: Union[str, Path]):
    """Resolve a preset name to ``(task_type, prompt_text, layer)``.

    Raises:
        PresetNotFoundError: if ``name`` is not in the registry; the
            message includes the list of available preset names.
        PresetError: if the resolved entry's ``prompt_file`` does not
            exist (declared but missing — never silently swallowed).
    """
    registry = load_registry(project_root)
    entry = registry.get(name)
    if entry is None:
        available = ", ".join(sorted(registry)) if registry else "(none)"
        raise PresetNotFoundError(
            f"Unknown preset '{name}'. Available presets: {available}"
        )
    if not entry.prompt_file.is_file():
        raise PresetError(
            f"Preset '{name}' ({entry.layer} layer) points to a missing "
            f"prompt file: {entry.prompt_file}"
        )
    prompt_text = entry.prompt_file.read_text(encoding="utf-8")
    return entry.type, prompt_text, entry.layer
