"""PreToolUse hook + controlled-plugin generator for spec-file write protection.

This module has two responsibilities and depends only on the standard library,
with **no import-time side effects**, so it can be imported freely by the engine
and run as a standalone hook subprocess (``python -m se3.engine.spec_write_hook``):

1. :func:`main` — the PreToolUse hook entry point. It reads the hook payload JSON
   from stdin, resolves the target file path, and *denies* any
   ``Write`` / ``Edit`` / ``NotebookEdit`` that targets a spec file under
   ``<cwd>/se3/specs/``. Every other write is allowed. The hook is deliberately
   **step-agnostic**: it always rejects spec writes and never inspects the step
   exemption set. Whether the hook is installed at all for a given step is decided
   upstream in ``llm_caller`` (only non-exempt steps receive the guard plugin via
   ``--plugin-dir``), so by the time the hook actually runs, a spec write is by
   definition illegal. A PreToolUse hook is *not* suppressed by
   ``--dangerously-skip-permissions``, so this is an enforcement layer the
   sub-agent cannot bypass.

2. :func:`ensure_guard_plugin` — generates/caches a minimal Claude *plugin*
   carrying only the PreToolUse hook and returns its directory, so ``llm_caller``
   can inject it through ``--plugin-dir <dir>``. This deliberately avoids a second
   ``--settings`` flag: Claude CLI treats a repeated ``--settings`` as a full
   *replacement* of the prior one (its "additional settings" merge semantics apply
   only across the user/project setting *sources*, not to a duplicated flag), so a
   guard passed via a second ``--settings`` silently clobbered the agent's own
   ``--settings opus.json`` — dropping its ``model`` back to the user settings. A
   ``--plugin-dir`` is a pure CLI argument, session-scoped, repeatable, and
   loaded *additively* without participating in ``--settings`` override semantics,
   so the agent's flag settings (model, permissions, effortLevel) are preserved
   while the guard hook is still installed. The plugin carries only the hook,
   never any ``permissions.deny`` entries, so it does not re-introduce the
   downstream-settings hazard that the default ``--setting-sources user``
   isolation guards against.

It also exposes :func:`snapshot_spec_files` / :func:`diff_spec_files` content-hash
helpers, reused by the post-step diff fallback guard (the second hard layer) to
catch spec writes that slipped past the hook via a ``Bash`` redirect.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Dict, List, Optional


# Relative location (under the project root) of the committed spec corpus that the
# guard protects. Kept as a tuple so it composes cleanly with both ``Path`` joins
# and the settings-file location below.
_SPECS_RELPATH = ("se3", "specs")

# The controlled guard plugin is written under se3/tmp/ (gitignored runtime dir).
_GUARD_PLUGIN_RELPATH = ("se3", "tmp", "spec_write_guard_plugin")

# Tool-input keys that carry the target path for the write tools we match.
_FILE_PATH_KEYS = ("file_path", "notebook_path")


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------

def _specs_dir(cwd: str) -> Path:
    """Return the absolute ``se3/specs`` directory for *cwd* (no existence check)."""
    base = Path(cwd) if cwd else Path.cwd()
    return Path(os.path.normpath(str(base.joinpath(*_SPECS_RELPATH))))


def _resolve_target(file_path: str, cwd: str) -> Optional[Path]:
    """Resolve *file_path* (possibly relative to *cwd*) to a normalized absolute Path."""
    if not file_path:
        return None
    try:
        p = Path(file_path)
        if not p.is_absolute():
            base = Path(cwd) if cwd else Path.cwd()
            p = base / p
        return Path(os.path.normpath(str(p)))
    except Exception:
        return None


def _is_spec_target(file_path: str, cwd: str) -> bool:
    """Return True when *file_path* resolves to a file under ``<cwd>/se3/specs/``."""
    target = _resolve_target(file_path, cwd)
    if target is None:
        return False
    specs_dir = _specs_dir(cwd)
    try:
        target.relative_to(specs_dir)
        return True
    except ValueError:
        return False


def _emit_deny(tool_name: str, file_path: str) -> None:
    """Emit a PreToolUse deny decision and exit.

    Writes the structured ``permissionDecision=deny`` JSON to stdout (the
    advanced-control hook protocol) AND exits with code 2 with the reason on
    stderr (the simple blocking-exit protocol) as a belt-and-suspenders backstop,
    so the write is rejected regardless of which mechanism Claude CLI honors.
    """
    reason = (
        f"Spec files under se3/specs/ are read-only for this step. "
        f"{tool_name or 'This tool'} writing to '{file_path}' is denied. "
        f"Recording code into spec files is the dedicated responsibility of the "
        f"update_spec step and `se3 sync`, not of this step. You ARE free to change "
        f"existing code behavior; if your change alters behavior or you believe a "
        f"spec needs updating, note it in your summary and let the plan spec_changes "
        f"-> verify_spec -> update_spec channel handle the spec write."
    )
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    try:
        sys.stdout.write(json.dumps(output))
        sys.stdout.flush()
    except Exception:
        pass
    try:
        sys.stderr.write(reason + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    sys.exit(2)


def main() -> None:
    """PreToolUse hook entry point.

    Reads the hook payload from stdin, denies a write that targets a spec file,
    and otherwise allows the call (exit 0). Any missing field, malformed JSON, or
    unexpected error results in a safe *allow* — the hook never crashes the host
    agent, and the post-step diff fallback remains as the second line of defense.
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        sys.exit(0)

    if not raw or not raw.strip():
        sys.exit(0)

    try:
        payload = json.loads(raw)
    except Exception:
        sys.exit(0)

    if not isinstance(payload, dict):
        sys.exit(0)

    tool_input = payload.get("tool_input") or {}
    file_path = None
    if isinstance(tool_input, dict):
        for key in _FILE_PATH_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                file_path = value
                break

    if not file_path:
        sys.exit(0)

    cwd = payload.get("cwd") or os.getcwd()
    if _is_spec_target(file_path, cwd):
        tool_name = payload.get("tool_name") or "tool"
        _emit_deny(str(tool_name), file_path)

    sys.exit(0)


# ---------------------------------------------------------------------------
# Controlled guard-plugin generator
# ---------------------------------------------------------------------------

def _guard_plugin_manifest() -> dict:
    """Return the minimal ``.claude-plugin/plugin.json`` manifest dict."""
    return {
        "name": "se3-spec-write-guard",
        "version": "1.0.0",
        "description": (
            "SE3 spec-write protection: a PreToolUse hook that denies "
            "Write/Edit/NotebookEdit targeting se3/specs/."
        ),
    }


def _guard_plugin_hooks_payload() -> dict:
    """Return the plugin ``hooks/hooks.json`` dict carrying ONLY the PreToolUse hook."""
    # Claude CLI runs hook commands through a shell, so the interpreter path MUST
    # be shell-quoted: a venv whose path contains a space (e.g.
    # ``/home/user/my env/bin/python``) would otherwise be split by the shell and
    # the hook subprocess would silently fail to launch — degrading the primary,
    # unbypassable hard guard down to only the post-step diff fallback.
    command = f"{shlex.quote(sys.executable)} -m se3.engine.spec_write_hook"
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Write|Edit|NotebookEdit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                        }
                    ],
                }
            ]
        }
    }


def _write_if_changed(path: Path, desired: str) -> None:
    """Write *desired* to *path* only when it differs from the current content.

    Keeps :func:`ensure_guard_plugin` idempotent so repeated calls across a flow
    do not churn the generated files' mtimes.
    """
    try:
        existing = path.read_text(encoding="utf-8")
    except Exception:
        existing = None
    if existing != desired:
        path.write_text(desired, encoding="utf-8")


def ensure_guard_plugin(project_root) -> Path:
    """Generate/cache the controlled guard plugin and return its directory.

    Materializes a minimal Claude plugin under
    ``<project_root>/se3/tmp/spec_write_guard_plugin/``::

        .claude-plugin/plugin.json   # minimal manifest (name/version/description)
        hooks/hooks.json             # PreToolUse spec-write hook, no permissions

    ``llm_caller`` injects this via ``--plugin-dir <dir>`` rather than a second
    ``--settings`` flag: a repeated ``--settings`` is treated by Claude CLI as a
    full replacement of the prior one, which silently clobbered the agent's own
    ``--settings opus.json`` (and its ``model``). ``--plugin-dir`` is loaded
    additively and does not participate in ``--settings`` override semantics.

    The call is idempotent: each generated file is rewritten only when its
    desired content differs from what is already on disk.

    Args:
        project_root: The target project root (the ``cwd`` of the Claude subprocess).

    Returns:
        Absolute path to the plugin directory.
    """
    root = Path(project_root)
    plugin_dir = root.joinpath(*_GUARD_PLUGIN_RELPATH)

    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
    hooks_path = plugin_dir / "hooks" / "hooks.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.parent.mkdir(parents=True, exist_ok=True)

    _write_if_changed(
        manifest_path,
        json.dumps(_guard_plugin_manifest(), indent=2, sort_keys=True),
    )
    _write_if_changed(
        hooks_path,
        json.dumps(_guard_plugin_hooks_payload(), indent=2, sort_keys=True),
    )

    return plugin_dir


# ---------------------------------------------------------------------------
# Content-hash snapshot helpers (reused by the post-step diff fallback)
# ---------------------------------------------------------------------------

def snapshot_spec_files(project_root) -> Dict[str, str]:
    """Return a ``{project-relative path: sha256-hex}`` map of every spec file.

    Walks ``<project_root>/se3/specs/`` recursively, hashing each regular file's
    content. Used to snapshot spec state before/after a step so the fallback guard
    can detect any spec write (including ones a ``Bash`` redirect slipped past the
    PreToolUse hook). Returns an empty map when the specs directory is absent.
    """
    root = Path(project_root)
    specs_dir = root.joinpath(*_SPECS_RELPATH)
    snapshot: Dict[str, str] = {}
    if not specs_dir.is_dir():
        return snapshot
    for path in sorted(specs_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
            rel = str(path.relative_to(root))
        except Exception:
            continue
        snapshot[rel] = hashlib.sha256(data).hexdigest()
    return snapshot


def diff_spec_files(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    """Return the sorted list of spec paths that were added, removed, or modified.

    Compares two :func:`snapshot_spec_files` maps (or, equivalently, two
    :func:`capture_spec_contents` byte maps — only value equality is tested). A
    path appears in the result when its value changed, or when it is present in
    exactly one of the snapshots.
    """
    before = before or {}
    after = after or {}
    changed = {
        key
        for key in set(before) | set(after)
        if before.get(key) != after.get(key)
    }
    return sorted(changed)


def capture_spec_contents(project_root) -> Dict[str, bytes]:
    """Return a ``{project-relative path: raw bytes}`` map of every spec file.

    The byte-level counterpart of :func:`snapshot_spec_files`. The post-step diff
    fallback guard captures this before a non-exempt step so that, on detecting an
    illegal spec write, it can *revert* each touched file to its pre-step content
    (or delete a newly-created one) — not merely fail the step. Reverting is
    essential: a left-on-disk illegal write would otherwise survive a later
    ``se3 run --resume`` (the resumed run's fresh pre-step snapshot already
    contains the tampered content, so the re-run sees no diff and the change
    leaks through to commit). Returns an empty map when the specs dir is absent.
    """
    root = Path(project_root)
    specs_dir = root.joinpath(*_SPECS_RELPATH)
    contents: Dict[str, bytes] = {}
    if not specs_dir.is_dir():
        return contents
    for path in sorted(specs_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
            rel = str(path.relative_to(root))
        except Exception:
            continue
        contents[rel] = data
    return contents


def restore_spec_files(
    project_root, before: Dict[str, bytes], changed: List[str]
) -> List[str]:
    """Revert each *changed* spec path to its pre-step content.

    For every project-relative path in *changed*: if it existed in *before*
    (captured via :func:`capture_spec_contents`), its original bytes are written
    back; otherwise the path was newly created by the illegal write and is
    deleted. This guarantees a non-``update_spec``/``sync`` step can never get its
    spec modification accepted into the flow, even across a ``--resume``.

    Returns the sorted list of paths that could NOT be restored (best-effort: a
    fault on one path never aborts restoring the rest).
    """
    root = Path(project_root)
    before = before or {}
    failed: List[str] = []
    for rel in changed:
        target = root / rel
        try:
            if rel in before:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(before[rel])
            elif target.exists():
                # Newly created by the illegal write — remove it.
                target.unlink()
        except Exception:
            failed.append(rel)
    return sorted(failed)


if __name__ == "__main__":
    main()
