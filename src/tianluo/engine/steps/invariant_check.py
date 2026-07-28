"""Invariant Check step handler.

Replaces the retired ``spec_gate`` / ``spec_check`` per-requirement drift
machinery. INVARIANT_CHECK is an **anchored** check: it asks the LLM whether
the diff violates any invariant that has been *explicitly recorded*, where the
closed anchor set is::

    {task_description, charter full text, why-comments of the touched code}

frozen at flow start. It reuses the verbatim_quote anchoring that ``self_check``
adopted after its free-text schema let ungrounded "nits" spin the fix loop: an
issue survives validation only when its ``expectation_source.verbatim_quote`` is
a literal substring of the anchor pool. This makes the coverage face honest —
**only invariants that were written down (as a charter rule or a why-comment, or
that the task itself demands) are machine-guarded**; unwritten expectations are
deliberately not. We explicitly reject an anchor-less self-check: when there are
no anchors (no charter, no why-comments, no task text), the step cheap-passes
rather than inventing invariants to enforce.

Routing: a surviving violation returns ``REVISION_NEEDED`` (the state machine
routes it into the existing fix loop, same as TEST / SELF_CHECK / VERIFY_SPEC).
No diff, or no anchors, returns ``COMPLETED`` (cheap pass) — this is what makes
the ``review`` flow (``analyze -> invariant_check -> summarize``) pass for free
when it changed nothing.
"""

from __future__ import annotations

import difflib
import logging
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from ..charter import load_charter
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ._project_root import resolve_flow_project_root
from ..prompt_markers import inject_boundary
from ..utils.json_parser import parse_json_response
from ...config import DEFAULT_MAX_FIX_ITERATIONS
from ._fix_context import format_fix_iteration_display
# Reuse self_check's anchoring machinery verbatim — single source of truth so
# the two anchored checks cannot drift in their normalization / validation.
from .self_check import (
    _changed_paths,
    _describe_issue,
    _normalize_for_quote_match,
    _validate_and_filter_issues,
)

logger = logging.getLogger(__name__)


# Comment prefixes we treat as carrying potential "why" intent when harvesting
# the touched files' colocated comments into the anchor pool. Best-effort and
# language-agnostic: we keep the comment text (after the marker) so a
# verbatim_quote can substring-match the human-written rationale.
_COMMENT_MARKERS = ("#", "//")

# Marker prefixes (checked on the comment BODY, after the ``#`` / ``//`` marker
# has been stripped) that flag a comment as carrying *binding* intent — a why /
# invariant the author explicitly asked to be protected. A diff that DELETES or
# rewrites such a marked comment (without restoring it or declaring a reason) is
# hard-guarded into REVISION_NEEDED (缺口二); unmarked comments never block, so
# the mechanical set-diff cannot spin the fix loop on debug/refactor noise.
_WHY_MARKER_PREFIXES = ("WHY:", "INVARIANT:")


def _is_marked_why_comment(body: str) -> bool:
    """True when ``body`` (a marker-stripped comment) carries a WHY:/INVARIANT: tag.

    Case-insensitive; the colon is required so ``# WHY not`` prose does not trip
    the hard guard. Only explicitly tagged comments are machine-protected.
    """
    upper = body.strip().upper()
    return any(upper.startswith(p) for p in _WHY_MARKER_PREFIXES)


def _extract_comments(text: str) -> list[str]:
    """Return the text of single-line comments (``#`` / ``//``) in ``text``.

    Best-effort textual harvest (not an AST pass): keeps the body after the
    comment marker, dropping empty markers.
    """
    comments: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        for marker in _COMMENT_MARKERS:
            if stripped.startswith(marker):
                body = stripped[len(marker):].strip()
                if body:
                    comments.append(body)
                break
    return comments


def _read_baseline_file(
    project_root: Path, baseline_commit: str | None, rel: str
) -> str | None:
    """Read ``rel`` as it existed at the flow's frozen baseline commit.

    Uses ``git show <baseline>:<rel>`` so the ORIGINAL pre-implementation text
    is recovered even after the working tree edited or deleted the file.
    Returns ``None`` (silently) when there is no baseline, the file did not
    exist at baseline, or git is unavailable.
    """
    if not baseline_commit:
        return None
    try:
        result = subprocess.run(
            ["git", "show", f"{baseline_commit}:{rel}"],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _harvest_why_comments(
    project_root: Path,
    changed_files: set[str],
    baseline_commit: str | None = None,
) -> list[str]:
    """Collect colocated comment lines from the touched code files.

    The new knowledge system puts *why / intent* into colocated comments, so the
    why-comments of the changed code are part of the anchor set. Crucially, the
    harvest reads BOTH the file's **baseline** content (as frozen at flow start,
    before ``implement`` could touch it) AND its current working-tree content,
    merging the two comment sets per file. This closes the gap where an
    implementation that deletes or rewrites a why-comment documenting a binding
    invariant — while violating that invariant — would otherwise erase the
    original quote from the anchor pool and let the violation slip through: the
    baseline copy preserves the original quote regardless of what the diff did,
    and the working-tree copy still surfaces any newly added intent comments.

    Unreadable or binary files are skipped silently. Returns one string per file
    (its merged comment block joined by newlines) so ``_build_source_pool`` can
    treat each as a pool entry.
    """
    out: list[str] = []
    for rel in sorted(changed_files):
        if not isinstance(rel, str) or not rel:
            continue
        comments: list[str] = []
        seen: set[str] = set()

        def _add(text: str) -> None:
            for body in _extract_comments(text):
                if body not in seen:
                    seen.add(body)
                    comments.append(body)

        # Baseline (pre-implementation) content first — preserves the ORIGINAL
        # why-comment anchor text even if the diff edited or deleted it.
        baseline_text = _read_baseline_file(project_root, baseline_commit, rel)
        if baseline_text is not None:
            _add(baseline_text)

        # Working-tree content — surfaces newly added intent comments.
        path = project_root / rel
        try:
            if path.is_file():
                _add(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            pass

        if comments:
            out.append("\n".join(comments))
    return out


def _deleted_comments_by_file(
    project_root: Path,
    changed_files: set[str],
    baseline_commit: str | None,
) -> dict[str, list[str]]:
    """Per-file set-diff of colocated comments: baseline HAS, working tree LACKS.

    For each changed file, compare the comment set at the frozen baseline (via
    ``git show <baseline>:<rel>``) against the current working-tree comment set;
    a comment present at baseline but absent now was **deleted or rewritten** (a
    rewrite makes the old body vanish, which is a "deletion" of the old text —
    exactly the silent knowledge loss 缺口二 guards). Returns ``{rel: [body, …]}``
    for files that lost at least one comment, preserving baseline order.

    Silently skips (contributes nothing) when there is no baseline commit, the
    file did not exist at baseline (a newly added file loses nothing), or either
    revision is binary/unreadable — mirroring ``_harvest_why_comments``' best-
    effort, never-raise contract so this side-channel can never break the step.
    """
    out: dict[str, list[str]] = {}
    if not baseline_commit:
        return out
    for rel in sorted(changed_files):
        if not isinstance(rel, str) or not rel:
            continue
        baseline_text = _read_baseline_file(project_root, baseline_commit, rel)
        if baseline_text is None:
            # No baseline copy (new file, or unreadable at baseline) → nothing to
            # have lost. Skip silently.
            continue
        # Working-tree comment set. A file deleted outright reads as empty, so
        # every baseline comment counts as lost (the whole file's intent is gone).
        working_text = ""
        path = project_root / rel
        try:
            if path.is_file():
                working_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Unreadable working copy: we cannot reliably diff, so skip rather
            # than flag every baseline comment as a phantom deletion.
            continue

        # INVARIANT: multiset (occurrence-counted) difference, not a plain set
        # difference. If a file holds two identical comments and the diff removes
        # only one, exactly one occurrence must count as deleted — a set
        # membership test would see the surviving copy and silently drop the loss.
        working_counts = Counter(_extract_comments(working_text))
        deleted: list[str] = []
        for body in _extract_comments(baseline_text):
            if working_counts.get(body, 0) > 0:
                working_counts[body] -= 1
            else:
                deleted.append(body)
        if deleted:
            out[rel] = deleted
    return out


def _marked_comments_with_pos(text: str) -> list[tuple[int, str]]:
    """Return ``(line_index, body)`` for each MARKED (WHY:/INVARIANT:) comment.

    The line index lets the pairing prefer the positionally-nearest candidate when
    a file has several marked comments. Unlike the retired code-slot correlation it
    does NOT inspect the surrounding code, so a comment that was rewritten *together
    with the code it annotates* is still eligible to pair with its replacement —
    "the comment moves with its code" is the typical in-place re-declaration, which
    must never be forced to keep the annotated code byte-identical.
    """
    out: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        marker = next((m for m in _COMMENT_MARKERS if stripped.startswith(m)), None)
        if marker is None:
            continue
        body = stripped[len(marker):].strip()
        if body and _is_marked_why_comment(body):
            out.append((i, body))
    return out


def _rewritten_marked_exemptions(
    project_root: Path,
    changed_files: set[str],
    baseline_commit: str | None,
) -> dict[str, Counter]:
    """Per-file count of deleted marked comment occurrences re-declared in the diff.

    A deleted marked comment is EXEMPT from the hard guard when the diff carries an
    updated WHY:/INVARIANT: comment that stands in for it — the second accepted exit
    ("record the reason in an updated comment"). The rule is a purely mechanical
    one-to-one pairing, and this is the SOLE authoritative semantics: a deleted
    marked comment is exempt **if and only if** it pairs one-to-one with a marked
    comment newly added or rewritten **in the same file**. Pairing does NOT require
    the annotated code to stay byte-identical, so ``# WHY: use SQLite`` above
    ``DATABASE='sqlite'`` rewritten to ``# WHY: use Postgres for HA`` above
    ``DATABASE='postgres'`` — or a rewrite of a comment at end-of-file — still
    counts as an in-place re-declaration. When counts are unequal, pairs are chosen
    greedily by text similarity then positional proximity; each replacement exempts
    AT MOST one deletion, so a net loss (more deletions than new marked comments)
    still leaves the unpaired deletions guarded.

    Pairing is strictly SAME-FILE. A marked comment that vanishes from one file and
    reappears verbatim in *another* changed file is NOT exempted — an unpaired
    same-file deletion always triggers REVISION_NEEDED, regardless of any verbatim
    reappearance elsewhere. (A cross-file channel would let an unrelated addition in
    some other file swallow a genuine loss, and would falsely consume a legitimate
    in-place rewrite whose new text happens to match a deletion elsewhere. The exit
    for a genuine move is the same as for any relocation: re-declare the rationale
    with a marked comment in the file that lost it, which the same-file pairing then
    naturally exempts.)

    Same best-effort, never-raise contract as its siblings (no baseline / unreadable
    file → contributes nothing). A brand-new file (no baseline) contributes only
    additions; a file deleted outright reads as an empty working tree so all its
    baseline marked comments count as deleted.
    """
    out: dict[str, Counter] = {}
    if not baseline_commit:
        return out

    for rel in sorted(changed_files):
        if not isinstance(rel, str) or not rel:
            continue
        baseline_text = _read_baseline_file(project_root, baseline_commit, rel)
        working_text = ""
        path = project_root / rel
        try:
            if path.is_file():
                working_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Unreadable working copy: cannot diff reliably, skip (mirror sibling).
            continue
        # A file absent at baseline contributes no deletions, only additions.
        base = _marked_comments_with_pos(baseline_text) if baseline_text is not None else []
        work = _marked_comments_with_pos(working_text)
        # INVARIANT: occurrence-counted (multiset) diff, not set membership. Two
        # identical marked comments where the diff rewrites only one must yield
        # exactly one deletion and one addition to pair; a set test would cancel
        # both against the surviving copy and see no change at all.
        work_counts = Counter(b for _, b in work)
        dels: list[tuple[int, str]] = []
        for pos, b in base:
            if work_counts.get(b, 0) > 0:
                work_counts[b] -= 1
            else:
                dels.append((pos, b))
        base_counts = Counter(b for _, b in base)
        adds: list[tuple[int, str]] = []
        for pos, b in work:
            if base_counts.get(b, 0) > 0:
                base_counts[b] -= 1
            else:
                adds.append((pos, b))
        if not dels or not adds:
            continue

        # Greedy one-to-one pairing WITHIN this file only. An in-place rewrite may
        # share no words with the old rationale, so every (deletion, addition) pair
        # is eligible; highest similarity first, then closest position, ``di``/``ai``
        # keep the sort stable. Each addition exempts at most one deletion.
        candidates: list[tuple[float, int, int, int]] = []
        for di, (dpos, dbody) in enumerate(dels):
            for ai, (apos, abody) in enumerate(adds):
                similarity = difflib.SequenceMatcher(None, dbody, abody).ratio()
                proximity = -abs(dpos - apos)
                candidates.append((similarity, proximity, di, ai))
        candidates.sort(key=lambda c: (-c[0], -c[1], c[2], c[3]))

        used_del: set[int] = set()
        used_add: set[int] = set()
        for _similarity, _proximity, di, ai in candidates:
            if di in used_del or ai in used_add:
                continue
            used_del.add(di)
            used_add.add(ai)
            # Count exemptions per body occurrence, not as a set. Each pairing
            # exempts exactly ONE deleted occurrence; storing a bare set would let
            # a single pairing exempt every identical deletion of that body.
            out.setdefault(rel, Counter())[dels[di][1]] += 1
    return out


def _build_why_comment_guard_issues(
    deleted_by_file: dict[str, list[str]],
    exempt_by_file: dict[str, Counter] | None = None,
) -> list[dict]:
    """Synthesize anchored issues for deleted/rewritten WHY:/INVARIANT: comments.

    缺口二 hard guard: only comments the author explicitly tagged with a
    WHY:/INVARIANT: prefix are protected. Each such lost comment becomes an
    issue whose ``verbatim_quote`` is the comment body itself — which is still in
    the anchor pool because ``_harvest_why_comments`` reads the baseline copy —
    so it survives ``_validate_and_filter_issues``' verbatim-quote check. The
    issue is grounded via ``missing_in`` (the file that should have kept the
    comment), and ``expected_behavior`` names the two accepted exits: restore the
    comment, or record the reason in an updated WHY:/INVARIANT: comment.

    ``exempt_by_file`` maps each file to a ``Counter`` of deleted marked comment
    bodies → how many occurrences were re-declared in the diff; each occurrence is
    consumed once so identical duplicates are exempted individually
    (``_rewritten_marked_exemptions`` pairs a dropped
    comment one-to-one with a newly added/rewritten marked comment IN THE SAME FILE,
    so a completely-reworded rationale, or a comment rewritten together with its
    code, is still recognised as a re-declaration). The pairing is one-to-one and
    same-file: each new marked comment exempts at most one deletion, so a net loss
    (more marked deletions than new marked comments) still leaves the unpaired
    deletions guarded, and a comment that merely reappears in another file is not
    exempt.
    """
    exempt_by_file = exempt_by_file or {}
    issues: list[dict] = []
    for rel in sorted(deleted_by_file):
        # Copy so we can decrement: each exemption covers ONE deleted occurrence.
        exempt = Counter(exempt_by_file.get(rel) or {})
        for body in deleted_by_file[rel]:
            if not _is_marked_why_comment(body):
                continue
            # A marked comment re-declared at the SAME code slot IS the "updated
            # WHY:/INVARIANT: comment" exit — the author re-declared the rationale
            # in place, so treat the dropped old text as a rewrite, not a loss.
            # Consume one exemption per paired occurrence; an unpaired identical
            # deletion (more removals than re-declarations) still routes to fix.
            if exempt.get(body, 0) > 0:
                exempt[body] -= 1
                continue
            issues.append({
                "severity": "high",
                "actual_behavior": (
                    f"the diff deleted or rewrote a marked intent comment in "
                    f"{rel} without restoring it: \"{body}\""
                ),
                "expected_behavior": (
                    "A WHY:/INVARIANT: comment records binding intent and must "
                    "not be silently dropped. Either restore the comment, or, if "
                    "the intent genuinely changed, record the new reasoning in an "
                    "updated WHY:/INVARIANT: comment at the same location."
                ),
                "divergence": (
                    f"after this change {rel} no longer carries the recorded "
                    f"intent \"{body}\", so the knowledge is lost from the "
                    f"code and from the next flow's anchor pool"
                ),
                "expectation_source": {
                    "type": "why_comment",
                    "verbatim_quote": body,
                },
                "evidence_lines": [],
                "missing_in": [rel],
            })
    return issues


def _build_anchor_inputs(step: Step, flow: FlowInstance, project_root: Path) -> dict:
    """Assemble the synthetic step_inputs that drives self_check's validators.

    We reuse ``self_check._validate_and_filter_issues`` (and the
    ``_build_source_pool`` it calls) unchanged. Those read:

    - ``task_description_base`` — the clean task text (anchor #1)
    - ``spec_content`` (non-``base`` entries) — here we inject the charter full
      text (anchor #2) and the harvested why-comments (anchor #3) under
      descriptive keys, so they enter the verbatim_quote source pool.
    - ``changes_made.files_changed`` — the touched paths, used to validate each
      issue's ``evidence_lines``.

    The anchor sources are read from ``step.inputs`` first (the state machine
    freezes them at flow start); the charter falls back to an on-disk read so
    the handler is self-sufficient when those inputs are absent.
    """
    task_description = (
        step.inputs.get("task_description")
        or flow.task_description
        or ""
    )

    charter_text = step.inputs.get("charter")
    if not (isinstance(charter_text, str) and charter_text):
        charter_text = load_charter(project_root)

    changes_made = step.inputs.get("changes_made") or {}
    if not isinstance(changes_made, dict):
        changes_made = {}
    changed = _changed_paths({"changes_made": changes_made})

    # why-comments: prefer an explicit frozen list from inputs, else harvest.
    # The harvest reads the baseline (pre-implementation) copy of each touched
    # file as well as the working tree, so the original why-comment anchor text
    # survives even when the diff deleted or rewrote it.
    why_comments = step.inputs.get("why_comments")
    if isinstance(why_comments, list):
        why_comments = [w for w in why_comments if isinstance(w, str) and w.strip()]
    else:
        baseline_commit = getattr(flow, "baseline_commit", None)
        why_comments = _harvest_why_comments(project_root, changed, baseline_commit)

    spec_content: dict[str, str] = {}
    if isinstance(charter_text, str) and charter_text.strip():
        spec_content["charter"] = charter_text
    if why_comments:
        spec_content["why_comments"] = "\n\n".join(why_comments)

    return {
        "task_description_base": task_description,
        "spec_content": spec_content,
        "changes_made": changes_made,
    }


def _anchor_pool_is_empty(anchor_inputs: dict) -> bool:
    """True when the frozen anchor set yields no usable text to anchor against.

    Mirrors the substring-pool construction of ``_build_source_pool`` +
    ``_normalize_for_quote_match``: if every anchor normalizes to empty, there
    is nothing recorded to guard, so we must NOT run an anchor-less check.
    """
    from .self_check import _build_source_pool

    pool = [_normalize_for_quote_match(s) for s in _build_source_pool(anchor_inputs)]
    return not any(p for p in pool)


INVARIANT_CHECK_PROMPT = """You are an invariant auditor. Your ONLY job is to decide whether the diff below VIOLATES an invariant that has been **explicitly written down** in the anchored material.

## Task Description
{task_description}

## Charter (project-wide binding conventions — anchor)
{charter}

## Why-comments of the touched code (intent recorded next to the code — anchor)
{why_comments}

## Changes Made (the diff under audit)
{changes_made}

## What an invariant violation is
A *binding invariant* is a rule that the Task Description, the Charter, or a touched-code why-comment states EXPLICITLY. A violation is the diff doing something that contradicts such a recorded rule.

## HARD scope limit (this is the whole point of this check)
You may ONLY report a violation when you can quote the recorded rule **verbatim** from the anchored material above. If a concern is real but is not written down anywhere above, it is OUT OF SCOPE here — do NOT report it. Unwritten expectations are intentionally not machine-guarded. This is NOT a free code review: do NOT report style, missing tests, performance, or "nice to have" concerns, and do NOT report anything a downstream specialized step owns (e.g. version bumping is decided by version_analyze).

## Constitutional-amendment exemption
If the diff edits `tianluo/charter.md` AND the Task Description explicitly calls for that charter change, the specific clauses the task directs you to modify MUST NOT be cited as a violation: a task-sanctioned amendment to the charter is legitimate governance, not a breach of the (now-superseded) old text. Do not read a directed rewrite of an old rule as a violation of that old rule. All OTHER invariants — and every charter clause the task did NOT name — still apply in full.

## Issue Schema (HARD requirements — handler validates and drops violators)
Each issue MUST be a JSON object with:
- `severity`: one of "critical" / "high" / "medium" / "low"
- `actual_behavior`: what the diff does that violates the invariant (concrete, observable; non-empty)
- `expected_behavior`: what the recorded invariant requires instead (non-empty)
- `divergence`: the concrete input / sequence / state under which the diff breaks the invariant (non-empty)
- `expectation_source`: where the invariant is recorded. Must be:
    {{ "type": "task_description" | "charter" | "why_comment",
       "verbatim_quote": "<a literal substring copied from the Task Description, Charter, or a why-comment above>" }}
  The handler normalizes both the quote and the anchor pool and DROPS any issue whose quote is not a literal substring of the anchored material. Quote the substantive rule, not a generic word.
- `evidence_lines`: array of `"path:N"` strings whose `path` appears in the Changes Made files. At least one entry required UNLESS `missing_in` is non-empty.
- `missing_in`: array of file paths that the invariant required to be edited but were not.

Report ONLY recorded-invariant violations. Everything you list that passes validation goes straight into a fix loop and WILL be changed in the code on the spot — there is no discard channel for concerns that are merely observations, preferences, or unrelated to a recorded invariant. Leave those out entirely.

Respond in JSON format:
```json
{{
    "issues": [
        {{
            "severity": "critical|high|medium|low",
            "actual_behavior": "...",
            "expected_behavior": "...",
            "divergence": "...",
            "expectation_source": {{
                "type": "charter",
                "verbatim_quote": "..."
            }},
            "evidence_lines": ["src/foo.py:42"],
            "missing_in": []
        }}
    ],
    "summary": "Brief statement of whether any recorded invariant is violated"
}}
```

If the diff violates no recorded invariant, return an empty issues array.
"""

# Two-segment marker only: USER_CONTENT region is empty (no user literal here).
INVARIANT_CHECK_PROMPT = inject_boundary(INVARIANT_CHECK_PROMPT, "## Task Description\n")


WHY_LOSS_REJUDGE_PROMPT = """You are triaging comment deletions for lost *intent* knowledge.

The diff below deleted or rewrote the following single-line comments (they were present at the flow's baseline but are gone from the working tree now). NONE of them carried an explicit WHY:/INVARIANT: marker (those are handled elsewhere), so this is an ADVISORY triage only — nothing here blocks the flow.

Your ONLY job: decide which of these deletions represent a **meaningful loss of "why / intent" knowledge** — the reasoning behind a non-obvious trade-off, a constraint, or the reason a code path exists. EXCLUDE noise:
- debug / commented-out code / TODO scaffolding,
- a comment that merely restates what the code does,
- a comment that was clearly MOVED (its text likely reappears elsewhere in the diff, e.g. a refactor relocating a block across files — in a per-file diff a move looks like a deletion),
- trivial or obvious remarks.

## Deleted comments (per file)
{deleted_block}

Respond in JSON:
```json
{{
    "losses": [
        {{ "file": "<path the comment was deleted from>",
           "comment": "<the deleted comment text, verbatim>",
           "why_it_matters": "<one line: what intent knowledge is lost>" }}
    ]
}}
```
If none of them is a meaningful why/intent loss, return an empty losses array.
"""


def _format_deleted_comments_block(losses_by_file: dict[str, list[str]]) -> str:
    """Render the non-marked deleted comments for the advisory re-judge prompt."""
    lines: list[str] = []
    for rel in sorted(losses_by_file):
        bodies = losses_by_file[rel]
        if not bodies:
            continue
        lines.append(f"### {rel}")
        for body in bodies:
            lines.append(f"- {body}")
    return "\n".join(lines) if lines else "(none)"


def _rejudge_why_losses(
    project_root: Path,
    flow: FlowInstance,
    step: Step,
    losses_by_file: dict[str, list[str]],
) -> list[dict]:
    """Advisory-only LLM triage of unmarked comment deletions (缺口二).

    Filters the full mechanical set-diff down to deletions that are a *meaningful*
    why/intent loss (dropping debug/move/restatement noise), returning a list of
    ``{file, comment, why_it_matters}`` dicts destined for ``step.outputs`` only —
    NEVER the fix loop. Any failure (no losses, LLM error, unparsable / malformed
    response) degrades silently to ``[]`` so this side-channel cannot affect the
    main audit's return value.
    """
    if not losses_by_file:
        return []
    try:
        prompt = WHY_LOSS_REJUDGE_PROMPT.format(
            deleted_block=_format_deleted_comments_block(losses_by_file),
        )
        caller = LLMCaller(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value,
        )
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint=(
                '{"losses": [{"file": "...", "comment": "...", '
                '"why_it_matters": "..."}]}'
            ),
            required_keys=["losses"],
        )
        result = parse_json_response(response, required_keys=["losses"])
        if not result:
            return []
        losses = result.get("losses", [])
        if not isinstance(losses, list):
            return []
        cleaned: list[dict] = []
        for entry in losses:
            if not isinstance(entry, dict):
                continue
            comment = entry.get("comment")
            if isinstance(comment, str) and comment.strip():
                cleaned.append({
                    "file": entry.get("file", ""),
                    "comment": comment,
                    "why_it_matters": entry.get("why_it_matters", ""),
                })
        return cleaned
    except Exception:
        # Advisory side-channel: never let a triage failure disturb the audit.
        logger.info("why-comment loss re-judge failed; degrading to none", exc_info=True)
        return []


def _has_changed_files(changes_made: Any) -> bool:
    """True iff ``changes_made`` reports at least one changed file/path."""
    if not isinstance(changes_made, dict):
        return False
    return bool(_changed_paths({"changes_made": changes_made}))


def _format_changes(changes_made: dict[str, Any]) -> str:
    """Render the diff's file list for the prompt (mirror self_check's view)."""
    if not changes_made:
        return "No changes recorded."
    lines: list[str] = []
    for fc in changes_made.get("files_changed", []):
        if isinstance(fc, str):
            lines.append(f"- modified: {fc}")
        elif isinstance(fc, dict):
            path = fc.get("path", "?")
            action = fc.get("action", "?")
            explanation = fc.get("explanation", "")
            lines.append(f"- {action}: {path}")
            if explanation:
                lines.append(f"  ({explanation})")
        else:
            lines.append(f"- {fc}")
    return "\n".join(lines) if lines else "Changes made but details unavailable."


def invariant_check_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the invariant_check step.

    Returns COMPLETED when the diff violates no recorded invariant (or when
    there is no diff / no anchors — the cheap-pass cases). Returns
    REVISION_NEEDED when at least one anchored invariant violation survives
    validation, letting the state machine route the fix loop and handle
    exhaustion centrally.
    """
    project_root = resolve_flow_project_root(flow)

    changes_made = step.inputs.get("changes_made") or {}

    # Cheap pass #1 — no diff (the review flow's invariant_check lands here).
    if not _has_changed_files(changes_made):
        logger.info("invariant_check: no diff to audit — passing (cheap).")
        step.outputs["issues"] = []
        step.outputs["actionable_count"] = 0
        step.outputs["skipped_reason"] = "no_diff"
        return StepStatus.COMPLETED

    anchor_inputs = _build_anchor_inputs(step, flow, project_root)

    # Cheap pass #2 — no anchors recorded. We explicitly REFUSE an anchor-less
    # check: with nothing written down there is no binding invariant to guard,
    # and inventing one would be exactly the ungrounded self-check this design
    # rejects.
    if _anchor_pool_is_empty(anchor_inputs):
        logger.info(
            "invariant_check: no recorded anchors (charter / why-comments / "
            "task) — passing (cheap); anchor-less check refused."
        )
        step.outputs["issues"] = []
        step.outputs["actionable_count"] = 0
        step.outputs["skipped_reason"] = "no_anchors"
        return StepStatus.COMPLETED

    fix_iteration = step.inputs.get("fix_iteration", 0)
    raw_max = step.inputs.get("max_fix_iterations")
    max_iterations = (
        raw_max if isinstance(raw_max, int) and not isinstance(raw_max, bool)
        else DEFAULT_MAX_FIX_ITERATIONS
    )

    task_description = anchor_inputs["task_description_base"]
    spec_content = anchor_inputs["spec_content"]
    charter_text = spec_content.get("charter", "_(no charter recorded)_")
    why_comments_text = spec_content.get(
        "why_comments", "_(no why-comments harvested from the touched code)_"
    )

    prompt = INVARIANT_CHECK_PROMPT.format(
        task_description=task_description or "_(none)_",
        charter=charter_text,
        why_comments=why_comments_text,
        changes_made=_format_changes(changes_made),
    )

    logger.info("Running invariant check (anchored to charter + why-comments)...")

    try:
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value,
            external_attempt=retry_count,
            fix_iteration=fix_iteration,
        )
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint=(
                '{"issues": [{"severity": "critical|high|medium|low", '
                '"actual_behavior": "...", "expected_behavior": "...", '
                '"divergence": "...", '
                '"expectation_source": {"type": "charter|task_description|why_comment", "verbatim_quote": "..."}, '
                '"evidence_lines": ["path:N"], "missing_in": []}], '
                '"summary": "..."}'
            ),
            required_keys=["issues"],
        )
        result = parse_json_response(response, required_keys=["issues"])
        if not result:
            step.error_message = "Failed to parse invariant-check result from LLM response"
            return StepStatus.FAILED

        raw_issues = result.get("issues", [])

        # Anchor each issue against {task_description, charter, why-comments}.
        kept_issues, validation_stats = _validate_and_filter_issues(
            raw_issues, anchor_inputs,
        )

        dropped = validation_stats["input_count"] - validation_stats["kept_count"]
        if dropped > 0:
            reasons = ", ".join(
                f"{k}={v}" for k, v in validation_stats.items()
                if k.endswith("_count") and v > 0
                and k not in ("kept_count", "input_count")
            )
            logger.info(
                "invariant_check validation: %d raw -> %d kept (dropped %d: %s)",
                validation_stats["input_count"],
                validation_stats["kept_count"],
                dropped, reasons,
            )

        step.outputs["invariant_check_result"] = result
        step.outputs["raw_issues"] = raw_issues
        step.outputs["validation_stats"] = validation_stats

        # -----------------------------------------------------------------
        # 缺口二: guard the knowledge artifacts (why-comments) themselves.
        # A diff that DELETES/rewrites a comment WITHOUT violating it slips past
        # the anchored audit above (nothing contradicts a recorded rule). Two
        # channels, split by explicit author intent:
        #   • hard guard — comments tagged WHY:/INVARIANT: become anchored issues
        #     that join the fix loop (their body is still in the anchor pool via
        #     the baseline harvest, so they pass verbatim-quote validation);
        #   • advisory — every OTHER deletion goes through a single LLM triage
        #     (meaningful why loss vs debug/move/restatement noise) and lands in
        #     step.outputs only, NEVER the fix loop. Feeding the raw set-diff into
        #     a hard fix loop would revive self_check's old anchor-less nit churn.
        baseline_commit = getattr(flow, "baseline_commit", None)
        changed_paths = _changed_paths({"changes_made": changes_made})
        deleted_by_file = _deleted_comments_by_file(
            project_root, changed_paths, baseline_commit
        )
        exempt_by_file = _rewritten_marked_exemptions(
            project_root, changed_paths, baseline_commit
        )

        guard_raw = _build_why_comment_guard_issues(
            deleted_by_file, exempt_by_file
        )
        hard_violations: list[dict] = []
        if guard_raw:
            hard_violations, _ = _validate_and_filter_issues(
                guard_raw, anchor_inputs,
            )
            if hard_violations:
                logger.warning(
                    "invariant_check: %d marked why-comment(s) deleted/rewritten "
                    "without restoration — routing to fix loop.",
                    len(hard_violations),
                )
        step.outputs["why_comment_hard_violations"] = hard_violations

        # Advisory: full set-diff MINUS the marked (hard-guarded) comments.
        advisory_by_file: dict[str, list[str]] = {}
        for rel, bodies in deleted_by_file.items():
            unmarked = [b for b in bodies if not _is_marked_why_comment(b)]
            if unmarked:
                advisory_by_file[rel] = unmarked
        why_comment_losses = _rejudge_why_losses(
            project_root, flow, step, advisory_by_file
        )
        step.outputs["why_comment_losses"] = why_comment_losses

        # Merge the hard-guard violations with the LLM's anchored violations.
        kept_issues = kept_issues + hard_violations
        step.outputs["issues"] = kept_issues
        step.outputs["actionable_count"] = len(kept_issues)

        if not kept_issues:
            logger.info("invariant_check passed (no recorded invariant violated).")
            return StepStatus.COMPLETED

        return _build_fix_outputs(step, kept_issues, fix_iteration, max_iterations)

    except Exception as e:
        logger.exception("Invariant-check step failed")
        step.error_message = f"Invariant check failed: {e}"
        return StepStatus.FAILED


def _build_fix_outputs(
    step: Step,
    issues: list,
    fix_iteration: int,
    max_iterations: int,
) -> StepStatus:
    """Populate fix-loop outputs from validated invariant violations.

    Mirrors ``self_check._build_fix_outputs`` so the state machine and the
    renderers consume invariant_check's REVISION_NEEDED the same way they
    consume self_check's.
    """
    iter_display = format_fix_iteration_display(fix_iteration, max_iterations)
    logger.warning(
        "invariant_check entering fix with %d invariant violation(s) "
        "(fix iteration %s)",
        len(issues), iter_display,
    )

    issue_details = "\n".join(
        f"- [{i.get('severity', 'high')}] {_describe_issue(i)}"
        for i in issues
    )
    fix_instructions = (
        f"Invariant check found {len(issues)} recorded-invariant violation(s):\n"
        f"{issue_details}\n\n"
        "Fix the diff so it no longer contradicts the quoted invariant(s)."
    )

    step.outputs["issues"] = issues
    step.outputs["actionable_count"] = len(issues)
    step.outputs["fix_needed"] = True
    step.outputs["fix_iteration"] = fix_iteration
    step.outputs["max_fix_iterations"] = max_iterations
    step.outputs["fix_instructions"] = fix_instructions
    step.outputs["fix_context"] = {
        "reason": "invariant_check",
        "issues": issues,
        "iteration": fix_iteration + 1,
    }
    return StepStatus.REVISION_NEEDED
