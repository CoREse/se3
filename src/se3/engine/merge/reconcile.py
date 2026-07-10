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

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..docs_updater import DocumentationUpdater
from ..git_tags import (
    VersionTagError,
    create_annotated_version_tag,
    should_tag_semver_bump,
    tag_name_for_version,
)
from ..version_bumper import BumpType, Version
from ..version_intent import (
    RECONCILE_TRAILER,
    VERSION_INTENT_DIR_RELPATH,
    VersionIntent,
    collect_intents,
    mark_consumed,
    reconcile_commit_exists,
)
from ..worktree import _run_git
from .version_aggregator import max_bump

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
    is_tag: bool = False
    tag_name: Optional[str] = None
    tag_created: bool = False
    already_reconciled: bool = False
    # True ONLY when version bumping is disabled for the project. A disabled
    # project legitimately produces no intent and no reconcile commit, so the
    # in-flow handler's "no-op without a reconcile commit == dropped intent"
    # guard must NOT fire for it. Distinct from a genuine outstanding-nothing
    # no-op (versioning enabled, intents already consumed / never present),
    # which the guard still scrutinises.
    version_disabled: bool = False
    error: Optional[str] = None


# --- historical / current version helpers ------------------------------------

# Fallback header→version pattern, used only when the project's template can't
# be resolved (a template-derived pattern from :func:`_header_regex_from_template`
# is preferred otherwise). The default changelog template renders
# ``## 11.13.1 - 2026-07-06``, but a project configuring a custom
# ``versions_entry_template`` may use a header with NO ``- <date>`` suffix and/or
# a different heading level (``# {{version}}`` or ``### {{version}}`` for date /
# build-number schemes). The suffix is therefore optional AND any heading level
# (``#`` .. ``######``) is accepted, so this fallback still collects historical
# versions for such projects rather than seeing an empty history and letting the
# custom-rules channel silently reuse an already-shipped version.
_VERSIONS_HEADER_RE = re.compile(r"(?m)^#{1,6}\s+(\S+?)(?:\s+-\s+.*)?\s*$")


def _version_bumper(project_root: Path):
    """Build a ``VersionBumper`` from the project's version config.

    Reuses the SAME detection/read/write abstraction the commit step uses so
    reconcile lands the version in whatever file the project is configured for
    (pyproject.toml, package.json, or an explicit ``version.file_path``) rather
    than assuming pyproject.toml — otherwise a Node.js / custom-path worktree
    flow could commit intent-only, then be unable to reconcile at merge time.
    """
    from ...config import load_version_config
    from ..version_bumper import VersionBumper

    return VersionBumper(load_version_config(project_root))


def read_current_version(project_root: Path) -> Optional[str]:
    """Read master's current version via the project's configured version file.

    Reconcile re-bases on the version *currently on disk in the main checkout*
    (the merge target), NOT on any session's recorded baseline — that re-basing
    is the whole point. Uses the same version-file abstraction as the commit
    step so package.json / an explicit ``version.file_path`` resolve identically
    to pyproject.toml. Returns ``None`` when no version file is found or it has
    no parseable version field.
    """
    bumper = _version_bumper(project_root)
    version_file = bumper.detect_version_file(Path(project_root))
    if version_file is None:
        return None
    try:
        return bumper.read_version(version_file)
    except (ValueError, KeyError, FileNotFoundError, OSError):
        return None


def _header_regex_from_template(project_root: Path) -> "re.Pattern[str]":
    """Build the VERSIONS.md header→version regex from the project's template.

    Historical-collision detection must recognise whatever entry shape the
    project's ``versions_entry_template`` produces — a markdown heading
    (``## <version> - <date>`` / ``### {{version}}``) OR a non-heading line
    (``ENTRY {{version}} | {{changes}}``, date / build-number schemes). A template
    whose version placeholder does not sit on a ``#`` heading would otherwise
    yield an empty historical set, letting the custom-rules LLM channel silently
    reuse an already-shipped version. We derive the regex from the *effective*
    changelog template (config override or packaged default); any resolution/parse
    fault degrades to the permissive module-level fallback rather than aborting the
    reconcile.
    """
    try:
        from ...config import load_docs_config

        updater = DocumentationUpdater(
            project_root, config=load_docs_config(project_root).to_updater_config()
        )
        template = updater.templates["versions_entry"].content
    except Exception:  # noqa: BLE001 - template resolution must never abort the guard
        return _VERSIONS_HEADER_RE

    # Anchor on whatever template line actually carries ``{{version}}`` — NOT only
    # a markdown heading. A custom ``versions_entry_template`` may use a heading
    # (``# {{version}}`` / ``### {{version}}`` for date / build-number schemes) OR
    # a non-heading entry line (``ENTRY {{version}} | {{changes}}``). The old
    # ``#{1,6}\s`` heading requirement missed the non-heading case entirely,
    # yielding an empty historical set and letting the custom-rules LLM channel
    # silently reuse an already-shipped non-heading version. Scanning for the first
    # line carrying the version placeholder (rather than the first heading) also
    # avoids latching onto an unrelated section title.
    header_line = ""
    for line in template.splitlines():
        stripped = line.strip()
        if "{{version}}" in stripped:
            header_line = stripped
            break
    if "{{version}}" not in header_line:
        return _VERSIONS_HEADER_RE

    # Anchor on the literal text BEFORE the version placeholder. We must ALSO
    # anchor on the literal text immediately AFTER it, otherwise a bracketed /
    # quoted template such as ``## [{{version}}] - {{date}}`` captures the
    # closing ``]`` into the version (``1.2.4]``) and the historical-collision
    # guard fails to recognise a later ``1.2.4`` as a reuse. The trailing anchor
    # is only the literal run directly following the placeholder up to the next
    # whitespace or ``{{`` placeholder; when that run is empty (the default
    # ``## {{version}} - {{date}}`` where a space follows) we fall back to a
    # whitespace-delimited capture. This catches both suffixed and suffixless
    # headers — even mixed in one file (an old default ``## v - date`` block
    # above new ``## v`` blocks) — without over-fitting to the whole suffix.
    prefix, suffix = header_line.split("{{version}}", 1)
    terminator = ""
    for i, ch in enumerate(suffix):
        if ch.isspace() or suffix.startswith("{{", i):
            break
        terminator += ch
    if terminator:
        capture = r"(\S+?)" + re.escape(terminator)
    else:
        capture = r"(\S+)"
    try:
        return re.compile(r"(?m)^" + re.escape(prefix) + capture)
    except re.error:
        return _VERSIONS_HEADER_RE


def historical_versions(project_root: Path) -> set[str]:
    """Return the set of version strings already recorded in VERSIONS.md.

    Used only as a collision guard; a missing / unreadable changelog yields an
    empty set (no guard, never a hard failure). Header parsing follows the
    project's configured changelog template so custom (suffixless / non-SemVer)
    headers are still collected — otherwise the guard silently sees no history.
    """
    versions_path = Path(project_root) / "VERSIONS.md"
    try:
        content = versions_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    header_re = _header_regex_from_template(project_root)
    return {m.group(1).strip() for m in header_re.finditer(content)}


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
) -> tuple[str, Optional[BumpType]]:
    """Deterministic SemVer channel: apply ``max(bump_type)`` to the current version.

    No LLM. When at least one intent declares a usable bump hint, apply the
    maximum. When NO intent declares a bump (every hint absent / "none" — a value
    ``version_analyze`` legitimately emits for e.g. docs-only work), the
    no-regress rule permits ``final == current`` (strictly-greater is required
    only when a bump was declared), so we leave the version untouched rather than
    fabricating a phantom patch release for work with no versionable change.

    Returns ``(final_version_str, chosen_bump_or_None)`` — ``chosen_bump`` is
    ``None`` exactly when no bump was declared and the version is left as-is.

    Raises:
        ReconcileError: if ``current_version`` is not parseable SemVer.
    """
    try:
        base = Version.parse(current_version)
    except ValueError as exc:
        raise ReconcileError(
            f"current version {current_version!r} is not parseable SemVer: {exc}"
        ) from exc
    bumps = _collect_bumps(intents)
    if not bumps:
        return str(base), None
    chosen = max_bump(bumps)
    return str(base.bump(chosen)), chosen


def _read_version_rules(project_root: Path) -> Optional[str]:
    """Return the version-rules markdown (truncated), or ``None`` when absent.

    Presence of this file is the sole switch between the deterministic and the
    LLM channel, so a read *error* on a file that exists must NOT collapse to
    ``None``: doing so would silently publish a SemVer bump on a project whose
    custom rules (date/build-number versioning) were merely unreadable. An
    existing-but-unreadable rules file is therefore a typed reconcile failure;
    only genuine absence returns ``None``.
    """
    rules_path = Path(project_root) / VERSION_RULES_FILE_RELPATH
    try:
        exists = rules_path.is_file()
    except OSError as exc:
        raise ReconcileError(
            f"could not stat version-rules file {rules_path}: {exc}"
        ) from exc
    if not exists:
        return None
    try:
        text = rules_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReconcileError(
            f"version-rules file {rules_path} exists but could not be read "
            f"({exc}); refusing to fall back to the default SemVer channel and "
            f"publish a version that ignores the project's custom rules"
        ) from exc
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
  "is_tag": true|false, "reasoning": "<one line>"}}
- Set "is_tag" to true only when the custom rules say this final release should
  create a git tag.
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
    revision_feedback: Optional[str] = None,
) -> tuple[str, bool]:
    """Custom-rules LLM channel: derive the final version via the model.

    Args:
        llm_call: ``prompt -> raw_response_text``. Injected so tests can stub
            the model and production wires a real :class:`LLMCaller`.
        revision_feedback: Optional reviewer feedback from a rejected prior
            decision, appended to the prompt so the reviewer can steer the
            recomputed version (custom-rules channel only — a deterministic
            SemVer result cannot be steered).

    Returns the final version string and tag decision as produced by the LLM
    (validation / no-regression enforcement happens in :func:`reconcile`).

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
    if revision_feedback and revision_feedback.strip():
        prompt += (
            "\n\nA human reviewer REJECTED the previous version decision with "
            "this feedback — honour it while still obeying the rules above:\n"
            f"{revision_feedback.strip()}\n"
        )
    try:
        response = llm_call(prompt)
    except ReconcileError:
        raise
    except Exception as exc:  # noqa: BLE001 - any transport fault becomes a typed reconcile failure
        # The LLM transport (timeout, RuntimeError, provider error) must surface
        # as a ReconcileError so run_merge / the step / the CLI render the
        # documented "version reconcile failed, rerun merge" recovery path
        # instead of letting a raw traceback escape run_merge after the branch
        # integration already committed.
        raise ReconcileError(
            f"version-rules LLM call failed: {exc}"
        ) from exc
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
    raw_is_tag = parsed.get("is_tag")
    return final.strip(), raw_is_tag if isinstance(raw_is_tag, bool) else False


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
            # SemVer precedence ignores build metadata (Version.__eq__), so
            # ``1.0.0+b1`` and ``1.0.0+b2`` compare equal here. Under the
            # custom-rules channel a build-metadata-only advance is a legitimate
            # ordered step (build-number / date schemes the design explicitly
            # contemplates): accept it when the string actually changed — the
            # historical-collision guard above already rejects reuse of a
            # released number. The default SemVer channel still rejects a
            # non-advancing equal.
            if custom_rules and final != current_version.strip():
                return
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

def _write_final_version(project_root: Path, final_version: str) -> Path:
    """Write *final_version* into the project's configured version file.

    Returns the version file path so the caller can stage exactly that file
    (package.json / an explicit ``version.file_path``, not necessarily
    pyproject.toml). Uses the same ``VersionBumper`` the commit step uses, so
    every project type / config the commit path supports reconciles too.

    Raises:
        ReconcileError: when no version file can be found or the write fails.
    """
    bumper = _version_bumper(project_root)
    # detect_version_file also primes script mode as a side effect, so it must
    # run before we consult the script runner / handler below.
    version_file = bumper.detect_version_file(Path(project_root))
    if version_file is None:
        raise ReconcileError(
            "no version file found in project; cannot write reconciled version"
        )
    try:
        # Write through the resolved handler / script runner rather than
        # VersionBumper.set_version: set_version enforces SemVer, but the
        # custom-rules channel may legitimately land a non-SemVer final (date /
        # build schemes) that validate_no_regression already accepted — so we
        # must not re-reject it here. File-type coverage (toml/json/py/script)
        # is identical to the commit path's abstraction.
        if getattr(bumper, "_use_script_mode", False) and bumper._script_runner:
            bumper._script_runner.set_version(final_version)
        else:
            handler = bumper._get_handler(version_file)
            handler.write_version(version_file, final_version)
    except (OSError, ValueError, KeyError, FileNotFoundError, RuntimeError) as exc:
        raise ReconcileError(
            f"failed to write version file {version_file}: {exc}"
        ) from exc
    return version_file


def _merge_changelog(
    project_root: Path, final_version: str, entries: list[str]
) -> None:
    """File the merged changelog bullets under *final_version* in VERSIONS.md.

    Delegates to :class:`DocumentationUpdater`, whose ``_insert_version_entry``
    merges bullets into an existing block instead of discarding them and drains
    the historical head-blank accumulation. README's version header/badge is
    updated too when a README exists.

    Loads the project's docs config (as the commit path does) so a custom
    ``versions_entry_template`` produces the same header/body shape at merge-time
    reconcile as it does on a direct commit, instead of the packaged default.
    """
    try:
        from ...config import load_docs_config

        config = load_docs_config(project_root).to_updater_config()
    except Exception:  # noqa: BLE001 - config load must not abort the reconcile
        config = None
    updater = DocumentationUpdater(project_root, config=config)
    # Always record the released number in VERSIONS.md, even with zero changelog
    # bullets (update_versions_md writes a "- No changes recorded" placeholder
    # body). historical_versions() is parsed from VERSIONS.md headers and is the
    # anti-collision guard's source of truth: a version that reaches the version
    # file but never lands a VERSIONS.md header could later be re-approved for
    # reuse — the exact collision this redesign exists to prevent.
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
    *,
    tag_name: Optional[str] = None,
) -> str:
    """Compose the reconcile commit message with per-flow reconcile trailers.

    The trailer (``Version-Reconcile-Session: <flow_id>``) is the git-durable
    idempotency signal :func:`reconcile_commit_exists` looks for. The body can
    cite each session's now-superseded provisional suggestion for audit.

    *tag_name* stamps a second git-durable trailer (``Version-Tag: <name>``)
    recording that THIS decision owns that release tag. It is the only signal
    :func:`undo_last_reconcile` can trust when it later decides whether it is
    entitled to delete the tag: a decision that creates no tag (patch bump,
    custom rule with ``is_tag: false``) stamps no trailer, so a same-named tag an
    operator added by hand is left alone. Written before the tag is created — a
    tag that failed to materialise simply leaves the delete a no-op.
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
    if tag_name:
        lines.append(f"{VERSION_TAG_TRAILER}: {tag_name}")
    return "\n".join(lines) + "\n"


def _commit_reconcile(
    project_root: Path,
    message: str,
    *,
    version_file: Optional[Path] = None,
    allow_empty: bool = False,
    intended_tag: Optional[str] = None,
) -> Optional[str]:
    """Stage version/doc/intent changes and create the reconcile commit.

    *intended_tag* is the tag the caller will create on the resulting commit (or
    ``None`` when this decision creates no tag). It decides what an unreadable
    commit hash means: with a tag owed the release would silently lose its tag,
    so we fail loud and the message becomes the operator's sole record of what
    to recreate by hand; with no tag owed the sha has no consumer and the
    reconcile stays a success.

    Returns the new commit sha, or ``None`` when there was nothing to commit
    (e.g. version file already matched — the write was a no-op) or when no tag
    is owed and the hash could not be read. Git failures raise
    :class:`ReconcileError` so the caller decides recovery.

    *version_file* is the file the reconciled version was written to; it is
    included in the commit pathspec explicitly so a package.json / custom
    ``version.file_path`` lands in the commit rather than assuming pyproject.toml.

    *allow_empty* forces the commit even when the reconcile-owned paths carry no
    diff. The reconcile step passes it because the commit's trailer — not the
    file diff — is the sole durable idempotency signal
    (:func:`reconcile_commit_exists`). A no-bump session whose intent flag was
    ALREADY committed (e.g. a prior bad/manual commit swept ``consumed: true``
    into HEAD) writes no version/doc/flag change, so a diff-gated commit would
    silently produce NOTHING — leaving the step "complete" yet permanently
    outstanding, re-running the same no-op on every resume. The empty commit
    stamps the trailer so the decision is durable. Still path-limited to the
    reconcile-owned paths, so ``--allow-empty`` never sweeps in an operator's
    unrelated staged files.
    """
    reconcile_paths: list[str] = []
    if version_file is not None:
        try:
            reconcile_paths.append(str(Path(version_file).relative_to(project_root)))
        except ValueError:
            reconcile_paths.append(str(version_file))
    reconcile_paths += ["VERSIONS.md", "README.md", "se3/version-intents"]
    existing = [p for p in reconcile_paths if (Path(project_root) / p).exists()]

    # Stage the reconcile-owned paths first — ``git add`` is what pulls in an
    # untracked/newly-written intent JSON that a bare pathspec commit would
    # otherwise reject ("pathspec did not match any file(s) known to git").
    for path in existing:
        _run_git(project_root, "add", path, check=False, timeout=15)

    # Constrain BOTH the emptiness check and the commit to the reconcile-owned
    # paths. The main checkout is shared: an operator may have unrelated files
    # staged (the worktree flow ran elsewhere). A whole-tree ``status`` /
    # ``git commit`` would report that unrelated dirt as work-to-do and sweep it
    # into the version reconcile commit. Path-limiting keeps the reconcile commit
    # to exactly the version file + changelog + consumed-intent records.
    status = _run_git(
        project_root, "status", "--porcelain", "--", *existing,
        check=False, timeout=15,
    )
    if status.returncode == 0 and not status.stdout.strip() and not allow_empty:
        # Nothing staged/changed and the caller does not need a durable trailer —
        # a resumed reconcile whose write was a no-op. When *allow_empty* is set
        # the caller DOES need the commit (its trailer is the idempotency signal),
        # so we fall through and stamp an empty commit instead of skipping.
        return None

    # ``git commit -- <pathspec>`` records exactly these (now-staged) paths,
    # disregarding anything else already staged elsewhere in the main checkout,
    # so an operator's unrelated staged file never rides along. ``--allow-empty``
    # (when requested) keeps that same path-limit — it never sweeps in unrelated
    # staged work — while still producing the trailer-bearing commit.
    commit_args = ["commit", "-m", message]
    if allow_empty:
        commit_args.append("--allow-empty")
    commit_args += ["--", *existing]
    commit = _run_git(
        project_root, *commit_args,
        check=False, timeout=30,
    )
    if commit.returncode != 0:
        raise ReconcileError(
            f"reconcile commit failed: {commit.stderr.strip()}"
        )
    rev = _run_git(
        project_root, "rev-parse", "HEAD", check=False, timeout=15
    )
    commit_hash = rev.stdout.strip() if rev.returncode == 0 else ""
    if not commit_hash:
        if intended_tag is None:
            # No tag is owed, so an unreadable hash costs the caller nothing it
            # needs: the commit is durable and the sha is only ever consumed by
            # the tag block. Returning None keeps a patch/no-tag reconcile a
            # success, exactly as before tagging existed.
            return None
        # A tag is owed but the commit is durable and unidentifiable, so it can
        # neither be tagged nor reported. Failing loud beats returning success
        # for a release whose tag was silently skipped — recovery is a manual
        # ``git tag -a``. No placeholder commit-ish is invented: the message
        # states the hash is unreadable and points at HEAD, the only honest
        # locator left.
        raise ReconcileError(
            f"version tag {intended_tag} was NOT created; reconcile commit was "
            f"created but its hash could not be read (git rev-parse HEAD "
            f"failed): {rev.stderr.strip()}. The commit is the tip of "
            f"{_describe_head_location(project_root)}"
        )
    return commit_hash


def _describe_head_location(project_root: Path) -> str:
    """An honest, human-usable locator for ``HEAD`` when its hash is unreadable."""
    branch_ref = _run_git(
        project_root, "rev-parse", "--abbrev-ref", "HEAD", check=False, timeout=15
    )
    branch = branch_ref.stdout.strip() if branch_ref.returncode == 0 else ""
    if branch and branch != "HEAD":
        return f"HEAD on branch {branch}"
    return "HEAD (current branch could not be determined)"


def _reconcile_owned_relpaths(project_root: Path) -> list[str]:
    """The paths reconcile writes and must therefore isolate from operator dirt.

    The configured version file (pyproject.toml / package.json / a custom
    ``version.file_path``) PLUS the changelog/version-doc files. The version file
    is NOT exempt: an operator may hold an unrelated uncommitted edit to it (e.g. a
    dependency bump in pyproject.toml) that reconcile must not destroy, and its
    dirt must be neutralized before the base version is read so a half-applied
    bump can never be re-read as the base. Version-file detection faults degrade to
    the doc-only set rather than aborting reconcile.
    """
    rels = ["README.md", "VERSIONS.md"]
    try:
        version_file = _version_bumper(project_root).detect_version_file(
            Path(project_root)
        )
    except Exception:  # noqa: BLE001 - version-file detection must not abort reconcile
        version_file = None
    if version_file is not None:
        try:
            rels.insert(0, str(Path(version_file).relative_to(project_root)))
        except ValueError:
            rels.insert(0, str(version_file))
    return rels


def _assert_staged_state_replayable(project_root: Path, rel: str) -> None:
    """Raise ReconcileError if ``rel`` has dirt reconcile can't safely replay.

    Two un-replayable conditions are rejected read-only here: a divergent staged
    state (below), and a dirty working-tree file reconcile cannot read (so it can be
    neither snapshotted for replay nor safely reset without losing the edit).

    Read-only — mutates nothing. Reconcile can replay only ONE operator state (the
    working tree) onto its committed rewrite, so an index that differs from BOTH
    HEAD (merely unstaged) and the working tree (staged == tree) is unambiguous and
    replays fine, but an index differing from both (the operator staged one version
    then edited it further) cannot be folded in alongside reconcile's change —
    refuse rather than silently overwrite (改动 C: reject, let the operator
    commit/stash first).

    Split out as a PRE-PASS the caller runs over every reconcile-owned path before
    it mutates any of them: :func:`_detach_operator_edits` resets/unlinks each path
    as it scans, and reconcile() invokes it BEFORE the try/finally that reattaches
    snapshots, so a raise mid-scan would strand already-detached earlier paths
    (their operator edit wiped, no snapshot to replay). Detecting the fatal
    condition read-only up front guarantees the detach either processes every
    eligible path or none.
    """
    path = Path(project_root) / rel
    status = _run_git(
        project_root, "status", "--porcelain", "--", rel, check=False, timeout=15
    )
    if status.returncode != 0:
        # git could not determine whether this reconcile-owned path is dirty (index
        # lock, repo error, timeout). Treating an unknown tree as clean is exactly
        # the hole this pre-pass exists to close: a pre-existing operator edit would
        # then go undetached and be re-read as the base, overwritten, or committed.
        # Refuse read-only, before the mutation loop touches anything (改动 C: 2a —
        # typed pre-write failure, let the operator commit/stash first).
        raise ReconcileError(
            f"reconcile could not determine the working-tree state of "
            f"reconcile-owned path {rel!r}: git status failed "
            f"({status.stderr.strip() or 'unknown error'}). Refusing to continue "
            "on an unknown working tree — commit or stash any change and retry."
        )
    if not status.stdout.strip():
        return  # clean — nothing to detach, nothing to check
    if not path.exists():
        return  # operator deletion — no working-tree/staged ambiguity to fold
    try:
        operator_content = path.read_text(encoding="utf-8")
    except OSError as exc:
        # Dirty but unreadable (EACCES, EIO, a transient FS fault). Reconcile can
        # only preserve an operator edit it can snapshot; a path it cannot read can
        # neither be snapshotted for replay nor safely reset, so the later rollback's
        # ``git checkout HEAD -- <rel>`` would overwrite it and the dirt could taint
        # the version/docs read. Reject here, read-only, before the mutation loop or
        # any write/commit (改动 C: 2a — typed pre-write failure, leave no reconcile
        # output behind and let the operator commit/stash/fix the path first).
        raise ReconcileError(
            f"reconcile could not read the uncommitted change on reconcile-owned "
            f"path {rel!r} ({exc}); refusing to continue on a working tree it cannot "
            "safely snapshot — commit, stash, or fix the path before merging."
        ) from exc
    head = _run_git(
        project_root, "show", f"HEAD:{rel}", check=False, timeout=15
    )
    if head.returncode != 0:
        return  # untracked — no HEAD to compare a staged state against
    idx = _run_git(project_root, "show", f":{rel}", check=False, timeout=15)
    if idx.returncode != 0:
        return  # no distinct index entry
    staged_content = idx.stdout
    if staged_content != operator_content and staged_content != head.stdout:
        raise ReconcileError(
            f"reconcile-owned path {rel!r} has a staged state that "
            "differs from both HEAD and the working tree (the operator "
            "staged one version and then edited it further). Reconcile "
            "cannot preserve both the staged and the working-tree edit "
            "while rewriting this file; commit or stash the change "
            "before merging."
        )


def _assert_detached_to_head(
    project_root: Path, rel: str, checkout: subprocess.CompletedProcess
) -> None:
    """Confirm a reconcile-owned path was actually reset to HEAD; else refuse.

    A ``git checkout HEAD -- <rel>`` that does not run to completion (index lock,
    EACCES, an FS error) leaves the operator's dirt in place. Continuing on that
    unsafe tree is exactly the double-bump / operator-edit-loss hole the detach
    exists to close: reconcile would read the dirty version as its base, commit the
    operator's edit into the release commit, or overwrite it during the
    version/changelog write. So fail with a typed error the moment the reset does
    not verifiably land — the operator's snapshot was already recorded, so the
    caller's reattach still replays the edit — rather than mutating further.
    """
    if checkout.returncode != 0:
        raise ReconcileError(
            f"reconcile could not detach the operator's uncommitted change to "
            f"reconcile-owned path {rel!r}: git checkout HEAD failed "
            f"({checkout.stderr.strip() or 'unknown error'}). Refusing to continue "
            "on an unsafe working tree — commit or stash the change before merging."
        )
    # Defense in depth: a returncode-0 checkout that somehow left the path dirty
    # (a partial reset) is still unsafe, so verify the tree is genuinely clean for
    # this path before letting reconcile read its base and write.
    status = _run_git(
        project_root, "status", "--porcelain", "--", rel, check=False, timeout=15
    )
    if status.returncode != 0 or status.stdout.strip():
        raise ReconcileError(
            f"reconcile reset reconcile-owned path {rel!r} to HEAD but the working "
            "tree is still dirty for it; refusing to continue on an unsafe working "
            "tree — commit or stash the change before merging."
        )


def _detach_operator_edits(
    project_root: Path,
    relpaths: list[str],
    snapshots: Optional[dict[str, tuple[str, Optional[str]]]] = None,
    *,
    persist: Optional[Callable[[dict[str, tuple[str, Optional[str]]]], None]] = None,
) -> dict[str, tuple[str, Optional[str]]]:
    """Snapshot operator dirt on reconcile-owned paths and reset them to HEAD.

    git commits whole files and reconcile re-bases the version on the COMMITTED
    base, so an operator's uncommitted edit to a reconcile-owned path (the version
    file, README.md, or VERSIONS.md) must not (a) ride into the reconcile commit,
    (b) be re-read as the version base — a dirty bumped version file would
    double-bump — or (c) be destroyed by the failure rollback. Each dirty path is
    snapshotted as ``(committed-base, operator-working-tree)`` and reset to HEAD so
    reconcile writes and commits on the committed base alone; the operator's diff
    is replayed via 3-way merge afterward (see :func:`_reattach_operator_edits`).

    An operator DELETION of a tracked reconcile-owned path is dirt too, and git
    reports it (`` D``) even though there is no working-tree file to read. Such a
    path is snapshotted with ``operator_content=None`` (the deletion sentinel) and
    restored from HEAD so reconcile can still write its version/changelog change on
    the committed base; the deletion is replayed afterward. Skipping it — as a bare
    ``path.exists()`` guard used to — would let reconcile silently recreate and
    commit the file the operator had deleted, losing the deletion.

    An UNTRACKED operator file on a reconcile-owned path (git reports ``??``, the
    path is absent from HEAD) is neutralized by unlinking it rather than by
    ``git checkout HEAD -- <path>``, which is a silent no-op with no HEAD version to
    restore. Left in place, the file would be swept into the reconcile commit by
    _commit_reconcile's ``git add``; removing it lets reconcile write on the true
    empty base while the snapshot replays the untracked content afterward, so it
    survives as operator work instead of riding into the release commit.

    Resetting the version file here — rather than discarding the edit outright, as
    the old ``_restore_version_file`` did — is what simultaneously closes the
    double-bump window (base always read from HEAD) and preserves operator edits to
    the version file (they are replayed, not lost). Clean paths are skipped so the
    normal case (no operator dirt) is unchanged. Called only after the entry-time
    residue recovery, so any remaining dirt is genuine operator work rather than a
    prior reconcile's half-applied bump.

    Returns a ``relpath -> (head_content, operator_content)`` map, where
    ``operator_content`` is ``None`` for a pre-existing operator deletion.

    ``snapshots`` may be supplied by the caller so that a mid-loop fault (e.g. a
    ``git checkout`` timing out under lock contention) still leaves the caller
    holding every snapshot taken so far — its reattach ``finally`` then replays
    them instead of stranding an already-detached path whose operator edit was
    reset to HEAD. When omitted a fresh dict is created (standalone use).

    The scan is split into a read-only CAPTURE pass (populate ``snapshots`` for
    every dirty path) and a MUTATE pass (reset/unlink each captured path). Doing
    ALL captures before ANY mutation is load-bearing for two guarantees: (1) the
    optional ``persist`` callback — invoked once at the capture→mutate boundary —
    writes a crash-durable copy of every operator edit BEFORE the first
    working-tree reset, so a hard kill anywhere in the mutate/apply window can be
    recovered on the next run (the in-memory dict alone dies with the process). If
    that durable write fails with operator dirt present it RAISES, aborting before
    the mutate pass so no operator edit is reset to HEAD without an on-disk copy;
    and (2) a reset that fails partway through the mutate pass still leaves EVERY
    dirty path snapshotted, so the caller's blind failure-rollback + reattach
    finally can never strand an un-snapshotted operator edit (the fix for the old
    interleaved scan, which reset an earlier path then raised on a later one with
    that earlier edit already wiped and unrecoverable).
    """
    if snapshots is None:
        snapshots = {}
    # PRE-PASS (read-only): reject any un-replayable staged/working-tree
    # divergence across ALL paths BEFORE mutating a single one. Checking every
    # path up front makes the replayability decision all-or-nothing.
    for rel in relpaths:
        _assert_staged_state_replayable(project_root, rel)

    # CAPTURE PASS (read-only): snapshot every dirty path and record the mutation
    # it will need, WITHOUT touching the working tree yet.
    pending: list[tuple[str, str]] = []  # (rel, "checkout" | "unlink")
    for rel in relpaths:
        path = Path(project_root) / rel
        # Status FIRST (before the existence check): a tracked file the operator
        # deleted no longer exists on disk yet is still real dirt git must report,
        # whereas an untracked path that simply never existed reports clean and is
        # correctly skipped here.
        status = _run_git(
            project_root, "status", "--porcelain", "--", rel,
            check=False, timeout=15,
        )
        if status.returncode != 0:
            # The read-only pre-pass already probed status for every path, but git
            # can still fail transiently between the pre-pass and here. An unknown
            # tree must never be read as clean: continuing would leave a pre-existing
            # operator edit undetached (re-read as base / overwritten / committed).
            # Raise typed — no path has been mutated yet, so nothing is stranded.
            raise ReconcileError(
                f"reconcile could not determine the working-tree state of "
                f"reconcile-owned path {rel!r}: git status failed "
                f"({status.stderr.strip() or 'unknown error'}). Refusing to continue "
                "on an unknown working tree — commit or stash any change and retry."
            )
        if not status.stdout.strip():
            # Clean (or a non-existent untracked path) — reconcile transforms the
            # committed base directly, nothing to detach.
            continue
        if not path.exists():
            # Operator deleted this tracked reconcile-owned path. No working-tree
            # content to read; snapshot the deletion and (in the mutate pass) restore
            # from HEAD so reconcile writes on the committed base and
            # _reattach_operator_edits replays the deletion after the commit.
            head = _run_git(
                project_root, "show", f"HEAD:{rel}", check=False, timeout=15
            )
            head_content = head.stdout if head.returncode == 0 else ""
            snapshots[rel] = (head_content, None)
            pending.append((rel, "checkout"))
            continue
        try:
            operator_content = path.read_text(encoding="utf-8")
        except OSError as exc:
            # The read-only pre-pass already rejected an unreadable dirty path, but a
            # read can still fail transiently between the pre-pass and here. Such dirt
            # cannot be snapshotted, so leaving it in place would let the rollback's
            # ``git checkout HEAD -- <rel>`` overwrite it or let it taint the
            # version/docs read. Raise typed — nothing has been mutated yet.
            raise ReconcileError(
                f"reconcile could not read the uncommitted change on reconcile-owned "
                f"path {rel!r} ({exc}); refusing to continue on a working tree it "
                "cannot safely snapshot — commit, stash, or fix the path before "
                "merging."
            ) from exc
        head = _run_git(
            project_root, "show", f"HEAD:{rel}", check=False, timeout=15
        )
        tracked = head.returncode == 0
        head_content = head.stdout if tracked else ""
        # An un-replayable divergent staged state was already rejected read-only
        # by the pre-pass above (_assert_staged_state_replayable), so anything
        # reaching here has a staged state reconcile can fold in — snapshot it and
        # queue its detach.
        snapshots[rel] = (head_content, operator_content)
        # Tracked → reset to HEAD; untracked → unlink (``git checkout HEAD`` is a
        # silent no-op with no HEAD version, so _commit_reconcile's ``git add``
        # would otherwise sweep the operator's file into the release commit).
        pending.append((rel, "checkout" if tracked else "unlink"))

    # DURABLE persist point: every operator edit is now snapshotted but the working
    # tree is still untouched. Persist the crash backstop HERE so a SIGKILL / OOM /
    # power-loss during the mutate pass below — or the long version derivation that
    # follows it (a custom-rules LLM call can run for minutes) — is recoverable from
    # disk, not only from the in-memory dict that dies with the process. A persist
    # that cannot write the durable copy RAISES here (before any mutation), so the
    # operator's dirt is never reset to HEAD without a recoverable on-disk copy.
    if persist is not None:
        persist(snapshots)

    # MUTATE PASS: reset/unlink each captured path. A fault here still leaves every
    # dirty path snapshotted (reattach replays them) and the durable backstop
    # already written.
    for rel, action in pending:
        path = Path(project_root) / rel
        if action == "checkout":
            # Reset to HEAD so reconcile's write, its commit, and the base-version
            # read all see the committed content, never the operator dirt (a
            # deletion is restored from HEAD the same way, then replayed later).
            checkout = _run_git(
                project_root, "checkout", "HEAD", "--", rel, check=False, timeout=15
            )
            _assert_detached_to_head(project_root, rel, checkout)
        else:  # unlink — untracked operator file on a reconcile-owned path
            try:
                path.unlink()
            except OSError as exc:
                # An untracked operator file left in place would be swept into the
                # reconcile commit by _commit_reconcile's ``git add``; if it cannot
                # be removed the tree is unsafe, so fail typed rather than release
                # the operator's file. The snapshot is recorded, so the reattach
                # still replays it.
                raise ReconcileError(
                    f"reconcile could not remove the operator's untracked file on "
                    f"reconcile-owned path {rel!r} ({exc}); refusing to continue on "
                    "an unsafe working tree — commit or stash (or remove) the file "
                    "before merging."
                ) from exc
    return snapshots


def _three_way_merge(
    project_root: Path, current: str, base: str, other: str
) -> tuple[str, bool, bool]:
    """3-way merge ``other`` onto ``current`` relative to common ancestor ``base``.

    ``current`` is reconcile's committed content, ``other`` the operator's
    working-tree content, ``base`` their shared HEAD ancestor — so the merge lands
    the operator's delta on top of reconcile's change. Uses ``git merge-file -p``
    (writes to stdout, mutates nothing). Returns ``(merged_text, conflicted, ok)``.

    ``ok`` is False when ``git merge-file`` could not run to completion (tooling /
    OS error). In that case ``merged_text`` is meaningless — the caller MUST NOT
    treat it as a clean merge, since doing so would overwrite the operator's edit
    with reconcile's content and report success. We surface the failure instead of
    silently keeping reconcile's content.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        cur_f = Path(td) / "current"
        base_f = Path(td) / "base"
        other_f = Path(td) / "other"
        try:
            cur_f.write_text(current, encoding="utf-8")
            base_f.write_text(base, encoding="utf-8")
            other_f.write_text(other, encoding="utf-8")
        except OSError:
            return current, False, False
        res = _run_git(
            project_root,
            "merge-file", "-p",
            "-L", "reconcile", "-L", "base", "-L", "operator",
            str(cur_f), str(base_f), str(other_f),
            check=False, timeout=15,
        )
    # git merge-file -p exit code: 0 = clean, 1..127 = conflict count, and a fatal
    # error surfaces as 128/255 (or a negative signal code). A fatal error is NOT a
    # clean merge — flag it (ok=False) so the caller can preserve the operator edit
    # rather than drop it under a false "clean" result.
    if res.returncode < 0 or res.returncode >= 128:
        return current, False, False
    return res.stdout, res.returncode > 0, True


def _reattach_operator_edits(
    project_root: Path, snapshots: dict[str, tuple[str, Optional[str]]]
) -> list[str]:
    """Replay operator edits detached by :func:`_detach_operator_edits`.

    The operator's diff (``head_content -> operator_content``) is merged ONTO the
    post-reconcile working-tree file via a 3-way merge, so BOTH reconcile's
    committed change AND the operator's edit survive. Writing the raw operator
    snapshot back (the old behavior) would instead revert reconcile's committed
    version/changelog change in the working tree — a reverse diff a later operator
    commit could accidentally land, silently dropping the release note. Runs on
    success (operator edit survives alongside the release) and on failure (the
    rolled-back path is at HEAD, so the merge simply restores the operator edit).
    Overlapping edits are surfaced as conflict markers plus a warning, never
    silently dropped.

    Returns the list of relpaths whose operator edit could NOT be replayed — a
    write/unlink failure (``EACCES``, ``ENOSPC``, …) left the detached operator
    edit absent from the working tree. Those failures must NOT be swallowed: the
    caller surfaces them as a typed reconcile failure rather than reporting
    success while the operator's edit is silently gone. Per-path best-effort
    otherwise (one path's failure never aborts replaying the rest).

    ``operator_content is None`` marks a pre-existing operator DELETION. Reconcile
    re-created and committed the file (the release artifact must exist in history),
    so the deletion and the release change genuinely conflict. The deletion is
    replayed in the working tree (the file is removed again, preserving the
    operator's uncommitted intent as a dirty deletion against the reconcile commit)
    and a warning is logged, rather than silently keeping reconcile's file or
    dropping the release note. This surfaces the divergence for manual resolution.
    """
    failed: list[str] = []
    for rel, (head_content, operator_content) in snapshots.items():
        path = Path(project_root) / rel
        if operator_content is None:
            # Replay the operator's deletion over reconcile's committed re-creation.
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                # The deletion could not be replayed; the file still carries
                # reconcile's content, so the operator's uncommitted deletion is
                # lost from the working tree — record it for the caller to surface.
                failed.append(rel)
                logger.warning(
                    "reconcile: operator had deleted %s before reconcile ran but "
                    "the deletion could not be replayed (unlink failed); the file "
                    "still holds reconcile's content — resolve manually",
                    rel,
                )
                continue
            logger.warning(
                "reconcile: operator had deleted %s before reconcile ran; the "
                "release change was committed and the deletion was replayed in "
                "the working tree — resolve the divergence manually",
                rel,
            )
            continue
        try:
            current_content = path.read_text(encoding="utf-8")
        except OSError:
            current_content = None
        if current_content is None:
            # reconcile left nothing readable there; restore the operator edit
            # verbatim rather than lose it.
            try:
                path.write_text(operator_content, encoding="utf-8")
            except OSError:
                failed.append(rel)
                logger.warning(
                    "reconcile: could not restore operator's uncommitted edit to "
                    "%s (write failed); the edit is absent from the working tree "
                    "— resolve manually",
                    rel,
                )
            continue
        merged, conflicted, ok = _three_way_merge(
            project_root, current_content, head_content, operator_content
        )
        if not ok:
            # ``git merge-file`` failed fatally: ``merged`` is just reconcile's
            # content, so writing it would silently drop the operator's edit while
            # reconcile still reports success. Instead lay down explicit conflict
            # markers preserving BOTH reconcile's committed content and the
            # operator's edit, and warn — the divergence is surfaced for manual
            # resolution rather than resolved by discarding the operator's work.
            fallback = (
                "<<<<<<< reconcile\n"
                + current_content
                + ("\n" if not current_content.endswith("\n") else "")
                + "=======\n"
                + operator_content
                + ("\n" if not operator_content.endswith("\n") else "")
                + ">>>>>>> operator\n"
            )
            try:
                path.write_text(fallback, encoding="utf-8")
            except OSError:
                # Even the conflict fallback could not be written; the operator's
                # edit is now absent from the working tree — surface it.
                failed.append(rel)
                logger.warning(
                    "reconcile: could not 3-way merge operator's uncommitted edit "
                    "to %s and could not write conflict markers (write failed); the "
                    "operator edit is absent from the working tree — resolve "
                    "manually",
                    rel,
                )
                continue
            logger.warning(
                "reconcile: could not 3-way merge operator's uncommitted edit to "
                "%s (git merge-file failed); wrote conflict markers preserving both "
                "reconcile's release change and the operator edit — resolve manually",
                rel,
            )
            continue
        try:
            path.write_text(merged, encoding="utf-8")
        except OSError:
            # The merged content could not be written back; the operator's edit is
            # absent from the working tree — surface it rather than swallow.
            failed.append(rel)
            logger.warning(
                "reconcile: could not write the merged operator edit to %s (write "
                "failed); the operator edit is absent from the working tree — "
                "resolve manually",
                rel,
            )
            continue
        if conflicted:
            logger.warning(
                "reconcile: operator's uncommitted edit to %s overlapped the "
                "release change; wrote conflict markers to the working tree "
                "instead of silently dropping either side",
                rel,
            )
    return failed


# Filename of the crash-durable operator-edit snapshot. It lives INSIDE the git
# dir (not the working tree) so it is invisible to ``git status`` — a marker in
# the tree would pollute every "clean tree" assertion and could ride into a
# commit — while still surviving a hard kill, so entry-time recovery can replay
# the operator's pre-existing edits that detach had reset to HEAD.
_RECOVERY_SNAPSHOT_FILENAME = "se3-reconcile-recovery.json"


def _recovery_snapshot_path(project_root: Path) -> Optional[Path]:
    """Absolute path of the crash-recovery snapshot inside this repo's git dir.

    Resolved via ``git rev-parse --absolute-git-dir`` so it is correct for a plain
    checkout as well as a linked worktree. Returns ``None`` (durability degrades to
    best-effort) if the git dir cannot be resolved — reconcile must never abort a
    merge over a missing backstop.
    """
    res = _run_git(
        project_root, "rev-parse", "--absolute-git-dir", check=False, timeout=15
    )
    if res.returncode != 0:
        return None
    git_dir = res.stdout.strip()
    if not git_dir:
        return None
    return Path(git_dir) / _RECOVERY_SNAPSHOT_FILENAME


def _persist_recovery_snapshot(
    project_root: Path, snapshots: dict[str, tuple[str, Optional[str]]]
) -> None:
    """Write the operator-edit snapshot durably before detach mutates the tree.

    No-op when there is no operator dirt (empty snapshot). When operator dirt IS
    present this MUST fail rather than degrade to best-effort: the caller invokes
    it at the capture→mutate boundary, and the detach that follows resets/unlinks
    those operator-owned paths to HEAD. If the durable copy could not be written,
    the ONLY surviving copy of the operator's pre-existing edit is the in-memory
    dict — which a subsequent hard kill (SIGKILL / OOM / power-loss during the
    minutes-long custom-rules LLM call) destroys with no on-disk recovery, so the
    next resume sees a clean tree and the edit is lost forever. Raising here — an
    event-time (2a) typed refusal, BEFORE detach mutates a single path — keeps the
    operator's dirt untouched so it can be committed/stashed and retried, rather
    than proceeding into an unrecoverable window. Called only for a real commit;
    a dry apply persists nothing.
    """
    if not snapshots:
        return
    path = _recovery_snapshot_path(project_root)
    if path is None:
        # Git dir unresolvable → no durable backstop can be written. With operator
        # dirt present, refuse before detach resets it to HEAD (see docstring).
        raise ReconcileError(
            "reconcile could not resolve the git directory to write a crash-recovery "
            "snapshot for the operator's uncommitted edit(s) to "
            + ", ".join(sorted(snapshots))
            + "; refusing to detach them without a durable backstop — commit or "
            "stash the change and retry."
        )
    payload = {
        "snapshots": {
            rel: {"head": head, "operator": operator}
            for rel, (head, operator) in snapshots.items()
        }
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        raise ReconcileError(
            f"reconcile could not write the crash-recovery snapshot to {path} "
            f"({exc}); refusing to detach the operator's uncommitted edit(s) to "
            + ", ".join(sorted(snapshots))
            + " without a durable backstop, since a hard kill would then lose them "
            "irrecoverably — commit or stash the change and retry."
        ) from exc


def _clear_recovery_snapshot(project_root: Path) -> None:
    """Remove the crash-recovery snapshot once the in-memory reattach has run."""
    path = _recovery_snapshot_path(project_root)
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _recover_operator_snapshot(project_root: Path) -> None:
    """Entry-time crash recovery: replay a durable operator-edit snapshot.

    A prior reconcile may have been hard-killed (SIGKILL / OOM / power-loss) after
    detaching operator edits to HEAD but before its reattach ``finally`` could
    replay them — a window that spans the whole version derivation, including a
    minutes-long custom-rules LLM call. The in-memory snapshot dies with that
    process, so without a durable copy the operator's pre-existing edits would be
    silently and permanently lost and the next run would see a clean tree. Replay
    the on-disk snapshot through the same 3-way reattach used on the normal path
    (tracked edits merge, untracked files are recreated, deletions replayed;
    overlaps surface as conflict markers, never silent loss), then clear it.

    The snapshot is cleared ONLY when replay fully succeeded. If reattach reports a
    path whose operator edit it could not write back (``EACCES`` / ``ENOSPC`` / …),
    the durable snapshot is the operator's ONLY remaining copy of that pre-crash
    edit, so it is preserved and a typed failure is raised — clearing it would
    delete the last copy and leave the edit gone with no recovery path. Aborting
    reconcile here is correct: the operator resolves the write failure and the next
    run retries the replay from the still-present snapshot.
    """
    path = _recovery_snapshot_path(project_root)
    if path is None:
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError as exc:
        # The snapshot EXISTS but a transient I/O failure (EACCES / ENOSPC / …)
        # blocked the read. Unlike a corrupt payload — whose content is already
        # lost, so dropping it costs nothing — the bytes here are intact on disk
        # and are the operator's ONLY surviving copy of a pre-crash edit. Returning
        # would let reconcile run to completion and then clear the still-unread
        # snapshot, silently deleting that last copy. Preserve it and abort with a
        # typed failure so the operator fixes the read fault and the next run
        # retries replay from the intact snapshot.
        raise ReconcileError(
            f"reconcile found a crash-recovery snapshot at {path} but could not "
            f"read it ({exc}); it holds the only surviving copy of the operator's "
            "uncommitted edit(s) from an interrupted prior run. The snapshot is "
            "preserved — resolve the read failure (permissions / disk) and re-run "
            "the merge to replay it."
        ) from exc
    try:
        data = json.loads(raw)
        raw_snaps = data["snapshots"]
        snapshots: dict[str, tuple[str, Optional[str]]] = {
            rel: (entry.get("head", ""), entry.get("operator"))
            for rel, entry in raw_snaps.items()
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        # An unreadable/corrupt backstop must not wedge every future reconcile;
        # drop it. (A genuine loss here is unrecoverable regardless.)
        _clear_recovery_snapshot(project_root)
        return
    if snapshots:
        logger.info(
            "reconcile: found a crash-recovery snapshot from an interrupted prior "
            "run; replaying %d operator edit(s) before reconciling",
            len(snapshots),
        )
        failed = _reattach_operator_edits(project_root, snapshots)
        if failed:
            # Do NOT clear: the snapshot holds the only surviving copy of these
            # operator edits, and clearing would lose them permanently. Surface a
            # typed failure so the next run retries replay from the preserved copy.
            raise ReconcileError(
                "reconcile recovered a crash snapshot from an interrupted prior run "
                "but could not replay the operator's uncommitted edit(s) to "
                + ", ".join(sorted(failed))
                + " (write/unlink failed); those edits are absent from the working "
                "tree. The recovery snapshot is preserved for a retry — resolve the "
                "write failure (permissions / disk space) and re-run the merge."
            )
    _clear_recovery_snapshot(project_root)


def _restore_reconcile_paths(project_root: Path) -> None:
    """Discard uncommitted writes to reconcile-owned files, restoring them to HEAD.

    Called to wipe a prior reconcile's half-applied bump — the version file,
    VERSIONS.md, README, and the consumed-flag writes — that never made it into a
    commit (the commit failed, or the process crashed before it). Recovery must
    reset BOTH the index and the working tree: ``_commit_reconcile`` ``git add``s
    these paths before the commit, so a failed ``git commit`` (e.g. a rejecting
    hook) leaves them *staged*. A plain ``git checkout -- <path>`` restores the
    worktree from that dirty index and leaves the staged version bump / consumed
    flag in place — a later reconcile would then see the consumed intent without
    a durable reconcile commit and either no-op or re-stage the dirt. Restoring
    ``HEAD -- <path>`` overwrites the index entry too, so both index and worktree
    return to the committed state and reconcile recomputes from a clean base;
    nothing else in the main checkout is touched. Best-effort per path (a repo
    without a README simply has nothing to restore); a stale ``consumed`` flag
    reverts to its committed ``consumed=False`` as a side effect of restoring the
    intents directory, so the intent is picked up as outstanding again on retry.
    """
    paths = ["VERSIONS.md", "README.md", VERSION_INTENT_DIR_RELPATH]
    try:
        version_file = _version_bumper(project_root).detect_version_file(
            Path(project_root)
        )
    except Exception:  # noqa: BLE001 - version-file detection must not abort recovery
        version_file = None
    if version_file is not None:
        try:
            paths.append(str(Path(version_file).relative_to(project_root)))
        except ValueError:
            paths.append(str(version_file))
    for path in paths:
        # A path ABSENT from HEAD (e.g. the first-ever VERSIONS.md in a project
        # that never committed one) cannot be restored by ``checkout HEAD -- <path>``
        # — there is no HEAD version, so git makes it a silent no-op and the
        # reconcile-created, staged phantom survives for the next reconcile to
        # misread as its base. Detect that case and instead unstage + delete the
        # phantom so recovery leaves a truly clean base. ``git cat-file -e`` works
        # on both blobs and trees, so the intents directory is handled too.
        in_head = (
            _run_git(
                project_root, "cat-file", "-e", f"HEAD:{path}",
                check=False, timeout=15,
            ).returncode
            == 0
        )
        if in_head:
            # ``checkout HEAD -- <path>`` (not the index-only ``checkout -- <path>``)
            # so staged changes are unstaged, not merely mirrored back into the tree.
            _run_git(
                project_root, "checkout", "HEAD", "--", path, check=False, timeout=15
            )
            continue
        # Absent from HEAD: unstage reconcile's phantom, then remove the file it
        # created. Only reconcile writes these paths at this point (operator dirt
        # was already detached and reset before reconcile ran), so deleting is safe.
        _run_git(
            project_root, "reset", "-q", "HEAD", "--", path, check=False, timeout=15
        )
        full = Path(project_root) / path
        try:
            if full.is_file():
                full.unlink()
        except OSError:
            pass


VERSION_TAG_TRAILER = "Version-Tag"


def reconcile_commit_tag(message: str) -> Optional[str]:
    """The release tag a reconcile commit claims ownership of, from its trailer.

    Absence means the decision created no tag, so nothing on that commit is the
    version flow's to delete — the tag name implied by the subject is NOT a
    substitute (an operator may have tagged ``v<version>`` themselves while the
    review gate was pending).
    """
    prefix = f"{VERSION_TAG_TRAILER}:"
    for line in (message or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip() or None
    return None


def is_version_tag_failure(error_message: str) -> bool:
    """Whether a reconcile error means a commit landed without its release tag.

    Both failure modes leave the same durable wreckage — a reconcile commit on
    HEAD with no annotated tag — and therefore need the same manual-recovery
    guidance: ``create_annotated_version_tag`` raising ``VersionTagError``, and
    ``_commit_reconcile`` failing to read back the hash it was about to tag.
    """
    text = error_message or ""
    return "failed to create version tag" in text or (
        "version tag" in text and "was NOT created" in text
    )


def _delete_version_tags_pointing_at(
    project_root: Path, commit: str, tag_name: Optional[str]
) -> None:
    """Delete the release tag this reconcile created on an undone commit.

    Scoped to the ONE tag name the rejected decision recorded in its
    ``Version-Tag`` trailer, not to every ``v``-prefixed tag on the commit: the
    reconcile commit sits on HEAD of a shared main checkout while a human review
    gate is pending, so an operator is free to have tagged it with tags of their
    own (``vendor-freeze``, ``v-rc-candidate``, or even ``v<final_version>`` when
    the decision itself tagged nothing). Those are not ours to delete. When
    *tag_name* is ``None`` (a non-tagging decision, or a commit predating the
    trailer) nothing is deleted.
    """
    if not tag_name:
        return
    refs = _run_git(
        project_root,
        "for-each-ref",
        f"refs/tags/{tag_name}",
        "--format=%(refname:short)%09%(objectname)%09%(*objectname)",
        check=False,
        timeout=15,
    )
    if refs.returncode != 0:
        raise ReconcileError(
            f"failed to inspect tags before revision recompute: "
            f"{refs.stderr.strip()}"
        )
    tags_to_delete: list[str] = []
    for line in refs.stdout.splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        name = parts[0].strip()
        if name != tag_name:
            continue
        object_name = parts[1].strip() if len(parts) > 1 else ""
        peeled_name = parts[2].strip() if len(parts) > 2 else ""
        if commit in {object_name, peeled_name}:
            tags_to_delete.append(name)
    for tag in tags_to_delete:
        delete = _run_git(
            project_root, "tag", "-d", tag, check=False, timeout=15
        )
        if delete.returncode != 0:
            raise ReconcileError(
                f"failed to delete stale version tag {tag} before revision "
                f"recompute: {delete.stderr.strip()}"
            )


def undo_last_reconcile(project_root: Path, flow_id: str) -> bool:
    """Un-commit *flow_id*'s reconcile commit while it is still ``HEAD``.

    Used by the human-review revision path: when a reviewer *rejects* the version
    decision, the already-created reconcile commit (version file + changelog +
    consumed intents, all committed atomically) must be undone so the intents
    become outstanding again and reconcile recomputes from a clean base — a
    plain re-run would otherwise see the git-durable reconcile trailer (which a
    ``git revert`` would NOT erase) and no-op, leaving the rejected version
    standing.

    Only safe while *this flow's* reconcile commit is still ``HEAD``: the CONFIRM
    gate that precedes the rejection creates no commit, so nothing has built on
    it. The HEAD check is FLOW-SCOPED — it must carry *this* flow's trailer
    (``Version-Reconcile-Session: <flow_id>``), not merely *some* reconcile
    trailer. The merge lock is released between merge_integrate and
    version_reconcile, so a concurrent flow's own reconcile commit can land on
    HEAD while the reviewer deliberates; undoing THAT would discard another
    flow's just-published release while leaving this flow's rejected (now buried)
    commit intact. A generic trailer check would do exactly that. When HEAD is
    not this flow's reconcile commit (a sibling's reconcile commit, a
    non-reconcile commit that buried ours, or no parent) it refuses (returns
    ``False``) rather than discarding another flow's work; the caller then
    decides fail-vs-recompute by whether this flow's commit is merely buried.

    Undo is SCOPED, not a repo-wide ``git reset --hard HEAD~1``: the worktree
    flow ran elsewhere, so the operator is free to have unrelated uncommitted
    edits in this main checkout, and a hard reset would delete every one of them.
    Instead the commit is moved back with a mixed reset (HEAD → parent, working
    tree preserved) and only the reconcile-owned paths are restored to the parent.
    Operator dirt that happens to sit ON a reconcile-owned content path (a README
    tweak, a dep bump in the version file) is not sacrificed either: it is detached
    and 3-way replayed around the reset, so it survives alongside the revert instead
    of being blind-restored away — consistent with run_merge's stash protection of
    main-checkout dirt.

    Returns ``True`` when a reconcile commit was undone, ``False`` when there was
    nothing safe to undo.
    """
    project_root = Path(project_root)
    head_msg = _run_git(
        project_root, "log", "-1", "--pretty=%B", check=False, timeout=15
    )
    # Match THIS flow's trailer as a full message line, not a substring: a
    # substring test would false-positive when one flow_id is a prefix of
    # another, and the generic-trailer test it replaces would false-positive on
    # any sibling flow's reconcile commit sitting at HEAD.
    marker = f"{RECONCILE_TRAILER}: {flow_id}"
    head_lines = {
        line.strip() for line in (head_msg.stdout or "").splitlines()
    }
    if head_msg.returncode != 0 or marker not in head_lines:
        return False
    # A parent must exist — refuse to reset a root commit away.
    parent = _run_git(
        project_root, "rev-parse", "--verify", "--quiet", "HEAD~1",
        check=False, timeout=15,
    )
    if parent.returncode != 0:
        return False
    head = _run_git(
        project_root, "rev-parse", "HEAD", check=False, timeout=15
    )
    if head.returncode != 0 or not head.stdout.strip():
        return False
    head_commit = head.stdout.strip()
    # The rejected decision's own release tag, read from the ``Version-Tag``
    # trailer the decision stamped when it created one. Trailer-scoped, not
    # subject-derived: a decision that tagged nothing (patch bump, custom rule
    # with ``is_tag: false``) leaves no trailer, so an operator's hand-made
    # ``v<version>`` on the pending commit is not ours to delete. Read before the
    # reset, while the commit is still HEAD.
    undone_tag = reconcile_commit_tag(head_msg.stdout or "")
    # Detach the operator's uncommitted edits on the reconcile-owned content paths
    # (version file / README / VERSIONS.md), snapshotted against the current
    # reconcile commit as their base, BEFORE unwinding it. Without this the scoped
    # restore below would blind-``checkout`` those paths from the parent and silently
    # delete a reviewer's unrelated dirty work (a dep bump in pyproject, a README
    # tweak) that happens to sit on a path reconcile also owns — the hole this fix
    # closes. The operator diff is replayed by the 3-way reattach in the finally, so
    # BOTH the reconcile revert AND the operator edit survive; the version-intents
    # directory (which the operator never edits) is still handled by the wholesale
    # restore.
    operator_snapshots = _detach_operator_edits(
        project_root,
        _reconcile_owned_relpaths(project_root),
        # Persist a crash-durable copy of the operator snapshots at the
        # capture→mutate boundary, exactly as the forward reconcile path does.
        # Without it a hard kill (SIGKILL/OOM) between the detach's mutate pass
        # (edits already reset to HEAD) and the reattach finally would lose the
        # reviewer's uncommitted edits with nothing for _recover_operator_snapshot
        # to replay on the next run.
        persist=lambda snaps: _persist_recovery_snapshot(project_root, snaps),
    )
    reattach_failed: list[str] = []
    try:
        # Mixed reset moves HEAD to the parent and resets the index but leaves the
        # working tree intact, so no unrelated operator edits elsewhere are lost. The
        # reconcile commit's own file changes now appear as uncommitted
        # modifications; the scoped restore below discards exactly those (and reverts
        # the consumed flags to the parent's committed state, making the intents
        # outstanding again).
        reset = _run_git(
            project_root, "reset", "--mixed", "HEAD~1", check=False, timeout=30
        )
        if reset.returncode != 0:
            raise ReconcileError(
                f"failed to undo prior reconcile commit: {reset.stderr.strip()}"
            )
        _restore_reconcile_paths(project_root)
        _delete_version_tags_pointing_at(project_root, head_commit, undone_tag)
    finally:
        # Replay the operator's detached edits onto the rolled-back (parent) content
        # via 3-way merge: on success the edit survives alongside the revert; on a
        # failed reset the owned paths are still at the reconcile commit, so the merge
        # simply restores the operator edit as found. Overlaps surface as conflict
        # markers, never silent loss. A reattach that cannot write leaves the
        # operator edit absent — captured so the success path surfaces it.
        if operator_snapshots:
            try:
                reattach_failed = _reattach_operator_edits(
                    project_root, operator_snapshots
                )
            except (subprocess.SubprocessError, OSError) as reattach_exc:
                # A git call inside the 3-way reattach can time out / fault under
                # this host's known lock contention. Raised from a finally it
                # would escape UNTYPED and replace any in-flight ReconcileError
                # from the try — swallow it, mark every snapshotted path
                # un-replayed so the durable recovery snapshot is preserved for
                # next-run replay and the success path raises a typed failure.
                logger.warning(
                    "reconcile: replaying operator edits failed (%s); the durable "
                    "recovery snapshot is preserved for next-run replay",
                    reattach_exc,
                )
                reattach_failed = sorted(operator_snapshots)
        # The in-memory reattach ran on both the success and graceful-raise paths
        # (i.e. NOT a hard kill), so the crash-durable backstop is no longer
        # needed — clear it, unless reattach itself failed, in which case the
        # durable snapshot is the operator's remaining copy and entry-time
        # recovery should still get a chance to replay it next run.
        if not reattach_failed:
            _clear_recovery_snapshot(project_root)
    # Only reached when the undo itself succeeded (a failed reset re-raises in the
    # finally). If an operator's pre-existing edit could not be replayed, the undo
    # detached it and left it absent from the working tree — surface a typed
    # failure rather than reporting a clean undo with the operator's work gone.
    if reattach_failed:
        raise ReconcileError(
            "undid the prior reconcile commit but could not replay the operator's "
            "uncommitted edit(s) to "
            + ", ".join(sorted(reattach_failed))
            + " (write/unlink failed); those edits are absent from the working "
            "tree — restore them manually"
        )
    logger.info("reconcile: undid prior reconcile commit for revision recompute")
    return True


# --- entry point -------------------------------------------------------------

def reconcile(
    project_root: Path,
    *,
    flow_ids: Optional[list[str]] = None,
    llm_call: Optional[Callable[[str], str]] = None,
    commit: bool = True,
    revision_feedback: Optional[str] = None,
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
        revision_feedback: Optional reviewer feedback (from a rejected prior
            decision) threaded into the custom-rules LLM prompt so the recompute
            can honour it. Ignored by the deterministic channel.

    Returns:
        A :class:`ReconcileResult`. A run with nothing outstanding is a success
        with ``already_reconciled=True`` and ``channel="noop"``.

    Raises:
        ReconcileError / VersionRegressionError: on a genuine fault (unparseable
        current version, regression/collision, write or commit failure).
    """
    project_root = Path(project_root)

    # Version bumping disabled for this project: preserve the "no automatic
    # version bump" contract. version_analyze emits no intent in this mode, so
    # there is normally nothing outstanding — but even if a stale intent survives
    # from when versioning was enabled, we must NOT read/write a version file (it
    # may not exist) nor fail. No-op success, leaving the working tree untouched.
    try:
        from ...config import load_version_config

        version_enabled = load_version_config(project_root).enabled
    except Exception:  # noqa: BLE001 - config load must not abort the merge
        version_enabled = True
    if not version_enabled:
        logger.info(
            "reconcile: version bumping is disabled for this project; skipping "
            "version derivation (no automatic bump)."
        )
        return ReconcileResult(
            success=True,
            base_version=None,
            channel="noop",
            already_reconciled=True,
            version_disabled=True,
        )

    # Entry-time crash recovery (change #3 crash-safety backstop): a prior reconcile
    # may have been hard-killed after detaching operator edits to HEAD but before its
    # reattach finally could replay them. Replay that durable on-disk snapshot NOW —
    # before anything reads or mutates the reconcile-owned paths — so the operator's
    # pre-existing edits are never silently lost across a kill that never reached the
    # in-memory reattach. Runs even on the eventual no-op path (a crash may have
    # committed the reconcile yet still failed to replay). commit-only: a dry apply
    # persists no snapshot, so there is nothing to recover.
    if commit:
        _recover_operator_snapshot(project_root)

    # Collect ALL intents (including on-disk-consumed) so a genuinely-completed
    # reconcile can be told apart from an interrupted one: a ``consumed`` flag is
    # only trustworthy when the reconcile commit that was meant to carry it
    # actually exists.
    intents = collect_intents(project_root, include_consumed=True)
    wanted: Optional[set[str]] = None
    if flow_ids is not None:
        wanted = set(flow_ids)
        intents = [i for i in intents if i.flow_id in wanted]

        # A SCOPED reconcile (flow_ids given) is an explicit claim by the caller
        # — the ``se3 merge`` CLI / merge_integrate step compute the scope from
        # the integrated branches' committed intent filenames — that EACH of
        # these flows was introduced by the branch just merged and MUST
        # contribute its version decision. ``collect_intents`` silently drops an
        # intent whose JSON is corrupt/invalid (``read_intent_file`` -> None). If
        # such a drop emptied the scope we would fall straight through to the
        # "nothing outstanding" no-op below and publish the merge as CLEAN with
        # no bump/changelog — stranding the very feature the caller asked us to
        # reconcile (the redesign's core failure mode). So a requested flow_id
        # that is neither readable here NOR already reconciled (a durable
        # reconcile commit exists for it) is an unreadable/missing intent that
        # has not been applied: a hard fault, NOT an empty scope. An
        # already-reconciled flow_id is fine — its decision has already landed.
        readable_ids = {i.flow_id for i in intents}
        unaccounted = [
            fid
            for fid in sorted(wanted - readable_ids)
            if not reconcile_commit_exists(project_root, fid)
        ]
        if unaccounted:
            raise ReconcileError(
                "version-intent(s) requested for reconcile could not be read and "
                "have no prior reconcile commit (corrupt or missing intent JSON): "
                f"{', '.join(unaccounted)}. The branch is integrated but its "
                "version decision cannot be derived; restore or repair the intent "
                "file(s) and rerun the merge."
            )

    # The AUTHORITATIVE "already reconciled" signal is the git-durable reconcile-
    # commit trailer, NOT the on-disk ``consumed`` flag. That flag is only an
    # auxiliary record meant to land atomically INSIDE the reconcile commit; a
    # consumed flag without a matching reconcile commit is residue — a run that
    # crashed after marking but before committing, or a stale flag some unrelated
    # commit swept into HEAD. Trusting such a flag (as the old ``is_consumed``
    # filter did, OR-ing the file flag in) would strand a feature with no
    # committed version, the exact failure the redesign exists to prevent. So an
    # intent is outstanding iff no reconcile commit for it exists.
    def _still_outstanding(items: list[VersionIntent]) -> list[VersionIntent]:
        return [
            i
            for i in items
            if not reconcile_commit_exists(project_root, i.flow_id)
        ]

    outstanding = _still_outstanding(intents)

    if not outstanding:
        # A no-op stays a no-op: tag creation is scoped to the reconcile commit
        # THIS call creates. Scanning history for a reconcile commit that lost its
        # tag (a crash between ``git commit`` and ``git tag``) would let an
        # unrelated merge resurrect a tag the operator deleted on purpose;
        # recovery from a tag failure is a manual ``git tag -a``.
        # Reconcile ran; there was simply nothing left to apply. Not a fault.
        # Checked BEFORE any detach/residue handling so a run with nothing to do
        # never touches the operator's working tree.
        logger.info("reconcile: no outstanding intents; nothing to bump")
        return ReconcileResult(
            success=True,
            base_version=read_current_version(project_root),
            channel="noop",
            already_reconciled=True,
        )

    # Detach operator dirt from ALL reconcile-owned CONTENT paths (version file +
    # docs) BEFORE reading the base version or recovering residue, then replay it
    # via 3-way merge in the finally below. Doing it FIRST is what lets the base
    # version be read from HEAD instead of a dirty working-tree version (closing the
    # double-bump window WITHOUT discarding operator edits), while keeping operator
    # work out of the reconcile commit and safe from the failure rollback.
    #
    # It also SUBSUMES crash-residue cleanup on these content paths: a prior
    # reconcile's half-applied version/changelog write is dirty exactly like operator
    # dirt, so resetting it to HEAD here neutralizes it too — and because the
    # deterministic channel reproduces that same bump, the 3-way replay collapses the
    # identical residue against reconcile's re-write while still preserving any
    # genuine operator edit layered on the same file (overlaps surface as conflict
    # markers, never silent loss — the design's prescribed "snapshot dirty diff,
    # re-apply on HEAD, replay operator diff" recovery). This is the fix for the old
    # blind ``_restore_reconcile_paths`` residue restore, which reset these paths to
    # HEAD before any snapshot and thus silently deleted a retry-time operator edit.
    # Only for a real commit — a dry apply (commit=False) never commits or rolls
    # back, so it writes in place and reads its base from the working tree as before.
    #
    # The detach itself, the crash-residue reset, AND the whole version derivation +
    # apply phase are ALL wrapped in the ONE try/finally that owns the operator-edit
    # reattach. The detach mutates reconcile-owned paths (resetting operator dirt to
    # HEAD), so any fault AFTER the first such mutation — a ``git checkout`` timing
    # out under lock contention, the residue-reset git call stalling, a mid-apply
    # write error — must still reach the reattach finally, or the operator's
    # snapshotted edits are silently lost. ``operator_snapshots`` is created BEFORE
    # the try and passed into ``_detach_operator_edits`` so a mid-loop raise still
    # leaves the caller holding every snapshot taken so far.
    operator_snapshots: dict[str, tuple[str, Optional[str]]] = {}

    # READ-ONLY replayability pre-pass, run BEFORE the try. An un-replayable
    # divergent staged state is a hard refusal that must propagate WITHOUT the
    # except's ``_restore_reconcile_paths`` blind-reset firing — that reset would
    # wipe operator edits on paths the detach never even reached (nothing was
    # snapshotted yet, so the reattach finally could not restore them). Validating
    # up front keeps such a refusal side-effect-free; the detach inside the try
    # re-runs the same read-only check (harmless) and only then mutates.
    if commit:
        try:
            for _rel in _reconcile_owned_relpaths(project_root):
                _assert_staged_state_replayable(project_root, _rel)
        except ReconcileError:
            raise
        except (subprocess.SubprocessError, OSError) as exc:
            # This read-only pre-pass runs BEFORE the try/finally, so a git
            # stall/timeout here (``git status`` exceeding its 15s cap under the
            # lock contention this host is known for) would escape reconcile()
            # UNTYPED and surface as a raw traceback on the CLI path instead of
            # the documented reconcile-failure recovery rendering. The pre-pass
            # mutates nothing, so mapping it to a typed ReconcileError is safe.
            raise ReconcileError(
                "reconcile could not verify the replayability of its owned "
                f"paths (git error: {exc}); commit or stash any change and retry."
            ) from exc

    # The ENTIRE version derivation + apply phase is wrapped in one rollback and a
    # reattach finally. ANY mid-apply failure (an unwritable VERSIONS.md, a failing
    # commit hook, a regression) must leave NO half-applied, uncommitted bump
    # behind: a version file left dirty at e.g. 11.13.0 with no reconcile commit
    # would be re-read by the NEXT reconcile as its base and advance to 11.14.0 —
    # one intent bumping the version twice and stranding 11.13.0 as a
    # permanently-skipped ghost, and the stale dirty edit swept into the eventual
    # reconcile commit. Rolling reconcile-owned paths back to HEAD guarantees the
    # FAILED step leaves a clean tree and a resume recomputes from the committed
    # base. (The entry-time recovery is the backstop for a hard kill that never
    # reaches this handler.) The rollback is gated on ``commit``: a deliberate dry
    # apply (commit=False) keeps its writes.
    reconcile_commit: Optional[str] = None
    consumed_flow_ids: list[str] = []
    reattach_failed: list[str] = []
    is_tag = False
    tag_name: Optional[str] = None
    tag_created = False
    # Whether reconcile has begun WRITING its own output (version file / changelog
    # / consumed flags). The failure rollback below (_restore_reconcile_paths, a
    # blind ``checkout HEAD --`` over ALL reconcile-owned paths) is only safe once
    # the apply phase started — by then detach has fully completed, so every dirty
    # path is snapshotted and the reattach finally can replay it. A fault DURING
    # detach (a transient ``git status`` / ``git checkout`` failure) must NOT
    # trigger that blind restore: it would reset a not-yet-snapshotted dirty path to
    # HEAD, and the finally — which replays only snapshotted paths — could not
    # restore it, silently discarding an operator edit reconcile had not even
    # written over yet. Gating on this flag scopes the discard to residue reconcile
    # itself produced.
    apply_started = False
    try:
        # Detach operator dirt from ALL reconcile-owned CONTENT paths (version file
        # + docs) BEFORE reading the base version or recovering residue, then replay
        # it via 3-way merge in the finally below. Doing it FIRST is what lets the
        # base version be read from HEAD instead of a dirty working-tree version
        # (closing the double-bump window WITHOUT discarding operator edits), while
        # keeping operator work out of the reconcile commit and safe from the
        # failure rollback. It also SUBSUMES crash-residue cleanup on these content
        # paths: a prior reconcile's half-applied version/changelog write is dirty
        # exactly like operator dirt, so resetting it to HEAD here neutralizes it
        # too — and because the deterministic channel reproduces that same bump, the
        # 3-way replay collapses the identical residue against reconcile's re-write
        # while still preserving any genuine operator edit layered on the same file
        # (overlaps surface as conflict markers, never silent loss).
        if commit:
            _detach_operator_edits(
                project_root,
                _reconcile_owned_relpaths(project_root),
                operator_snapshots,
                # Persist a crash-durable copy of the operator snapshots at the
                # capture→mutate boundary (inside detach), before the first reset —
                # the backstop entry-time recovery replays after a hard kill.
                persist=lambda snaps: _persist_recovery_snapshot(
                    project_root, snaps
                ),
            )

        # Recover the remaining, operator-untouchable half of crash residue: an
        # outstanding intent whose ``consumed`` flag is set with no reconcile commit
        # (a signal an operator never produces) means a prior reconcile marked-then-
        # died. Its content half is already neutralized by the detach above; only
        # the consumed-flag directory is left. Reset ONLY ``se3/version-intents`` to
        # HEAD — a path the operator never edits, so this loses no operator work — so
        # the intent re-registers as outstanding and its flag flip lands atomically
        # inside the fresh reconcile commit rather than as pre-existing dirt. Skipped
        # for a dry apply. Inside the try so a stall here still reattaches snapshots.
        if commit and any(i.consumed for i in outstanding):
            logger.info(
                "reconcile: detected residue from an interrupted prior reconcile; "
                "reverting the consumed-flag directory to HEAD and recomputing from "
                "the committed base"
            )
            _run_git(
                project_root, "checkout", "HEAD", "--",
                VERSION_INTENT_DIR_RELPATH, check=False, timeout=15,
            )
            intents = collect_intents(project_root, include_consumed=True)
            if wanted is not None:
                intents = [i for i in intents if i.flow_id in wanted]
            outstanding = _still_outstanding(intents)

        current_version = read_current_version(project_root)
        if not current_version:
            raise ReconcileError(
                "could not read master's current version from the project's "
                "configured version file"
            )

        rules_text = _read_version_rules(project_root)
        declared_bump = bool(_collect_bumps(outstanding))

        # The changelog substance the merged intents carry. Computed here (before
        # the channel branch) because it participates in the deterministic no-bump
        # decision below: an intent carrying a changelog bullet is a versionable,
        # user-facing change even when its auxiliary bump_type hint was absent.
        changelog_entries = _merge_changelog_entries(outstanding)

        if rules_text is None:
            channel = "deterministic"
            final_version, chosen_bump = compute_deterministic(
                current_version, outstanding
            )
            # compute_deterministic leaves final == current when no intent declared
            # a bump, which is a legitimate no-op ONLY when there is nothing to file.
            # If any intent carries a changelog bullet, that bullet IS a versionable
            # change: consuming the intent while writing neither a version nor a
            # VERSIONS.md entry would permanently drop the note (and a later resume
            # would skip it, the reconcile commit now existing). So when there is
            # changelog substance but no declared bump, apply a PATCH so the substance
            # lands under a real released version. The phantom-patch the design avoids
            # is one for work with NO versionable change — empty changelog stays a
            # true no-op here.
            if chosen_bump is None and changelog_entries:
                chosen_bump = BumpType.PATCH
                final_version = str(Version.parse(current_version).bump(BumpType.PATCH))
                declared_bump = True
            bump_label: Optional[str] = (
                chosen_bump.value if chosen_bump is not None else None
            )
            is_tag = should_tag_semver_bump(bump_label)
        else:
            channel = "custom-rules"
            call = llm_call or _default_llm_call(project_root)
            final_version, is_tag = compute_via_rules(
                project_root,
                current_version,
                outstanding,
                rules_text,
                call,
                revision_feedback=revision_feedback,
            )
            bump_label = None

        # A deterministic merge where no intent declared a bump AND no intent carried
        # a changelog bullet leaves the version unchanged (compute_deterministic
        # returned final == current, and the changelog-substance override above did not
        # fire). There is no release to publish, so writing the version file (a no-op)
        # and a VERSIONS.md block would fabricate a changelog entry for work with no
        # versionable change. Skip both; the intents are still consumed and a reconcile
        # commit (carrying the consumed-flag flips) is still created so the decision is
        # durable and resume never recomputes. (An intent WITH changelog substance but
        # no bump never reaches here as a no-op — the override forced a patch above, so
        # its bullet lands under a real version rather than being silently dropped.)
        #
        # The custom-rules channel has NO legitimate no-op: an LLM result equal to
        # the current version means the model failed to produce an advancing version,
        # and the design mandates a typed failure (unconsumed intents, no reconcile
        # commit) rather than silently swallowing the release behind a no-op commit a
        # future resume would treat as done. Force publish_release True there so
        # validate_no_regression below runs and raises on the equal-to-current case;
        # only the deterministic channel may legitimately settle on final == current.
        #
        # The deterministic decision is driven by ``chosen_bump`` (None ⇒ no bump and
        # no changelog substance ⇒ genuine no-op), NOT by a string compare of
        # final vs current. A raw ``final_version != current_version`` misfires when
        # the on-disk version is written non-canonically (e.g. "1.2" or "1.02.3"):
        # compute_deterministic canonicalizes to "1.2.0"/"1.2.3", so the strings
        # differ even though the version did not advance, spuriously publishing and
        # then tripping validate_no_regression's "does not advance current" on a
        # numerically-equal pair.
        publish_release = channel == "custom-rules" or chosen_bump is not None
        if not publish_release:
            is_tag = False
        if is_tag:
            tag_name = tag_name_for_version(final_version)

        # Validate only when actually publishing a new number. The no-regress /
        # no-collision contract is about a NEW released version; the no-bump no-op
        # (final == current) is not a release, and current is by definition already
        # the historical head, so running validate_no_regression on it would
        # spuriously trip its "collides with an already-released version" guard.
        if publish_release:
            validate_no_regression(
                current_version,
                final_version,
                declared_bump=declared_bump,
                custom_rules=(rules_text is not None),
                historical=historical_versions(project_root),
            )

        # From here on reconcile is writing its OWN output, so the failure rollback
        # is now safe (detach fully completed → every operator path is snapshotted).
        apply_started = True
        version_file: Optional[Path] = None
        if publish_release:
            version_file = _write_final_version(project_root, final_version)
            _merge_changelog(project_root, final_version, changelog_entries)

        # Mark consumed BEFORE committing so the consumed intent files ship
        # inside the reconcile commit itself — the on-disk flag and the trailer
        # then land atomically, and a re-entry sees both.
        for intent in outstanding:
            # Count each intent handled this run whether we wrote the flag or it
            # was already set (a raced / resumed marking); mark_consumed is a
            # safe no-op in the already-flagged case.
            mark_consumed(project_root, intent.flow_id)
            consumed_flow_ids.append(intent.flow_id)

        if commit:
            message = _build_commit_message(
                final_version, current_version, channel, outstanding,
                tag_name=tag_name if (publish_release and is_tag) else None,
            )
            # allow_empty: reaching here means outstanding intents are being
            # consumed with commit=True, and upstream (reconcile_commit_exists in
            # _still_outstanding) already proved no reconcile commit yet exists —
            # so we MUST create one. Its trailer is the sole durable idempotency
            # signal; the no-bump path where the consumed flag was already
            # committed produces no file diff, and without allow_empty
            # _commit_reconcile would return None, leaving the session forever
            # outstanding despite the step reporting "complete".
            reconcile_commit = _commit_reconcile(
                project_root, message, version_file=version_file,
                allow_empty=True,
                intended_tag=tag_name if (publish_release and is_tag) else None,
            )
            if publish_release and is_tag and reconcile_commit:
                try:
                    tag_name = create_annotated_version_tag(
                        project_root, final_version, reconcile_commit
                    )
                except VersionTagError as exc:
                    raise ReconcileError(str(exc)) from exc
                tag_created = True
    except (
        ReconcileError,
        subprocess.SubprocessError,
        OSError,
        KeyError,
        ValueError,
    ) as exc:
        # KeyError / ValueError are included deliberately: the apply phase routes
        # through DocumentationUpdater, which can raise KeyError ('Template
        # versions_entry not found') or ValueError on a broken/missing docs
        # template AFTER ``apply_started`` — those must trigger the same scoped
        # rollback of reconcile-produced residue (half-written version file +
        # changelog) rather than escaping and leaving the main checkout dirty.
        # IntentReadError (RuntimeError, not in this tuple) is intentionally NOT
        # caught here so it propagates to the caller as its own type — the step
        # handler and CLI adapter map it to a graceful FAILED/exit, and wrapping
        # it as a generic ReconcileError would erase that typed distinction.
        # Only wipe reconcile-owned paths once the apply phase started (see
        # ``apply_started``): a fault during detach must not blind-reset a
        # not-yet-snapshotted operator edit the reattach finally could never
        # restore. When apply had not started, detach left every path it DID
        # mutate snapshotted, and any un-mutated dirty path still holds the
        # operator's content untouched — so the finally alone fully restores.
        if commit and apply_started:
            _restore_reconcile_paths(project_root)
        if isinstance(exc, ReconcileError):
            raise
        raise ReconcileError(f"reconcile failed during apply: {exc}") from exc
    finally:
        # Replay operator edits on both paths: on success they survive alongside
        # reconcile's committed change (3-way merged, not overwritten); on failure
        # the rollback above reset these paths to HEAD, so the merge simply
        # restores the operator edits exactly as found. A reattach that itself
        # cannot write (EACCES/ENOSPC/…) leaves the operator edit detached and
        # absent from the working tree; capture those paths so the success path
        # below refuses to report success with the operator's edit silently gone.
        if operator_snapshots:
            try:
                reattach_failed = _reattach_operator_edits(
                    project_root, operator_snapshots
                )
            except (subprocess.SubprocessError, OSError) as reattach_exc:
                # A git call inside the 3-way reattach (``git merge-file`` under
                # this host's known lock contention) can time out / fault. Raised
                # from a finally, it would escape UNTYPED and, worse, REPLACE any
                # in-flight ReconcileError from the except above — breaking
                # run_merge's typed-failure contract and losing the original
                # error. Swallow it: mark every snapshotted path un-replayed so
                # (a) the durable recovery snapshot below is preserved for
                # entry-time replay next run (the operator's edits stay safe) and
                # (b) on the success path the ``reattach_failed`` check raises a
                # typed ReconcileError; on the in-flight-error path the original
                # exception propagates unmasked.
                logger.warning(
                    "reconcile: replaying operator edits failed (%s); the durable "
                    "recovery snapshot is preserved for next-run replay",
                    reattach_exc,
                )
                reattach_failed = sorted(operator_snapshots)
        # The in-memory reattach ran (this finally is reached on BOTH the success
        # and the graceful-raise path — i.e. NOT a hard kill), so the crash-durable
        # backstop is no longer needed. Clear it — unless reattach itself failed,
        # in which case the durable snapshot is the operator's remaining copy and
        # entry-time recovery should still get a chance to replay it next run.
        if commit and not reattach_failed:
            _clear_recovery_snapshot(project_root)

    # Only reached on the success path (a mid-apply fault re-raises in the except).
    # If reattach could not put an operator's pre-existing edit back on a
    # reconcile-owned path, the version bump committed but the operator's edit is
    # gone from the working tree — a genuine fault the redesign requires us to
    # surface, not swallow. The reconcile commit is durable, so a resume sees it
    # exists and never re-bumps; the operator restores their edit manually.
    if reattach_failed:
        raise ReconcileError(
            "reconcile committed the version change but could not replay the "
            "operator's uncommitted edit(s) to "
            + ", ".join(sorted(reattach_failed))
            + " (write/unlink failed); those edits are absent from the working "
            "tree — restore them manually"
        )

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
        is_tag=is_tag,
        tag_name=tag_name,
        tag_created=tag_created,
    )
