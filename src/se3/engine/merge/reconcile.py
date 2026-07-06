"""Merge-side version reconciliation — the ``reconcile()`` library core.

Background (accident-driven): the version *decision* used to be baked into a
worktree session's own commit, against the pre-session baseline. Two concurrent
sessions diverging from the same baseline each computed the same next version;
the second to land wrote a verbatim no-op and its changelog entry was deduped
away, so two features shared one version and one lost its changelog. The fix
moves the decision to the merge side: a worktree session emits a
:class:`~se3.engine.version_intent.VersionIntent` (change summary + changelog
bullets + an auxiliary bump hint); this module derives the *final* version once,
at merge time, against master's **current** version — not the version the
session guessed.

Key design properties honoured here:

  * **Unconditional.** ``reconcile()`` has NO trigger predicate. It runs on the
    already-ancestor / no-op-merge path too. The old orchestrator behaviour
    ("Skipping version aggregation: no branches contributed bumps") is exactly
    the hole that dropped a bump; this core never skips on that basis. When
    there are no outstanding intents it is a clean no-op, but that is decided by
    *what intents exist*, not by a merge-shape heuristic.

  * **Two channels.** Without ``se3/version-rules.md`` the deterministic channel
    takes ``max(bump_type)`` across the merged-in intents and mechanically
    applies it to master's current version (reusing ``version_aggregator``'s
    ``max_bump`` / ``Version`` primitives — no LLM). With a rules file the LLM
    channel feeds master's current version + the intents' change summaries /
    changelog bullets + the rules text to the model and adopts its output;
    ``bump_type`` is auxiliary, never the sole carrier of intent.

  * **No regression.** SemVer: final ≥ current, and strictly greater when a bump
    was declared. Custom rules: final must differ from current and must not
    collide with any historically-released version (guards LLM hallucinating a
    backward / already-used number).

  * **Idempotent.** Consumed intents are marked; a resumed / re-entered
    reconcile re-collects only outstanding intents and, backed by the git-
    durable reconcile-commit trailer, never double-bumps.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..docs_updater import DocumentationUpdater
from ..version_bumper import BumpType, Version
from ..version_intent import (
    RECONCILE_TRAILER,
    VersionIntent,
    collect_intents,
    is_consumed,
    mark_consumed,
)
from ..worktree import _run_git
from .version_aggregator import (
    _parse_pyproject_version,
    _safe_write_version,
    max_bump,
)

logger = logging.getLogger(__name__)


VERSION_RULES_FILE_RELPATH = "se3/version-rules.md"

# Cap on the version-rules file injected into the LLM prompt (mirrors
# version_analyze's guard) so a pathological rules file can't blow up the call.
VERSION_RULES_MAX_BYTES = 20_000

# Maps the auxiliary intent ``bump_type`` string onto a SemVer bump. "none" /
# absent contributes nothing — the deterministic channel then falls back to the
# ``max_bump([])`` default (PATCH), matching version_aggregator semantics.
_BUMP_TYPE_BY_NAME = {
    "major": BumpType.MAJOR,
    "minor": BumpType.MINOR,
    "patch": BumpType.PATCH,
}


class ReconcileError(RuntimeError):
    """Base class for reconcile failures the caller must surface/decide on."""


class VersionRegressionError(ReconcileError):
    """Raised when a computed final version would regress or collide.

    Covers both a SemVer numeric regression (final < current, or final == current
    when a bump was declared) and a collision with a historically-released
    version (custom-rules channel guarding against an LLM writing a number that
    was already shipped).
    """


@dataclass
class ReconcileResult:
    """Structured outcome of :func:`reconcile`.

    A no-op (nothing outstanding to reconcile) is a *success* with
    ``already_reconciled=True`` and ``channel="noop"`` — reconcile ran, there
    was simply nothing to apply. Only a genuine fault sets ``error`` / raises.
    """

    success: bool = False
    base_version: Optional[str] = None
    final_version: Optional[str] = None
    bump_type: Optional[str] = None
    channel: str = "noop"  # "deterministic" | "custom-rules" | "noop"
    consumed_flow_ids: list[str] = field(default_factory=list)
    reconcile_commit: Optional[str] = None
    changelog_entries: list[str] = field(default_factory=list)
    already_reconciled: bool = False
    error: Optional[str] = None


# --- historical / current version helpers ------------------------------------

# Version headers in VERSIONS.md look like ``## 11.13.1 - 2026-07-06``. The
# reconcile channel consults these to reject a final version that collides with
# an already-released one (the custom-rules hallucination guard).
_VERSIONS_HEADER_RE = re.compile(r"(?m)^##\s+(\S+)\s+-")


def read_current_version(project_root: Path) -> Optional[str]:
    """Read master's current version from ``pyproject.toml`` on disk.

    Reconcile re-bases on the version *currently on disk in the main checkout*
    (the merge target), NOT on any session's recorded baseline — that re-basing
    is the whole point. Returns ``None`` when the file is absent or has no
    parseable version field.
    """
    pyproject = Path(project_root) / "pyproject.toml"
    try:
        content = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_pyproject_version(content)


def historical_versions(project_root: Path) -> set[str]:
    """Return the set of version strings already recorded in VERSIONS.md.

    Used only as a collision guard; a missing / unreadable changelog yields an
    empty set (no guard, never a hard failure).
    """
    versions_path = Path(project_root) / "VERSIONS.md"
    try:
        content = versions_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {m.group(1).strip() for m in _VERSIONS_HEADER_RE.finditer(content)}


# --- intent aggregation ------------------------------------------------------

def _collect_bumps(intents: list[VersionIntent]) -> list[BumpType]:
    """Map each intent's auxiliary ``bump_type`` onto a SemVer bump.

    Absent / "none" bump hints contribute nothing (they are dropped, not
    treated as PATCH here) so a caller can tell "no bump declared at all" apart
    from "an explicit patch". The deterministic channel applies the
    ``max_bump([])`` default separately when the list ends up empty.
    """
    bumps: list[BumpType] = []
    for intent in intents:
        if not intent.bump_type:
            continue
        bump = _BUMP_TYPE_BY_NAME.get(intent.bump_type.strip().lower())
        if bump is not None:
            bumps.append(bump)
    return bumps


def _merge_changelog_entries(intents: list[VersionIntent]) -> list[str]:
    """Flatten every intent's changelog bullets, de-duplicated, order-preserving.

    The changelog bullets are the changelog *substance* the reconcile step files
    under the final version — combining them here is what stops a second
    feature's entry from being silently dropped (the 11.12.0 collision).
    """
    seen: set[str] = set()
    merged: list[str] = []
    for intent in intents:
        for line in intent.versions_changes:
            text = line.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
    return merged


# --- channels ----------------------------------------------------------------

def compute_deterministic(
    current_version: str, intents: list[VersionIntent]
) -> tuple[str, BumpType]:
    """Deterministic SemVer channel: apply ``max(bump_type)`` to the current version.

    No LLM. Reuses ``version_aggregator.max_bump`` (empty → PATCH) so a set of
    intents with no usable bump hint still advances by the minimal SemVer step.

    Returns ``(final_version_str, chosen_bump)``.

    Raises:
        ReconcileError: if ``current_version`` is not parseable SemVer.
    """
    try:
        base = Version.parse(current_version)
    except ValueError as exc:
        raise ReconcileError(
            f"current version {current_version!r} is not parseable SemVer: {exc}"
        ) from exc
    chosen = max_bump(_collect_bumps(intents))
    return str(base.bump(chosen)), chosen


def _read_version_rules(project_root: Path) -> Optional[str]:
    """Return the version-rules markdown (truncated), or ``None`` when absent.

    Presence of this file is the sole switch between the deterministic and the
    LLM channel. Read errors are swallowed to ``None`` (fall back to SemVer)
    rather than aborting the reconcile.
    """
    rules_path = Path(project_root) / VERSION_RULES_FILE_RELPATH
    try:
        if not rules_path.is_file():
            return None
        text = rules_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s: %s", rules_path, exc)
        return None
    if len(text.encode("utf-8")) > VERSION_RULES_MAX_BYTES:
        logger.warning(
            "%s exceeds %d bytes; truncating for prompt injection.",
            rules_path,
            VERSION_RULES_MAX_BYTES,
        )
        text = text.encode("utf-8")[:VERSION_RULES_MAX_BYTES].decode(
            "utf-8", "ignore"
        )
    return text


_RECONCILE_LLM_PROMPT = """You are deciding the FINAL project version at merge time.

The project uses custom, natural-language version rules (below). Apply them to
derive the single final version string. The inputs are:

- **Current project version** (the merge target's version on disk; your baseline):
{current_version}

- **Merged-in session intents** (what the merged work changed — this, not any
  bump-type label, is the substance you decide from):
{intents_block}

- **Project version rules** (`se3/version-rules.md`, authoritative — overrides
  default SemVer on conflict):
{rules_text}

Rules for your answer:
- Derive the final version from the current version + the changes + the rules.
- It MUST NOT regress and MUST NOT reuse a version the project already released.
- Respond with a JSON object: {{"final_version": "<the version string>",
  "reasoning": "<one line>"}}
"""


def _render_intents_block(intents: list[VersionIntent]) -> str:
    """Render the intents as the change-substance block for the LLM prompt.

    The change summary and changelog bullets — NOT ``bump_type`` — are the
    injected substance; ``bump_type`` is a lossy auxiliary under custom rules
    (date versions, build numbers) so it is deliberately not the anchor.
    """
    parts: list[str] = []
    for intent in intents:
        lines = [f"- session {intent.flow_id}:"]
        if intent.change_summary:
            lines.append(f"    summary: {intent.change_summary}")
        for bullet in intent.versions_changes:
            lines.append(f"    change: {bullet}")
        if intent.bump_type:
            lines.append(f"    (auxiliary bump hint: {intent.bump_type})")
        parts.append("\n".join(lines))
    return "\n".join(parts) if parts else "- (no change details recorded)"


def compute_via_rules(
    project_root: Path,
    current_version: str,
    intents: list[VersionIntent],
    rules_text: str,
    llm_call: Callable[[str], str],
) -> str:
    """Custom-rules LLM channel: derive the final version via the model.

    Args:
        llm_call: ``prompt -> raw_response_text``. Injected so tests can stub
            the model and production wires a real :class:`LLMCaller`.

    Returns the final version string as produced by the LLM (validation /
    no-regression enforcement happens in :func:`reconcile`).

    Raises:
        ReconcileError: on an empty response or a response with no usable
            ``final_version`` field.
    """
    from ..utils.json_parser import parse_json_response

    prompt = _RECONCILE_LLM_PROMPT.format(
        current_version=current_version,
        intents_block=_render_intents_block(intents),
        rules_text=rules_text,
    )
    response = llm_call(prompt)
    if not response or not response.strip():
        raise ReconcileError("version-rules LLM returned an empty response")

    parsed = parse_json_response(response, required_keys=["final_version"])
    if not parsed:
        raise ReconcileError(
            "version-rules LLM response did not contain a JSON object with "
            "a 'final_version' field"
        )
    final = parsed.get("final_version")
    if not isinstance(final, str) or not final.strip():
        raise ReconcileError(
            "version-rules LLM response has an empty 'final_version'"
        )
    return final.strip()


def _default_llm_call(project_root: Path) -> Callable[[str], str]:
    """Build the production ``prompt -> response`` callable backed by LLMCaller."""

    def _call(prompt: str) -> str:
        from ..llm_caller import LLMCaller

        caller = LLMCaller(project_root, step_type="version_reconcile")
        return caller.call(prompt=prompt, json_mode="two_phase")

    return _call


# --- no-regression validation ------------------------------------------------

def validate_no_regression(
    current_version: str,
    final_version: str,
    *,
    declared_bump: bool,
    custom_rules: bool,
    historical: set[str],
) -> None:
    """Enforce the not-a-regression / no-collision contract; raise on violation.

    SemVer semantics (both sides parse): final ≥ current, and strictly greater
    when a bump was declared. Custom-rules semantics: rely on the rules for the
    ordering we cannot compute, but still guarantee final differs from current
    and does not reuse a historically-released version (the LLM-hallucination
    guard). When both sides happen to be SemVer we additionally forbid a numeric
    regression even under custom rules.

    Raises:
        VersionRegressionError: on any regression / collision.
        ReconcileError: on an empty final version.
    """
    final = (final_version or "").strip()
    if not final:
        raise ReconcileError("reconcile produced an empty final version")

    # Collision with an already-released version — this is the exact failure
    # (two features sharing a number) the whole redesign exists to prevent.
    if final in historical:
        raise VersionRegressionError(
            f"final version {final} collides with an already-released version "
            f"in VERSIONS.md"
        )

    base_v: Optional[Version]
    final_v: Optional[Version]
    try:
        base_v = Version.parse(current_version)
    except ValueError:
        base_v = None
    try:
        final_v = Version.parse(final)
    except ValueError:
        final_v = None

    if base_v is not None and final_v is not None:
        # Numeric ordering is checkable — enforce it regardless of channel.
        if final_v < base_v:
            raise VersionRegressionError(
                f"final version {final} is lower than current {current_version}"
            )
        if final_v == base_v:
            raise VersionRegressionError(
                f"final version {final} does not advance current {current_version}"
            )
        if declared_bump and not (final_v > base_v):
            raise VersionRegressionError(
                f"a bump was declared but final {final} does not exceed "
                f"current {current_version}"
            )
        return

    # At least one side is not SemVer (a custom scheme). We cannot order them,
    # so require inequality and lean on the historical-collision guard above.
    if not custom_rules:
        # Default channel must be pure SemVer; unparseable is a hard fault.
        raise ReconcileError(
            f"non-SemVer version under the default channel "
            f"(current={current_version!r}, final={final!r})"
        )
    if final == current_version.strip():
        raise VersionRegressionError(
            f"custom-rules final version {final} does not change current "
            f"{current_version}"
        )


# --- persistence -------------------------------------------------------------

def _write_final_version(project_root: Path, final_version: str) -> None:
    """Write *final_version* into ``pyproject.toml`` atomically.

    Raises:
        ReconcileError: when pyproject.toml is absent or has no version field.
    """
    pyproject = Path(project_root) / "pyproject.toml"
    if not pyproject.exists():
        raise ReconcileError("pyproject.toml not found; cannot write version")
    try:
        _safe_write_version(pyproject, final_version)
    except (OSError, ValueError) as exc:
        raise ReconcileError(f"failed to write pyproject.toml: {exc}") from exc


def _merge_changelog(
    project_root: Path, final_version: str, entries: list[str]
) -> None:
    """File the merged changelog bullets under *final_version* in VERSIONS.md.

    Delegates to :class:`DocumentationUpdater`, whose ``_insert_version_entry``
    merges bullets into an existing block instead of discarding them and drains
    the historical head-blank accumulation. README's version header/badge is
    updated too when a README exists.
    """
    updater = DocumentationUpdater(project_root)
    if entries:
        updater.update_versions_md(final_version, entries)
    try:
        updater.update_readme(final_version)
    except FileNotFoundError:
        # README is optional; a project without one still reconciles.
        pass


def _build_commit_message(
    final_version: str,
    base_version: str,
    channel: str,
    intents: list[VersionIntent],
) -> str:
    """Compose the reconcile commit message with per-flow reconcile trailers.

    The trailer (``Version-Reconcile-Session: <flow_id>``) is the git-durable
    idempotency signal :func:`reconcile_commit_exists` looks for. The body can
    cite each session's now-superseded provisional suggestion for audit.
    """
    lines = [
        f"chore: reconcile version to {final_version} at merge",
        "",
        f"Reconciled {len(intents)} session intent(s) against master "
        f"{base_version} via the {channel} channel.",
    ]
    provisional_notes = [
        f"- {intent.flow_id}: provisional {intent.provisional_suggested_version}"
        for intent in intents
        if intent.provisional_suggested_version
    ]
    if provisional_notes:
        lines.append("")
        lines.append("Superseded provisional versions:")
        lines.extend(provisional_notes)
    lines.append("")
    for intent in intents:
        lines.append(f"{RECONCILE_TRAILER}: {intent.flow_id}")
    return "\n".join(lines) + "\n"


def _commit_reconcile(
    project_root: Path, message: str
) -> Optional[str]:
    """Stage version/doc/intent changes and create the reconcile commit.

    Returns the new commit sha, or ``None`` when there was nothing to commit
    (e.g. version file already matched — the write was a no-op). Git failures
    raise :class:`ReconcileError` so the caller decides recovery.
    """
    to_stage = ["pyproject.toml", "VERSIONS.md", "README.md", "se3/version-intents"]
    for path in to_stage:
        if (Path(project_root) / path).exists():
            _run_git(project_root, "add", path, check=False, timeout=15)

    status = _run_git(
        project_root, "status", "--porcelain", check=False, timeout=15
    )
    if status.returncode == 0 and not status.stdout.strip():
        # Nothing staged/changed — a resumed reconcile whose write was a no-op.
        return None

    commit = _run_git(
        project_root, "commit", "-m", message, check=False, timeout=30
    )
    if commit.returncode != 0:
        raise ReconcileError(
            f"reconcile commit failed: {commit.stderr.strip()}"
        )
    rev = _run_git(
        project_root, "rev-parse", "HEAD", check=False, timeout=15
    )
    return rev.stdout.strip() if rev.returncode == 0 else None


# --- entry point -------------------------------------------------------------

def reconcile(
    project_root: Path,
    *,
    flow_ids: Optional[list[str]] = None,
    llm_call: Optional[Callable[[str], str]] = None,
    commit: bool = True,
) -> ReconcileResult:
    """Derive and apply the final version from merged-in session intents.

    Unconditional: this runs on every merge shape, including already-ancestor /
    no-op merges. It collects outstanding (unconsumed) intents from the merged
    master checkout, re-bases on master's *current* on-disk version, picks a
    channel (deterministic SemVer vs. custom-rules LLM), enforces no-regression,
    then writes the version file, merges the changelog, commits, and marks the
    intents consumed.

    Args:
        project_root: The main-checkout project root (the merge target).
        flow_ids: Optional restriction to specific flows; default is every
            outstanding intent in ``se3/version-intents/``.
        llm_call: Optional ``prompt -> response`` override for the custom-rules
            channel (tests stub it; production builds an LLMCaller when absent).
        commit: When ``True`` (default), stage + commit the reconcile. Set
            ``False`` for a dry apply (write files, mark consumed, no commit).

    Returns:
        A :class:`ReconcileResult`. A run with nothing outstanding is a success
        with ``already_reconciled=True`` and ``channel="noop"``.

    Raises:
        ReconcileError / VersionRegressionError: on a genuine fault (unparseable
        current version, regression/collision, write or commit failure).
    """
    project_root = Path(project_root)

    intents = collect_intents(project_root)
    if flow_ids is not None:
        wanted = set(flow_ids)
        intents = [i for i in intents if i.flow_id in wanted]

    # Idempotency: drop any intent already consumed by an earlier reconcile
    # (on-disk flag OR git-durable reconcile-commit trailer). collect_intents
    # already filters the on-disk flag; the trailer check closes the window
    # where the commit landed but the flag write did not.
    outstanding = [
        i
        for i in intents
        if not is_consumed(project_root, i.flow_id)
    ]

    if not outstanding:
        # Reconcile ran; there was simply nothing left to apply. Not a fault.
        logger.info("reconcile: no outstanding intents; nothing to bump")
        return ReconcileResult(
            success=True,
            base_version=read_current_version(project_root),
            channel="noop",
            already_reconciled=True,
        )

    current_version = read_current_version(project_root)
    if not current_version:
        raise ReconcileError(
            "could not read master's current version from pyproject.toml"
        )

    rules_text = _read_version_rules(project_root)
    declared_bump = bool(_collect_bumps(outstanding))

    if rules_text is None:
        channel = "deterministic"
        final_version, chosen_bump = compute_deterministic(
            current_version, outstanding
        )
        bump_label: Optional[str] = chosen_bump.value
    else:
        channel = "custom-rules"
        call = llm_call or _default_llm_call(project_root)
        final_version = compute_via_rules(
            project_root, current_version, outstanding, rules_text, call
        )
        bump_label = None

    validate_no_regression(
        current_version,
        final_version,
        declared_bump=declared_bump,
        custom_rules=(rules_text is not None),
        historical=historical_versions(project_root),
    )

    changelog_entries = _merge_changelog_entries(outstanding)

    # --- apply -----------------------------------------------------------
    _write_final_version(project_root, final_version)
    _merge_changelog(project_root, final_version, changelog_entries)

    # Mark consumed BEFORE committing so the consumed intent files ship inside
    # the reconcile commit itself — the on-disk flag and the trailer then land
    # atomically, and a re-entry sees both.
    consumed_flow_ids: list[str] = []
    for intent in outstanding:
        if mark_consumed(project_root, intent.flow_id):
            consumed_flow_ids.append(intent.flow_id)
        else:
            # Already flagged (raced): still count it as handled this run.
            consumed_flow_ids.append(intent.flow_id)

    reconcile_commit: Optional[str] = None
    if commit:
        message = _build_commit_message(
            final_version, current_version, channel, outstanding
        )
        try:
            reconcile_commit = _commit_reconcile(project_root, message)
        except (subprocess.SubprocessError, OSError) as exc:
            raise ReconcileError(f"reconcile commit failed: {exc}") from exc

    logger.info(
        "reconcile: %s -> %s via %s channel (%d intent(s), commit=%s)",
        current_version,
        final_version,
        channel,
        len(outstanding),
        reconcile_commit,
    )
    return ReconcileResult(
        success=True,
        base_version=current_version,
        final_version=final_version,
        bump_type=bump_label,
        channel=channel,
        consumed_flow_ids=consumed_flow_ids,
        reconcile_commit=reconcile_commit,
        changelog_entries=changelog_entries,
    )
