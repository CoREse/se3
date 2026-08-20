"""Persistent baselines and round state for diff-scoped SELF_CHECK.

Review baselines describe the workspace as it existed immediately before the
first IMPLEMENT call, or immediately before one FIX call.  Clean tracked files
refer to their immutable git blobs; dirty tracked files and pre-existing
untracked files are copied into a content-addressed store under the project's
gitignored runtime state.  That split keeps the common case compact while still
making a dirty working tree exactly reconstructable after a process restart.

The baseline is observational: capture and comparison never modify the user's
working tree or index.  Materialized diff artifacts also live under runtime
state and are inputs to SELF_CHECK, not project changes.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..runtime_paths import runtime_dir, runtime_dir_name

logger = logging.getLogger(__name__)

BASELINE_SCHEMA_VERSION = 1
_SAFE_BASELINE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# The finite closed set of transient runtime-state subtree names under the
# project's runtime directory (the same closed set the commit step uses for its
# leak signature; kept local because review_scope must stay importable without
# the commit step). Only paths beneath these subtrees are excluded from review
# baselines — every other path under tianluo/ (e2e/, issues/, prompts/,
# charter.md, ...) is a project asset and must be reviewed like any change.
_RUNTIME_STATE_SUBTREES = frozenset(
    {
        "cache", "history", "logs", "state", "tmp", "worktrees", "calls",
        "collab", "uploads",
    }
)
_HUNK_RE = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass
class ReviewBaseline:
    """JSON-serializable descriptor for one review baseline."""

    baseline_id: str
    kind: str
    flow_id: str
    captured_at: str
    project_root: str
    repository_identity: str = ""
    head_commit: str = ""
    tracked: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    untracked: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    available: bool = True
    diagnostics: List[str] = field(default_factory=list)
    schema_version: int = BASELINE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Optional["ReviewBaseline"]:
        """Deserialize a descriptor, rejecting unsafe or malformed records."""
        if not isinstance(data, dict):
            return None
        baseline_id = data.get("baseline_id")
        if not isinstance(baseline_id, str) or not _SAFE_BASELINE_ID_RE.match(
            baseline_id
        ):
            return None
        tracked = data.get("tracked", {})
        untracked = data.get("untracked", {})
        diagnostics = data.get("diagnostics", [])
        if not isinstance(tracked, dict) or not isinstance(untracked, dict):
            return None
        if not isinstance(diagnostics, list):
            return None
        try:
            schema_version = int(data.get("schema_version", 0))
        except (TypeError, ValueError):
            return None
        if schema_version != BASELINE_SCHEMA_VERSION:
            return None
        return cls(
            baseline_id=baseline_id,
            kind=str(data.get("kind", "unknown")),
            flow_id=str(data.get("flow_id", "")),
            captured_at=str(data.get("captured_at", "")),
            project_root=str(data.get("project_root", "")),
            repository_identity=str(data.get("repository_identity", "")),
            head_commit=str(data.get("head_commit", "")),
            tracked={str(k): dict(v) for k, v in tracked.items() if isinstance(v, dict)},
            untracked={
                str(k): dict(v) for k, v in untracked.items() if isinstance(v, dict)
            },
            available=bool(data.get("available", False)),
            diagnostics=[str(item) for item in diagnostics],
            schema_version=schema_version,
        )


@dataclass
class ReviewScope:
    """Reconstructed baseline-to-current diff supplied to SELF_CHECK."""

    requested_mode: str
    scope_mode: str
    baseline_id: str = ""
    changed_paths: List[str] = field(default_factory=list)
    # ``causal_anchors`` carries only NEW-side (current-file) line ranges of
    # added lines — the one numbering space a ``path:N`` evidence citation can
    # legally reference. Old-side line numbers of deleted lines live in
    # ``deletion_anchors`` instead: mixing them in here would let a citation to
    # an unchanged (or nonexistent) current line pass evidence validation.
    causal_anchors: Dict[str, List[List[int]]] = field(default_factory=dict)
    deletion_anchors: Dict[str, List[List[int]]] = field(default_factory=dict)
    unified_diff: str = ""
    artifact_path: str = ""
    undecidable: bool = False
    diagnostic: str = ""
    fallback_from_incremental: bool = False
    # WHY the whole-task anchors travel ALONGSIDE the round's own anchors
    # instead of being merged into them: an incremental round's attention is
    # the fix delta (``changed_paths`` / ``causal_anchors``), but its EVIDENCE
    # domain is every line this flow really changed — a finding anchored in
    # work an earlier IMPLEMENT/FIX did is grounded in fact and must not be
    # discarded as fabricated. Merging the two sets would erase which baseline
    # a path/range came from, and the scope manifest has to be able to tell the
    # checker "this hunk is the fix you just made, that one is earlier work".
    # They stay empty for a full round: there the round baseline IS the
    # implementation baseline, so a second copy would carry no new information.
    task_baseline_id: str = ""
    task_changed_paths: List[str] = field(default_factory=list)
    task_causal_anchors: Dict[str, List[List[int]]] = field(default_factory=dict)
    task_deletion_anchors: Dict[str, List[List[int]]] = field(default_factory=dict)
    task_artifact_path: str = ""
    task_scope_available: bool = False
    task_scope_diagnostic: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# The role names a caller may ask a flow's baselines for. Ids are never a
# public selector: which concrete baseline plays the "fix" role is a persisted
# round-state decision (see ``earliest_unreviewed_fix_baseline``), not something
# an operator can be expected to reproduce by hand.
BASELINE_SELECTOR_IMPLEMENTATION = "implementation"
BASELINE_SELECTOR_FIX = "fix"
BASELINE_SELECTORS = (BASELINE_SELECTOR_IMPLEMENTATION, BASELINE_SELECTOR_FIX)

# INVARIANT: these four outcomes stay mutually exclusive and separately
# reportable. "never captured" (the flow has not crossed the baseline boundary
# yet), "captured but unusable" (git could not answer at capture time) and
# "reclaimed" (the snapshot directory was cleaned at flow termination) are three
# different operator situations with three different remedies; collapsing any of
# them into a generic "no baseline" tells the operator nothing actionable.
BASELINE_STATUS_OK = "ok"
BASELINE_STATUS_NOT_CAPTURED = "not_captured"
BASELINE_STATUS_UNAVAILABLE = "unavailable"
BASELINE_STATUS_CLEANED = "cleaned"


@dataclass
class BaselineLookup:
    """Outcome of resolving one persisted baseline by its role in the flow."""

    status: str
    selector: str
    baseline: Optional[ReviewBaseline] = None
    baseline_id: str = ""
    diagnostic: str = ""

    @property
    def ok(self) -> bool:
        return self.status == BASELINE_STATUS_OK and self.baseline is not None


def requirement_fingerprint(description: str) -> str:
    """Stable identity for the effective requirement text of a review round."""
    return hashlib.sha256((description or "").encode("utf-8")).hexdigest()


class SelfCheckRoundController:
    """Persisted lifecycle for full/incremental multi-pass review rounds."""

    def __init__(self, context: Dict[str, Any]):
        # WHY the round state is attached lazily: merely CONSTRUCTING a
        # controller must not make a flow look as if it already carried round
        # state. Callers distinguish a pre-upgrade flow (no persisted round) by
        # probing ``context['self_check_review']`` — a materialize-on-read
        # __init__ would answer "state exists" for every flow the moment any
        # code path built a controller, silently discarding the legacy
        # pass-index adoption that keeps a resumed multi-pass chain from
        # restarting at pass #1.
        self._context = context
        raw = context.get("self_check_review")
        self._attached = isinstance(raw, dict)
        self.state: Dict[str, Any] = raw if isinstance(raw, dict) else {}

    def _attach(self) -> None:
        """Publish the round state into the flow context before mutating it.

        Adopts state another controller published in the meantime instead of
        replacing it: two controllers may be built from one context before
        either writes, and clobbering would drop the first one's bookkeeping.
        """
        if self._attached:
            return
        existing = self._context.get("self_check_review")
        if isinstance(existing, dict) and existing is not self.state:
            existing.update(self.state)
            self.state = existing
        else:
            self._context["self_check_review"] = self.state
        self._attached = True

    @property
    def active_round(self) -> Optional[Dict[str, Any]]:
        value = self.state.get("active_round")
        return value if isinstance(value, dict) else None

    def prepare_round(
        self,
        *,
        requirement_text: str,
        fix_iteration: int,
        passes_required: int,
        implementation_baseline: Optional[ReviewBaseline],
        latest_fix_baseline: Optional[ReviewBaseline],
    ) -> Dict[str, Any]:
        """Return the active round, creating one exactly once when necessary.

        ``latest_fix_baseline`` is the fix baseline an incremental round is
        diffed FROM. Callers pass the EARLIEST fix baseline not yet covered
        by any review round (never just the newest one), so the round's diff
        spans the union of every fix made since the last review — a defect
        introduced by an earlier unreviewed fix keeps its causal anchors
        inside the scope.
        """
        fingerprint = requirement_fingerprint(requirement_text)
        active = self.active_round
        if active and active.get("requirement_fingerprint") == fingerprint:
            return active

        self._attach()
        prior_fingerprint = self.state.get("requirement_fingerprint")
        forced_reason = str(self.state.pop("force_full_reason", "") or "")
        next_mode = str(self.state.pop("next_scope_mode", "") or "")
        # ``completed_full_rounds`` counts *clean* full rounds and gates the
        # final quality-gate routing; ``full_round_occurred`` merely records
        # that a full round has already run — findings and all — so a FIX
        # after the initial full round can switch to incremental review.
        full_round_occurred = bool(self.state.get("full_round_occurred", False))

        if active and active.get("requirement_fingerprint") != fingerprint:
            forced_reason = "effective_requirements_changed"
        elif prior_fingerprint and prior_fingerprint != fingerprint:
            forced_reason = "effective_requirements_changed"

        if forced_reason or next_mode == "full":
            mode = "full"
            baseline = implementation_baseline
            if forced_reason:
                reason = forced_reason
            else:
                reason = "full_closure"
        elif not full_round_occurred:
            # Covers both the very first review and a FIX that TEST/E2E
            # triggered before any full SELF_CHECK round ran: the first round
            # must review the implementation baseline, never a fix baseline.
            mode = "full"
            baseline = implementation_baseline
            reason = "initial_full"
        elif latest_fix_baseline is not None:
            mode = "incremental"
            baseline = latest_fix_baseline
            reason = "post_fix_incremental"
        else:
            # A missing fix baseline can never be interpreted as an empty fix.
            mode = "full"
            baseline = implementation_baseline
            reason = "fix_baseline_unavailable"

        active = {
            "round_id": f"scr-{uuid.uuid4().hex[:12]}",
            "scope_mode": mode,
            # INVARIANT: round accounting uses the mode the round actually
            # STARTED executing with. A later pass that degrades to full
            # (undecidable incremental reconstruction) must not retroactively
            # credit the round as the mandatory clean full round — pass #1 only
            # ever saw the fix delta, so the closure round is still owed.
            "round_scope_mode": mode,
            "baseline_id": baseline.baseline_id if baseline else "",
            "baseline_kind": baseline.kind if baseline else "",
            "round_reason": reason,
            "requirement_fingerprint": fingerprint,
            "fix_iteration": int(fix_iteration or 0),
            "pass_index": 1,
            "passes_required": max(1, int(passes_required or 1)),
            "status": "active",
        }
        self.state["active_round"] = active
        self.state["requirement_fingerprint"] = fingerprint
        return active

    def advance_pass(self) -> None:
        active = self.active_round
        if active:
            active["pass_index"] = int(active.get("pass_index", 1) or 1) + 1

    def force_full(self, reason: str) -> None:
        self._attach()
        active = self.active_round
        if active:
            self.state["last_round"] = {**active, "status": "invalidated"}
            self._note_full_round_occurred(active)
        self.state.pop("active_round", None)
        self.state["force_full_reason"] = reason or "forced_full"
        self.state.pop("next_scope_mode", None)

    def requirements_changed(self, requirement_text: str) -> bool:
        active = self.active_round
        if not active:
            return False
        return active.get("requirement_fingerprint") != requirement_fingerprint(
            requirement_text
        )

    def mark_findings(self) -> None:
        active = self.active_round
        if active:
            self._attach()
            self.state["last_round"] = {**active, "status": "findings"}
            self._note_full_round_occurred(active)
        self.state.pop("active_round", None)
        self.state.pop("next_scope_mode", None)

    def complete_clean(self) -> bool:
        """Finish the active round; return True when a closure round is due."""
        active = self.active_round
        if not active:
            # A persisted ``next_scope_mode`` marks a closure round that was
            # scheduled (clean incremental) but never materialized — the flow
            # was interrupted between the in-memory round completion and the
            # closure step's save. Keep demanding the closure round until one
            # actually starts (``prepare_round`` consumes the marker); without
            # this, a resumed flow advances past SELF_CHECK having reviewed
            # only the incremental fix delta.
            return str(self.state.get("next_scope_mode") or "") == "full"
        self._attach()
        completed = {**active, "status": "clean"}
        self.state["last_round"] = completed
        self.state.pop("active_round", None)
        if self._accounting_mode(active) == "incremental":
            self.state["next_scope_mode"] = "full"
            return True
        self._note_full_round_occurred(active)
        self.state["completed_full_rounds"] = int(
            self.state.get("completed_full_rounds", 0) or 0
        ) + 1
        self.state["last_clean_full_round_id"] = active.get("round_id", "")
        return False

    def _note_full_round_occurred(self, active: Dict[str, Any]) -> None:
        """Persist that a full round has run, clean or not.

        Scope selection keys off the first-ever full round having *run* (a full
        round that surfaced findings must still switch post-FIX rounds to
        incremental); ``completed_full_rounds`` alone counts clean ones and
        cannot carry that signal.
        """
        if self._accounting_mode(active) == "full":
            self._attach()
            self.state["full_round_occurred"] = True

    @staticmethod
    def _accounting_mode(active: Dict[str, Any]) -> str:
        """The mode a round is credited as, independent of mid-round degrades.

        Rounds persisted before ``round_scope_mode`` existed fall back to the
        executing mode, so an old flow's resume keeps its recorded accounting.
        """
        return str(
            active.get("round_scope_mode") or active.get("scope_mode") or "full"
        )


class ReviewScopeManager:
    """Capture review baselines and reconstruct precise code diffs."""

    def __init__(self, project_root: Path, flow_id: str):
        self.project_root = Path(project_root).resolve()
        self.flow_id = str(flow_id)
        self.root = runtime_dir(self.project_root) / "state" / "review-scopes" / self.flow_id

    def load_baseline(self, baseline_id: str) -> Optional[ReviewBaseline]:
        """Load one captured baseline descriptor from the runtime store."""
        try:
            descriptor_path = self._baseline_dir(baseline_id) / "descriptor.json"
        except ValueError:
            return None
        return self._read_descriptor(descriptor_path)

    def store_exists(self) -> bool:
        """Whether this flow's baseline snapshot directory is still on disk."""
        return self.root.is_dir()

    def list_baselines(self) -> List[ReviewBaseline]:
        """Every persisted descriptor of this flow, oldest capture first."""
        if not self.root.is_dir():
            return []
        found: List[ReviewBaseline] = []
        try:
            entries = sorted(self.root.iterdir())
        except OSError:
            return []
        for entry in entries:
            if not entry.is_dir():
                continue
            baseline = self._read_descriptor(entry / "descriptor.json")
            if baseline is not None:
                found.append(baseline)
        found.sort(key=lambda item: (item.captured_at, item.baseline_id))
        return found

    def load_fix_baselines(
        self, scope_context: Optional[Dict[str, Any]]
    ) -> Dict[str, ReviewBaseline]:
        """Load every captured fix baseline named by the flow's scope context."""
        result: Dict[str, ReviewBaseline] = {}
        if not isinstance(scope_context, dict):
            return result
        history = scope_context.get("fix_baseline_history")
        if not isinstance(history, list):
            return result
        for entry in history:
            if not isinstance(entry, dict):
                continue
            baseline_id = str(entry.get("baseline_id") or "")
            if not baseline_id:
                continue
            baseline = self.load_baseline(baseline_id)
            if baseline is not None:
                result[baseline_id] = baseline
        return result

    def earliest_unreviewed_fix_baseline(
        self, scope_context: Optional[Dict[str, Any]]
    ) -> Optional[ReviewBaseline]:
        """The EARLIEST fix baseline not yet covered by a review round.

        Multiple FIXes can run with no SELF_CHECK round between them. A round
        diffed from the earliest uncovered baseline spans the union of every
        such fix's changes, so a defect introduced by an earlier unreviewed
        fix keeps its causal anchors inside the scope — diffing from only the
        LAST fix would drop that fix's delta from the round entirely. When
        the earliest uncovered baseline cannot be loaded the union cannot be
        reconstructed, so this returns None and the round degrades to the
        full fallback instead of silently narrowing the scope.
        """
        if not isinstance(scope_context, dict):
            return None
        history = scope_context.get("fix_baseline_history")
        covered = scope_context.get("covered_fix_baseline")
        latest_dict = scope_context.get("latest_fix_baseline")
        latest_id = (
            latest_dict.get("baseline_id")
            if isinstance(latest_dict, dict)
            else None
        )
        if not isinstance(history, list) or not history:
            # Pre-history persisted flows and synthetic callers that carry
            # only the latest-fix-baseline key: there the latest baseline IS
            # the earliest unreviewed one.
            return ReviewBaseline.from_dict(latest_dict)
        fix_baselines = self.load_fix_baselines(scope_context)
        found_covered = not covered
        for entry in history:
            if not isinstance(entry, dict):
                continue
            baseline_id = str(entry.get("baseline_id") or "")
            if not baseline_id:
                continue
            if not found_covered:
                if baseline_id == covered:
                    found_covered = True
                continue
            # The first entry AFTER the covered baseline is the earliest
            # unreviewed fix.
            if baseline_id in fix_baselines:
                return fix_baselines[baseline_id]
            return None
        # Everything in history is covered: only a newer fix baseline beyond
        # the covered marker is still unreviewed (and its history entry may
        # be missing in pre-history synthetic callers).
        if latest_id and latest_id != covered:
            baseline = fix_baselines.get(latest_id)
            if baseline is not None:
                return baseline
            return ReviewBaseline.from_dict(latest_dict)
        return None

    def lookup_baseline(
        self,
        selector: str,
        flow_context: Optional[Dict[str, Any]] = None,
    ) -> BaselineLookup:
        """Resolve one baseline by role, reporting WHY it is not usable.

        Read-only: nothing here captures, writes or repairs a descriptor, so a
        display surface can call it against a finished flow without reviving
        state the flow no longer owns.

        ``flow_context`` is a flow's persisted ``state.context``. ``None`` means
        the flow record itself is gone, and then the snapshot store is the only
        evidence left — an absent store there is a reclaimed snapshot, not a
        baseline that was never taken.
        """
        selector = (
            BASELINE_SELECTOR_FIX
            if str(selector) == BASELINE_SELECTOR_FIX
            else BASELINE_SELECTOR_IMPLEMENTATION
        )
        has_context = isinstance(flow_context, dict)
        context: Dict[str, Any] = flow_context if has_context else {}
        scope_context = context.get("review_scope")
        if not isinstance(scope_context, dict):
            scope_context = {}

        baseline_id = self._selected_baseline_id(selector, context, scope_context)
        if baseline_id:
            baseline = self.load_baseline(baseline_id)
            if baseline is None:
                # The flow still names this baseline, so it WAS captured; the
                # descriptor is simply no longer on disk.
                return BaselineLookup(
                    status=BASELINE_STATUS_CLEANED,
                    selector=selector,
                    baseline_id=baseline_id,
                )
            return self._lookup_from_descriptor(selector, baseline)

        scanned = self._scan_store_for(selector)
        if scanned is not None:
            return self._lookup_from_descriptor(selector, scanned)
        if not has_context and not self.store_exists():
            return BaselineLookup(
                status=BASELINE_STATUS_CLEANED, selector=selector
            )
        return BaselineLookup(
            status=BASELINE_STATUS_NOT_CAPTURED, selector=selector
        )

    @staticmethod
    def _lookup_from_descriptor(
        selector: str, baseline: ReviewBaseline
    ) -> BaselineLookup:
        if not baseline.available:
            return BaselineLookup(
                status=BASELINE_STATUS_UNAVAILABLE,
                selector=selector,
                baseline=baseline,
                baseline_id=baseline.baseline_id,
                diagnostic="; ".join(baseline.diagnostics),
            )
        return BaselineLookup(
            status=BASELINE_STATUS_OK,
            selector=selector,
            baseline=baseline,
            baseline_id=baseline.baseline_id,
        )

    def _selected_baseline_id(
        self,
        selector: str,
        context: Dict[str, Any],
        scope_context: Dict[str, Any],
    ) -> str:
        if selector == BASELINE_SELECTOR_IMPLEMENTATION:
            declared = scope_context.get("implementation_baseline")
            if isinstance(declared, dict):
                return str(declared.get("baseline_id") or "")
            return ""
        # WHY the active round comes first for the fix role: once a round is
        # created the flow marks every fix baseline up to the latest one as
        # covered, so ``earliest_unreviewed_fix_baseline`` answers None for
        # exactly the window in which a checker is reading the scope. The
        # round's own baseline is what that checker is being asked to review.
        active = context.get("self_check_review")
        active_round = active.get("active_round") if isinstance(active, dict) else None
        if isinstance(active_round, dict):
            round_id = str(active_round.get("baseline_id") or "")
            round_kind = str(active_round.get("baseline_kind") or "")
            if round_id and round_kind != BASELINE_SELECTOR_IMPLEMENTATION:
                return round_id
        earliest = self.earliest_unreviewed_fix_baseline(scope_context)
        if earliest is not None:
            return earliest.baseline_id
        latest = scope_context.get("latest_fix_baseline")
        if isinstance(latest, dict):
            return str(latest.get("baseline_id") or "")
        return ""

    def _scan_store_for(self, selector: str) -> Optional[ReviewBaseline]:
        """Last-resort role resolution from the descriptors alone.

        A flow whose engine record no longer carries the scope context (an old
        persisted flow, an archive pruned of its context) still has its
        descriptors on disk, and they are self-describing.
        """
        matches = [
            baseline for baseline in self.list_baselines()
            if (
                baseline.kind == BASELINE_SELECTOR_IMPLEMENTATION
                if selector == BASELINE_SELECTOR_IMPLEMENTATION
                else baseline.kind.startswith("fix")
            )
        ]
        return matches[-1] if matches else None

    def unavailable_baseline(self, kind: str, diagnostic: str) -> ReviewBaseline:
        baseline = ReviewBaseline(
            baseline_id=self._new_baseline_id(kind),
            kind=kind,
            flow_id=self.flow_id,
            captured_at=datetime.now().isoformat(),
            project_root=str(self.project_root),
            available=False,
            diagnostics=[diagnostic],
        )
        self._write_descriptor(baseline)
        return baseline

    def capture(self, kind: str) -> ReviewBaseline:
        """Capture a read-only logical workspace baseline into runtime state."""
        baseline = ReviewBaseline(
            baseline_id=self._new_baseline_id(kind),
            kind=str(kind),
            flow_id=self.flow_id,
            captured_at=datetime.now().isoformat(),
            project_root=str(self.project_root),
        )
        try:
            identity, identity_error = self._repository_identity()
            if identity_error:
                raise RuntimeError(identity_error)
            baseline.repository_identity = identity

            head = self._git(["rev-parse", "HEAD"])
            if head is None:
                raise RuntimeError("git could not resolve HEAD")
            baseline.head_commit = head.decode("ascii", "replace").strip()
            if not baseline.head_commit:
                raise RuntimeError("git returned an empty HEAD")

            head_entries = self._head_entries(baseline.head_commit)
            tracked_paths = self._tracked_paths()
            dirty_paths = self._dirty_paths(baseline.head_commit)
            untracked_paths = self._untracked_paths()
            if any(value is None for value in (
                head_entries, tracked_paths, dirty_paths, untracked_paths
            )):
                raise RuntimeError("git workspace manifest could not be determined")

            assert head_entries is not None
            assert tracked_paths is not None
            assert dirty_paths is not None
            assert untracked_paths is not None

            for path in sorted(tracked_paths):
                self._validate_relative_path(path)
                head_entry = head_entries.get(path)
                if head_entry is not None and head_entry[0] == "160000":
                    # A submodule must be captured by its OWN checked-out state
                    # (HEAD + worktree fingerprint), never by the superproject
                    # index object id alone: the flow can commit or edit inside
                    # the submodule without staging the gitlink, and a
                    # pre-existing dirty submodule must be excluded accurately
                    # instead of stored as a missing entry.
                    baseline.tracked[path] = self._store_gitlink_entry(
                        baseline.baseline_id, path, head_entry
                    )
                elif path in dirty_paths or head_entry is None:
                    baseline.tracked[path] = self._store_worktree_entry(
                        baseline.baseline_id, path, tracked=True
                    )
                else:
                    mode, object_type, object_id = head_entry
                    baseline.tracked[path] = {
                        "path": path,
                        "tracked": True,
                        "kind": self._kind_from_git(mode, object_type),
                        "mode": mode,
                        "storage": "git",
                        "object_id": object_id,
                    }

            for path in sorted(untracked_paths):
                self._validate_relative_path(path)
                baseline.untracked[path] = self._store_worktree_entry(
                    baseline.baseline_id, path, tracked=False
                )
        except Exception as exc:  # noqa: BLE001 - persisted safe degradation
            baseline.available = False
            baseline.diagnostics.append(str(exc))
            logger.warning("Review baseline capture unavailable: %s", exc)

        self._write_descriptor(baseline)
        return baseline

    def resolve(
        self,
        requested_mode: str,
        baseline: Optional[ReviewBaseline],
        *,
        full_baseline: Optional[ReviewBaseline] = None,
        declared_paths: Optional[Sequence[str]] = None,
    ) -> ReviewScope:
        """Reconstruct a scope, falling incremental failures back to full.

        On a decidable incremental round the implementation baseline is
        reconstructed a SECOND time and attached to the result as the
        ``task_*`` fields. ``full_baseline`` is therefore no longer only the
        undecidable-fallback source: it is also the whole-task evidence domain
        the round grounds findings against (see ``ReviewScope``).
        """
        mode = "incremental" if requested_mode == "incremental" else "full"
        result = self.reconstruct(mode, baseline, declared_paths=declared_paths)
        if mode != "incremental" or not result.undecidable:
            if mode == "incremental":
                self._attach_task_scope(
                    result, full_baseline, declared_paths=declared_paths
                )
            return result

        incremental_diagnostic = result.diagnostic
        full = self.reconstruct(
            "full", full_baseline, declared_paths=declared_paths
        )
        full.requested_mode = "incremental"
        full.fallback_from_incremental = True
        prefix = (
            "Incremental baseline was undecidable; review safely fell back to "
            f"the implementation baseline ({incremental_diagnostic})."
        )
        full.diagnostic = f"{prefix} {full.diagnostic}".strip()
        return full

    def _attach_task_scope(
        self,
        result: ReviewScope,
        full_baseline: Optional[ReviewBaseline],
        *,
        declared_paths: Optional[Sequence[str]] = None,
    ) -> None:
        """Attach the implementation-baseline anchor set to an incremental scope.

        WHY a failure here never degrades the round: the whole-task domain only
        WIDENS what evidence can ground on. When it cannot be rebuilt the round
        still has its own decidable fix-delta domain and behaves exactly as it
        did before this widening existed — turning a usable incremental round
        undecidable over a purely additive input would be a regression, not a
        safety measure.
        """
        if full_baseline is None:
            result.task_scope_diagnostic = (
                "implementation baseline is missing; evidence can only ground "
                "in this fix's delta"
            )
            return
        if full_baseline.baseline_id == result.baseline_id:
            # The round already diffs from the implementation baseline, so its
            # own anchors ARE the whole-task anchors.
            return
        task = self.reconstruct(
            "full", full_baseline, declared_paths=declared_paths
        )
        if task.undecidable:
            result.task_scope_diagnostic = (
                "whole-task diff could not be reconstructed "
                f"({task.diagnostic}); evidence can only ground in this fix's "
                "delta"
            )
            return
        result.task_baseline_id = task.baseline_id
        result.task_changed_paths = list(task.changed_paths)
        result.task_causal_anchors = task.causal_anchors
        result.task_deletion_anchors = task.deletion_anchors
        result.task_artifact_path = task.artifact_path
        result.task_scope_available = True

    def reconstruct(
        self,
        scope_mode: str,
        baseline: Optional[ReviewBaseline],
        *,
        declared_paths: Optional[Sequence[str]] = None,
        write_artifact: bool = True,
    ) -> ReviewScope:
        """Build the actual baseline-to-current unified diff.

        ``write_artifact=False`` keeps the reconstruction purely observational:
        a display surface re-reads a baseline the flow already owns and must not
        add files to that flow's runtime state as a side effect of being looked
        at.

        ``declared_paths`` are the paths the implementation step reported it
        changed. They are only consulted for files git cannot see at all
        (untracked AND ignored), which the baseline capture — which enumerates
        with ``--exclude-standard`` — provably never holds: such a file would
        otherwise be absent from the baseline, the diff and the changed set,
        leaving a real flow change unreviewable and any finding on it dropped
        as ungrounded. They never override a reconstructable path.
        """
        if baseline is None:
            return ReviewScope(
                requested_mode=scope_mode,
                scope_mode=scope_mode,
                undecidable=True,
                diagnostic="review baseline descriptor is missing",
            )
        result = ReviewScope(
            requested_mode=scope_mode,
            scope_mode=scope_mode,
            baseline_id=baseline.baseline_id,
        )
        if not baseline.available:
            result.undecidable = True
            result.diagnostic = "; ".join(baseline.diagnostics) or "baseline unavailable"
            return result

        identity, identity_error = self._repository_identity()
        if identity_error or identity != baseline.repository_identity:
            result.undecidable = True
            result.diagnostic = identity_error or "repository identity changed since baseline"
            return result

        descriptor_path = self._baseline_dir(baseline.baseline_id) / "descriptor.json"
        on_disk = self._read_descriptor(descriptor_path)
        if on_disk is None or on_disk.to_dict() != baseline.to_dict():
            result.undecidable = True
            result.diagnostic = "persisted baseline descriptor is missing or corrupt"
            return result

        current_head = self._git(["rev-parse", "HEAD"])
        if current_head is None:
            result.undecidable = True
            result.diagnostic = "git could not resolve the current HEAD"
            return result
        current_head = current_head.decode("ascii", "replace").strip()
        if current_head != baseline.head_commit:
            # WHY a descendant HEAD is still decidable: the flow itself commits
            # during IMPLEMENT (each DAG leaf branch is merged back onto the
            # working branch), so HEAD routinely advances past the baseline
            # commit. The baseline manifest is content-keyed — git object ids
            # for clean tracked paths, stored blobs for dirty/untracked ones —
            # and resolves regardless of where HEAD points, so the
            # baseline-to-worktree diff is still exactly reconstructable and
            # still contains precisely the work done since the baseline.
            # Only history that no longer contains the baseline commit (rebase,
            # amend, reset backwards, an unrelated checkout) makes the change
            # set unattributable, and that alone degrades to the safe fallback.
            ancestry = self._is_ancestor(baseline.head_commit, current_head)
            if ancestry is not True:
                result.undecidable = True
                result.diagnostic = (
                    "HEAD no longer descends from the baseline commit "
                    "(history rewrite or unrelated checkout?); scope cannot be "
                    "attributed to this flow's changes"
                    if ancestry is False
                    else "git could not relate the current HEAD to the "
                         "baseline commit"
                )
                return result
            result.diagnostic = (
                "HEAD advanced from the baseline commit "
                f"{baseline.head_commit[:12]} to {current_head[:12]}; the diff "
                "below spans every change since the baseline, committed or not."
            )

        try:
            current_tracked = self._tracked_paths()
            current_untracked = self._untracked_paths()
            diff_names_raw = self._git(
                ["diff", "--name-only", "-z", baseline.head_commit, "--"]
            )
            if current_tracked is None or current_untracked is None or diff_names_raw is None:
                raise RuntimeError("current git workspace state could not be determined")
            diff_names = self._decode_z_paths(diff_names_raw)
            candidates = set(diff_names)
            candidates.update(
                path for path, entry in baseline.tracked.items()
                if entry.get("storage") != "git" or entry.get("kind") == "gitlink"
            )
            # WHY gitlinks are always candidates: a superproject ``git diff``
            # does not descend into a submodule, so a flow-introduced untracked
            # or unstaged inner file moves no superproject line while the
            # gitlink object id stays put. Only comparing the submodule's own
            # state (HEAD + worktree fingerprint) can see such a change; an
            # omitted gitlink would escape review with an empty scope
            # masquerading as "no changes".
            candidates.update(baseline.untracked)
            candidates.update(current_untracked)
            candidates.update(set(baseline.tracked) ^ set(current_tracked))
            candidates = {
                path for path in candidates
                if not self._is_runtime_untracked(path)
            }

            current_index = self._index_entries()
            if current_index is None:
                raise RuntimeError("current git index could not be determined")

            changes: Dict[str, Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]] = {}
            for path in sorted(candidates):
                self._validate_relative_path(path)
                before_desc = baseline.tracked.get(path) or baseline.untracked.get(path)
                before = self._load_baseline_entry(baseline, before_desc)
                after = self._read_current_entry(
                    path,
                    tracked=path in current_tracked,
                    index_entry=current_index.get(path),
                    baseline_desc=before_desc,
                )
                if self._entry_identity(before) != self._entry_identity(after):
                    changes[path] = (before, after)

            diff_text, anchors, deletions, inner_paths = self._render_diff(changes)
            ignored = self._ignored_declared_paths(
                declared_paths,
                baseline=baseline,
                current_tracked=current_tracked,
                current_untracked=current_untracked,
                known=set(changes) | set(candidates),
            )
            if ignored:
                diff_text += self._render_ignored_notes(ignored)
            # Inner submodule paths the diff actually rendered join the changed
            # set: each is an independently citable changed path (classified by
            # its OWN anchors), and without membership an anchor-less one —
            # deletion-only, or content the manifest proves changed but cannot
            # reconstruct — would have nothing to ground on.
            result.changed_paths = sorted(
                set(changes) | set(inner_paths) | set(ignored)
            )
            result.causal_anchors = anchors
            result.deletion_anchors = deletions
            result.unified_diff = diff_text
            if write_artifact:
                result.artifact_path = self._write_diff_artifact(
                    baseline.baseline_id, diff_text
                )
            return result
        except Exception as exc:  # noqa: BLE001 - never fake an empty diff
            result.undecidable = True
            result.diagnostic = str(exc)
            result.changed_paths = []
            result.causal_anchors = {}
            result.deletion_anchors = {}
            result.unified_diff = ""
            logger.warning("Review diff reconstruction unavailable: %s", exc)
            return result

    def _ignored_declared_paths(
        self,
        declared_paths: Optional[Sequence[str]],
        *,
        baseline: ReviewBaseline,
        current_tracked: set[str],
        current_untracked: set[str],
        known: set[str],
    ) -> List[str]:
        """Flow-reported paths git ignores, which capture provably cannot hold.

        A path git enumerates (tracked now or at capture, or untracked and not
        ignored) is fully reconstructable and is left to the manifest — it is
        NOT re-added here, so a declared path the diff proves unchanged stays
        out of the change set. Only a path that exists on disk yet appears in
        no git listing is invisible to ``--exclude-standard`` enumeration; that
        one is admitted as an anchor-less changed path so a finding on it can
        ground.
        """
        if not declared_paths:
            return []
        out: List[str] = []
        for raw in declared_paths:
            if not isinstance(raw, str) or not raw.strip():
                continue
            path = raw.strip().replace("\\", "/")
            # Trim a leading "./" only — a character-class strip would eat the
            # leading dot of a legitimate dotfile such as ``.env.local``.
            while path.startswith("./"):
                path = path[2:]
            if not path or path in known:
                continue
            try:
                self._validate_relative_path(path)
            except ValueError:
                continue
            if (
                path in current_tracked
                or path in current_untracked
                or path in baseline.tracked
                or path in baseline.untracked
                or self._is_runtime_untracked(path)
            ):
                continue
            absolute = self.project_root / path
            try:
                if not absolute.is_file():
                    continue
            except OSError:
                continue
            out.append(path)
        return sorted(set(out))

    @staticmethod
    def _render_ignored_notes(paths: Sequence[str]) -> str:
        """Explicit notes for git-ignored declared paths (never a fake diff)."""
        lines: List[str] = []
        for path in paths:
            lines.append(f"diff --git a/{path} b/{path}\n")
            lines.append(
                f"@@ {path} @@ (git-ignored path reported changed by the "
                "implementation; ignored files are outside baseline capture, "
                "so no line diff can be reconstructed — read the file "
                "directly to review it)\n"
            )
        return "".join(lines)

    def _new_baseline_id(self, kind: str) -> str:
        prefix = re.sub(r"[^a-z0-9]+", "-", str(kind).lower()).strip("-")
        prefix = prefix or "review"
        return f"{prefix[:40]}-{uuid.uuid4().hex[:12]}"

    def _baseline_dir(self, baseline_id: str) -> Path:
        if not _SAFE_BASELINE_ID_RE.match(str(baseline_id)):
            raise ValueError("unsafe review baseline id")
        return self.root / str(baseline_id)

    def _git(self, args: List[str]) -> Optional[bytes]:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except (FileNotFoundError, OSError):
            return None
        if proc.returncode != 0:
            logger.debug(
                "git %s failed: %s", " ".join(args),
                proc.stderr.decode("utf-8", "replace").strip(),
            )
            return None
        return proc.stdout or b""

    def _is_ancestor(self, ancestor: str, descendant: str) -> Optional[bool]:
        """Whether ``ancestor`` is reachable from ``descendant``.

        ``None`` when git cannot answer at all (missing object, unreadable
        repository), so the caller degrades to full instead of guessing that a
        commit it cannot see is part of the current history.
        """
        if not ancestor or not descendant:
            return None
        try:
            proc = subprocess.run(
                ["git", "merge-base", "--is-ancestor", ancestor, descendant],
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except (FileNotFoundError, OSError):
            return None
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        logger.debug(
            "git merge-base --is-ancestor failed: %s",
            proc.stderr.decode("utf-8", "replace").strip(),
        )
        return None

    def _repository_identity(self) -> Tuple[str, str]:
        top = self._git(["rev-parse", "--show-toplevel"])
        common = self._git(["rev-parse", "--git-common-dir"])
        if top is None or common is None:
            return "", "git repository identity is unavailable"
        top_path = Path(top.decode("utf-8", "surrogateescape").strip()).resolve()
        common_text = common.decode("utf-8", "surrogateescape").strip()
        common_path = Path(common_text)
        if not common_path.is_absolute():
            common_path = (self.project_root / common_path).resolve()
        if top_path != self.project_root:
            return "", "project root no longer matches the git worktree root"
        payload = f"{top_path}\0{common_path}".encode("utf-8", "surrogateescape")
        return hashlib.sha256(payload).hexdigest(), ""

    def _head_entries(
        self, head_commit: str
    ) -> Optional[Dict[str, Tuple[str, str, str]]]:
        raw = self._git(["ls-tree", "-rz", "--full-tree", head_commit])
        if raw is None:
            return None
        entries: Dict[str, Tuple[str, str, str]] = {}
        for item in raw.split(b"\0"):
            if not item:
                continue
            try:
                header, raw_path = item.split(b"\t", 1)
                mode, object_type, object_id = header.decode("ascii").split(" ", 2)
                path = raw_path.decode("utf-8", "surrogateescape")
            except (ValueError, UnicodeDecodeError):
                return None
            entries[path] = (mode, object_type, object_id)
        return entries

    def _tracked_paths(self) -> Optional[set[str]]:
        raw = self._git(["ls-files", "-z"])
        return None if raw is None else set(self._decode_z_paths(raw))

    def _dirty_paths(self, head_commit: str) -> Optional[set[str]]:
        raw = self._git(["diff", "--name-only", "-z", head_commit, "--"])
        return None if raw is None else set(self._decode_z_paths(raw))

    def _untracked_paths(self) -> Optional[set[str]]:
        raw = self._git(["ls-files", "--others", "--exclude-standard", "-z"])
        if raw is None:
            return None
        return {
            path for path in self._decode_z_paths(raw)
            if not self._is_runtime_untracked(path)
        }

    def _index_entries(self) -> Optional[Dict[str, Tuple[str, str]]]:
        raw = self._git(["ls-files", "-s", "-z"])
        if raw is None:
            return None
        entries: Dict[str, Tuple[str, str]] = {}
        for item in raw.split(b"\0"):
            if not item:
                continue
            try:
                header, raw_path = item.split(b"\t", 1)
                mode, object_id, stage = header.decode("ascii").split(" ", 2)
                path = raw_path.decode("utf-8", "surrogateescape")
            except (ValueError, UnicodeDecodeError):
                return None
            if stage == "0":
                entries[path] = (mode, object_id)
        return entries

    @staticmethod
    def _decode_z_paths(raw: bytes) -> List[str]:
        return [
            item.decode("utf-8", "surrogateescape")
            for item in raw.split(b"\0") if item
        ]

    def _is_runtime_untracked(self, path: str) -> bool:
        runtime = runtime_dir_name(self.project_root).rstrip("/")
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        return len(parts) >= 2 and parts[0] == runtime and parts[1] in _RUNTIME_STATE_SUBTREES

    @staticmethod
    def _validate_relative_path(path: str) -> None:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts or not path:
            raise ValueError(f"unsafe repository path in review baseline: {path!r}")

    @staticmethod
    def _kind_from_git(mode: str, object_type: str) -> str:
        if mode == "120000":
            return "symlink"
        if mode == "160000" or object_type == "commit":
            return "gitlink"
        return "file"

    def _store_worktree_entry(
        self, baseline_id: str, path: str, *, tracked: bool
    ) -> Dict[str, Any]:
        entry = self._read_path(path, tracked=tracked)
        if entry is None:
            return {
                "path": path,
                "tracked": tracked,
                "kind": "missing",
                "mode": "000000",
                "storage": "missing",
            }
        content = entry.pop("content")
        digest = hashlib.sha256(content).hexdigest()
        blob_dir = self._baseline_dir(baseline_id) / "blobs"
        blob_dir.mkdir(parents=True, exist_ok=True)
        blob_path = blob_dir / digest
        if not blob_path.exists():
            temporary = blob_dir / f".{digest}.{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(content)
            os.replace(str(temporary), str(blob_path))
        entry.update({
            "path": path,
            "tracked": tracked,
            "storage": "blob",
            "blob_sha256": digest,
            "size": len(content),
        })
        return entry

    @staticmethod
    def _manifest_bytes(manifest: Dict[str, Any]) -> bytes:
        """Deterministic serialization used as the gitlink identity content."""
        return json.dumps(manifest, sort_keys=True).encode("utf-8")

    def _sub_git(self, path: str, args: List[str]) -> Optional[bytes]:
        """Run git inside a checked-out submodule (``None`` on failure)."""
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.project_root / path), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except (FileNotFoundError, OSError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout or b""

    @staticmethod
    def _safe_inner_path(name: str) -> bool:
        candidate = Path(name)
        return bool(name) and not candidate.is_absolute() and ".." not in candidate.parts

    def _gitlink_manifest(self, path: str) -> Dict[str, Any]:
        """Cheap state fingerprint of one checked-out submodule.

        ``head`` is the submodule's HEAD (``""`` when unborn); ``files`` maps
        each tracked inner path to its index ``[mode, sha]``; ``untracked``
        maps each non-ignored untracked inner path to its worktree content
        sha; ``status`` carries the raw ``git status --porcelain=v1 -z`` text
        — the worktree fingerprint that flips on ANY post-baseline edit inside
        the submodule (including edits to files whose index sha is unchanged).
        A missing checkout yields an empty manifest; a checkout whose git
        cannot be read raises, so the baseline degrades to unavailable (→ full
        review) instead of silently masking submodule changes as unchanged.
        """
        absolute = self.project_root / path
        if not absolute.is_dir():
            return {"head": "", "files": {}, "untracked": {}, "status": ""}
        head_raw = self._sub_git(path, ["rev-parse", "HEAD"])
        files_raw = self._sub_git(path, ["ls-files", "-s", "-z"])
        status_raw = self._sub_git(
            path, ["status", "--porcelain=v1", "--untracked-files=all", "-z"]
        )
        untracked_raw = self._sub_git(
            path, ["ls-files", "--others", "--exclude-standard", "-z"]
        )
        if (
            head_raw is None
            or files_raw is None
            or status_raw is None
            or untracked_raw is None
        ):
            raise RuntimeError(f"submodule state unreadable: {path}")
        files: Dict[str, List[str]] = {}
        for item in files_raw.split(b"\0"):
            if not item:
                continue
            try:
                header, raw_name = item.split(b"\t", 1)
                mode, sha, stage = header.decode("ascii").split(" ", 2)
            except ValueError:
                continue
            if stage != "0":
                continue
            name = raw_name.decode("utf-8", "surrogateescape")
            if self._safe_inner_path(name):
                files[name] = [mode, sha]
        untracked: Dict[str, str] = {}
        for raw_name in untracked_raw.split(b"\0"):
            if not raw_name:
                continue
            name = raw_name.decode("utf-8", "surrogateescape")
            if not self._safe_inner_path(name):
                continue
            content = self._read_inner_file(absolute, name, "100644")
            if content is not None:
                untracked[name] = hashlib.sha256(content).hexdigest()
        status_text = status_raw.decode("utf-8", "surrogateescape")
        # The status text names dirty files but carries no content, so an edit
        # to an already-dirty file would keep the manifest byte-identical and
        # escape the scope. Hash each dirty file's worktree content to make
        # the manifest content-sensitive (dirty files are the few, so the
        # read cost stays bounded).
        dirty_hashes: Dict[str, str] = {}
        for name in self._worktree_modified_paths(status_text):
            if not self._safe_inner_path(name):
                continue
            content = self._read_inner_file(absolute, name, "100644")
            if content is not None:
                dirty_hashes[name] = hashlib.sha256(content).hexdigest()
        return {
            "head": head_raw.decode("ascii", "replace").strip(),
            "files": files,
            "untracked": untracked,
            "status": status_text,
            "dirty_hashes": dirty_hashes,
        }

    def _read_inner_file(
        self, absolute: Path, name: str, mode: str
    ) -> Optional[bytes]:
        """Read one inner path of a submodule checkout for rendering."""
        target = absolute / name
        try:
            info = target.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return os.fsencode(os.readlink(str(target)))
        if stat.S_ISDIR(info.st_mode):
            # A nested gitlink: its identity is its checked-out HEAD.
            head = self._sub_git(str(target.relative_to(self.project_root)), ["rev-parse", "HEAD"])
            if head is None:
                return None
            return (
                b"Subproject commit "
                + head.decode("ascii", "replace").strip().encode("ascii", "replace")
                + b"\n"
            )
        if stat.S_ISREG(info.st_mode):
            try:
                return target.read_bytes()
            except OSError:
                return None
        return None

    def _gitlink_worktree_contents(
        self, path: str, manifest: Dict[str, Any]
    ) -> Dict[str, str]:
        """Base64 content map for every file of a dirty submodule checkout.

        Stored in the baseline blob so a pre-existing dirty submodule can be
        excluded accurately AND its files can still be rendered against
        post-baseline edits even when the submodule's git history later moves.
        """
        absolute = self.project_root / path
        contents: Dict[str, str] = {}
        for inner_path, (mode, _sha) in (manifest.get("files") or {}).items():
            if not self._safe_inner_path(inner_path):
                continue
            content = self._read_inner_file(absolute, inner_path, mode)
            if content is not None:
                contents[inner_path] = base64.b64encode(content).decode("ascii")
        for inner_path in manifest.get("untracked") or {}:
            if not self._safe_inner_path(inner_path):
                continue
            content = self._read_inner_file(absolute, inner_path, "100644")
            if content is not None:
                contents[inner_path] = base64.b64encode(content).decode("ascii")
        return contents

    def _store_gitlink_entry(
        self, baseline_id: str, path: str, head_entry: Tuple[str, str, str]
    ) -> Dict[str, Any]:
        """Capture a gitlink's submodule state for the baseline descriptor."""
        _mode, _object_type, object_id = head_entry
        manifest = self._gitlink_manifest(path)
        dirty = (
            manifest["head"] != object_id
            or bool(manifest["status"])
            or bool(manifest["untracked"])
        )
        entry: Dict[str, Any] = {
            "path": path,
            "tracked": True,
            "kind": "gitlink",
            "mode": "160000",
            "storage": "git",
            "object_id": object_id,
            "submodule_manifest": manifest,
        }
        if dirty:
            # A dirty checkout's worktree contents are not reproducible from
            # any git object, so they must be stored for exact reconstruction.
            content = json.dumps(
                self._gitlink_worktree_contents(path, manifest),
                sort_keys=True,
            ).encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
            blob_dir = self._baseline_dir(baseline_id) / "blobs"
            blob_dir.mkdir(parents=True, exist_ok=True)
            blob_path = blob_dir / digest
            if not blob_path.exists():
                temporary = blob_dir / f".{digest}.{uuid.uuid4().hex}.tmp"
                temporary.write_bytes(content)
                os.replace(str(temporary), str(blob_path))
            entry.update({
                "storage": "blob",
                "blob_sha256": digest,
                "size": len(content),
            })
        return entry

    def _read_path(self, path: str, *, tracked: bool) -> Optional[Dict[str, Any]]:
        absolute = self.project_root / path
        try:
            info = absolute.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            content = os.fsencode(os.readlink(str(absolute)))
            return {
                "tracked": tracked,
                "kind": "symlink",
                "mode": "120000",
                "content": content,
            }
        if stat.S_ISREG(info.st_mode):
            return {
                "tracked": tracked,
                "kind": "file",
                "mode": "100755" if info.st_mode & stat.S_IXUSR else "100644",
                "content": absolute.read_bytes(),
            }
        if stat.S_ISDIR(info.st_mode):
            return None
        raise RuntimeError(f"unsupported filesystem entry in review scope: {path}")

    def _load_baseline_entry(
        self,
        baseline: ReviewBaseline,
        descriptor: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not descriptor or descriptor.get("storage") == "missing":
            return None
        storage = descriptor.get("storage")
        kind = str(descriptor.get("kind", "file"))
        blob_map: Optional[Dict[str, str]] = None
        if storage == "blob":
            digest = str(descriptor.get("blob_sha256", ""))
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise RuntimeError("baseline blob reference is malformed")
            blob_path = self._baseline_dir(baseline.baseline_id) / "blobs" / digest
            try:
                content = blob_path.read_bytes()
            except OSError as exc:
                raise RuntimeError(f"baseline blob {digest} is unreadable") from exc
            if hashlib.sha256(content).hexdigest() != digest:
                raise RuntimeError(f"baseline blob {digest} failed integrity validation")
            if kind == "gitlink":
                # The blob holds the dirty checkout's base64 content map; the
                # manifest still lives in the descriptor (the blob is only
                # written when the submodule was dirty at capture).
                try:
                    blob_map = json.loads(content.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise RuntimeError(
                        "submodule baseline contents are corrupt"
                    ) from exc
                if not isinstance(blob_map, dict):
                    raise RuntimeError("submodule baseline contents are malformed")
                manifest = descriptor.get("submodule_manifest")
                if not isinstance(manifest, dict):
                    raise RuntimeError("submodule baseline manifest is missing")
                content = self._manifest_bytes(manifest)
        elif storage == "git":
            object_id = str(descriptor.get("object_id", ""))
            if kind == "gitlink":
                manifest = descriptor.get("submodule_manifest")
                if isinstance(manifest, dict):
                    # New-format gitlink entry: identity is the submodule's
                    # own manifest, not the superproject index object id.
                    content = self._manifest_bytes(manifest)
                else:
                    # Legacy gitlink entry (manifest predates submodule
                    # capture): keep the recorded commit id as identity.
                    content = object_id.encode("ascii", "replace")
            else:
                content = self._git(["cat-file", "blob", object_id])
                if content is None:
                    raise RuntimeError(
                        f"baseline git blob {object_id or '<missing>'} is unavailable"
                    )
        else:
            raise RuntimeError(f"unknown baseline storage type: {storage!r}")
        entry: Dict[str, Any] = {
            "tracked": bool(descriptor.get("tracked", False)),
            "kind": kind,
            "mode": str(descriptor.get("mode", "100644")),
            "content": content,
        }
        if kind == "gitlink":
            manifest = descriptor.get("submodule_manifest")
            entry["manifest"] = manifest if isinstance(manifest, dict) else None
            entry["blob_map"] = blob_map
        return entry

    def _read_current_entry(
        self,
        path: str,
        *,
        tracked: bool,
        index_entry: Optional[Tuple[str, str]],
        baseline_desc: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if index_entry and index_entry[0] == "160000":
            if (
                isinstance(baseline_desc, dict)
                and "submodule_manifest" not in baseline_desc
            ):
                # Legacy gitlink entry (captured before the submodule manifest
                # existed): keep the recorded-commit identity so the baseline
                # still compares equal when only the live HEAD matches — the
                # full worktree fingerprint has no baseline side to diff
                # against and would mark every legacy submodule changed on
                # every round.
                head = self._sub_git(path, ["rev-parse", "HEAD"])
                return {
                    "tracked": tracked,
                    "kind": "gitlink",
                    "mode": "160000",
                    "content": (head or b"").strip(),
                    "manifest": None,
                    "blob_map": None,
                }
            # The current identity of a gitlink is its checked-out submodule
            # state — HEAD plus a worktree fingerprint — not the superproject
            # index object id, which does not change when the flow commits or
            # edits inside the submodule without staging the gitlink.
            manifest = self._gitlink_manifest(path)
            return {
                "tracked": tracked,
                "kind": "gitlink",
                "mode": "160000",
                "content": self._manifest_bytes(manifest),
                "manifest": manifest,
                "blob_map": None,
            }
        return self._read_path(path, tracked=tracked)

    @staticmethod
    def _entry_identity(entry: Optional[Dict[str, Any]]) -> Any:
        if entry is None:
            return None
        # WHY: the baseline exists to exclude work that predates the flow, so
        # a pre-existing untracked file the flow merely ``git add``s — same
        # content, kind and mode — must NOT enter the change set with a
        # zero-hunk diff. The tracked flag is excluded from the identity:
        # tracked-status flips alone are not flow content changes.
        return (
            str(entry.get("kind", "")),
            str(entry.get("mode", "")),
            hashlib.sha256(entry.get("content", b"")).hexdigest(),
        )

    def _render_diff(
        self,
        changes: Dict[str, Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]],
    ) -> Tuple[
        str,
        Dict[str, List[List[int]]],
        Dict[str, List[List[int]]],
        List[str],
    ]:
        pieces: List[str] = []
        anchors: Dict[str, List[List[int]]] = {}
        deletions: Dict[str, List[List[int]]] = {}
        # Paths the rendered diff names that are NOT top-level change keys
        # (submodule inner files). They must be citable, or a finding on a
        # deletion-only / content-unavailable inner file has neither an anchor
        # nor path membership and is discarded as bad evidence.
        rendered_paths: List[str] = []

        # Preserve exact renames as such instead of presenting unrelated delete
        # and add records. Modified renames remain a delete/add pair; the code
        # evidence is still exact even when similarity inference is ambiguous.
        deleted = {
            path: pair[0] for path, pair in changes.items()
            if pair[0] is not None and pair[1] is None
        }
        added = {
            path: pair[1] for path, pair in changes.items()
            if pair[0] is None and pair[1] is not None
        }
        paired: set[str] = set()
        for old_path, old_entry in deleted.items():
            matches = [
                new_path for new_path, new_entry in added.items()
                if new_path not in paired
                and self._entry_identity(old_entry) == self._entry_identity(new_entry)
            ]
            if len(matches) != 1:
                continue
            new_path = matches[0]
            paired.add(old_path)
            paired.add(new_path)
            pieces.append(
                f"diff --git a/{old_path} b/{new_path}\n"
                "similarity index 100%\n"
                f"rename from {old_path}\nrename to {new_path}\n"
            )

        for path, (before, after) in sorted(changes.items()):
            if path in paired:
                continue
            (
                rendered,
                rendered_anchors,
                rendered_deletions,
                inner_paths,
            ) = self._render_file_diff(path, before, after)
            pieces.append(rendered)
            # Submodule diffs render inner files under their inner paths
            # (``vendor/inner.py``): the anchors must carry the same keys the
            # rendered hunks show, or an evidence citation naming the real
            # changed file finds no anchor while the submodule path is
            # coincidentally anchored against inner-file line numbers.
            anchors.update(rendered_anchors)
            deletions.update(rendered_deletions)
            rendered_paths.extend(inner_paths)
        return "".join(pieces), anchors, deletions, rendered_paths

    def _render_file_diff(
        self,
        path: str,
        before: Optional[Dict[str, Any]],
        after: Optional[Dict[str, Any]],
    ) -> Tuple[
        str,
        Dict[str, List[List[int]]],
        Dict[str, List[List[int]]],
        List[str],
    ]:
        old_kind = before.get("kind") if before else "file"
        new_kind = after.get("kind") if after else "file"
        if old_kind == "gitlink" or new_kind == "gitlink":
            return self._render_gitlink_diff(path, before, after)
        rendered, line_ranges, deleted_ranges = self._render_plain_file_diff(
            path, before, after
        )
        anchors = {path: line_ranges} if line_ranges else {}
        deletions = {path: deleted_ranges} if deleted_ranges else {}
        return rendered, anchors, deletions, []

    def _render_plain_file_diff(
        self,
        path: str,
        before: Optional[Dict[str, Any]],
        after: Optional[Dict[str, Any]],
    ) -> Tuple[str, List[List[int]], List[List[int]]]:
        old_label = f"a/{path}" if before is not None else "/dev/null"
        new_label = f"b/{path}" if after is not None else "/dev/null"
        lines = [f"diff --git a/{path} b/{path}\n"]
        if before is None and after is not None:
            lines.append(f"new file mode {after.get('mode', '100644')}\n")
        elif before is not None and after is None:
            lines.append(f"deleted file mode {before.get('mode', '100644')}\n")
        elif before and after and before.get("mode") != after.get("mode"):
            lines.extend([
                f"old mode {before.get('mode')}\n",
                f"new mode {after.get('mode')}\n",
            ])

        old_content = (before.get("content") or b"") if before else b""
        new_content = (after.get("content") or b"") if after else b""

        if b"\0" in old_content or b"\0" in new_content:
            lines.append(f"Binary files {old_label} and {new_label} differ\n")
            return "".join(lines), [], []
        try:
            old_text = old_content.decode("utf-8")
            new_text = new_content.decode("utf-8")
        except UnicodeDecodeError:
            lines.append(f"Binary files {old_label} and {new_label} differ\n")
            return "".join(lines), [], []

        body = list(difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=old_label,
            tofile=new_label,
            lineterm="\n",
        ))
        # difflib passes content lines through verbatim, so an old final line
        # without a trailing newline arrives unterminated and joining would
        # glue it to the next diff line — mangling the rendered diff and
        # shifting every later new-side causal anchor. Terminate each line
        # before the join so _causal_ranges sees the true line layout.
        lines.extend(line if line.endswith("\n") else line + "\n" for line in body)
        rendered = "".join(lines)
        new_ranges, old_ranges = self._causal_ranges(rendered)
        return rendered, new_ranges, old_ranges

    def _gitlink_file_before(
        self,
        before: Optional[Dict[str, Any]],
        path: str,
        inner: str,
        spec: Optional[List[str]],
    ) -> Optional[bytes]:
        """Resolve one inner file's baseline content for a gitlink diff.

        Dirty-at-capture content comes from the stored blob map (which also
        covers untracked-before files, that have no index spec); clean-at-
        capture content is read back from the submodule's own git objects.
        """
        blob_map = before.get("blob_map") if before else None
        if isinstance(blob_map, dict) and inner in blob_map:
            try:
                return base64.b64decode(blob_map[inner])
            except (ValueError, TypeError):
                return None
        if spec is None:
            return None
        sha = spec[1]
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha or ""):
            return None
        content = self._sub_git(path, ["cat-file", "blob", sha])
        return content

    def _gitlink_file_after(
        self,
        path: str,
        inner: str,
        spec: Optional[List[str]],
    ) -> Optional[bytes]:
        """Resolve one inner file's current content for a gitlink diff."""
        mode = spec[0] if spec else "100644"
        return self._read_inner_file(self.project_root / path, inner, mode)

    #: Worktree-column (``Y``) letters that mark an unstaged difference between
    #: the checkout and the submodule index. ``T`` (typechange — a symlink
    #: replaced by a regular file and vice versa) and ``R``/``C`` leave the
    #: index sha AND mode untouched, so the index map cannot see them at all;
    #: omitting them would render a header-only submodule diff with no hunk and
    #: no anchor for a real content change. ``U`` (unmerged) likewise differs
    #: from the recorded index entry.
    _WORKTREE_DIRTY_STATUS = "MDTRCU"

    @classmethod
    def _worktree_modified_paths(cls, status_text: str) -> set[str]:
        """Inner paths whose worktree differs from the submodule index.

        Parses ``git status --porcelain=v1 -z``: each record is ``XY <path>``;
        rename/copy records append a second NUL-terminated destination that
        carries no ``XY `` prefix and is skipped.
        """
        paths: set[str] = set()
        for record in status_text.split("\0"):
            if len(record) < 4 or record[2] != " ":
                continue
            x, y, name = record[0], record[1], record[3:]
            if not name or x == "?":
                continue
            if y in cls._WORKTREE_DIRTY_STATUS:
                paths.add(name)
        return paths

    def _render_gitlink_diff(
        self,
        path: str,
        before: Optional[Dict[str, Any]],
        after: Optional[Dict[str, Any]],
    ) -> Tuple[
        str,
        Dict[str, List[List[int]]],
        Dict[str, List[List[int]]],
        List[str],
    ]:
        """Render a submodule change: HEAD move plus per-file inner diffs.

        The baseline side is the captured manifest (plus the stored dirty
        contents when the submodule was dirty at capture); the current side is
        the live manifest. Files whose content cannot be resolved on either
        side degrade to an explicit note instead of a fabricated empty diff.

        Anchor dicts are keyed by the INNER paths the hunks are labeled with
        (``{submodule}/{inner}``) — never by the submodule path itself, whose
        gitlink entry has no file line space of its own.

        The fourth return value lists EVERY inner label the diff rendered,
        anchor-bearing or not. Those labels join the scope's changed paths, so
        an inner file that is deletion-only or whose content could not be
        resolved still grounds a citation at path level instead of being
        dropped as ungrounded evidence.
        """
        lines = [f"diff --git a/{path} b/{path}\n"]
        if before is None or after is None:
            lines.append(
                f"{'new' if before is None else 'deleted'} submodule {path}\n"
            )
            return "".join(lines), {}, {}, []

        before_manifest = before.get("manifest")
        after_manifest = after.get("manifest")
        before_head = (before_manifest or {}).get("head", "")
        after_head = (after_manifest or {}).get("head", "")
        if not isinstance(before_manifest, dict):
            # Legacy entry: identity was the recorded commit id itself.
            before_head = before_head or (
                before.get("content") or b""
            ).decode("ascii", "replace").strip()
            before_manifest = {"head": before_head, "files": {}, "untracked": {}}
        if not isinstance(after_manifest, dict):
            after_manifest = {"head": after_head, "files": {}, "untracked": {}}
        if before_head != after_head:
            lines.append(
                f"-Subproject commit {before_head or '(not checked out)'}\n"
            )
            lines.append(
                f"+Subproject commit {after_head or '(not checked out)'}\n"
            )

        before_files: Dict[str, List[str]] = {
            str(k): list(v)
            for k, v in ((before_manifest.get("files") or {}).items())
        }
        after_files: Dict[str, List[str]] = {
            str(k): list(v)
            for k, v in ((after_manifest.get("files") or {}).items())
        }
        # The untracked maps carry content sha256s (not just names), so an
        # edit to a pre-existing untracked inner file — same name, same
        # membership — is still visible as a content change and rendered
        # with real hunks instead of a header-only submodule diff.
        before_untracked_map = {
            str(k): str(v)
            for k, v in ((before_manifest.get("untracked") or {}).items())
        }
        after_untracked_map = {
            str(k): str(v)
            for k, v in ((after_manifest.get("untracked") or {}).items())
        }
        inner_anchors: Dict[str, List[List[int]]] = {}
        inner_deletions: Dict[str, List[List[int]]] = {}
        inner_rendered: List[str] = []
        changed_by_index = {
            inner
            for inner in (
                set(before_files) | set(after_files)
                | set(before_untracked_map) | set(after_untracked_map)
            )
            if (
                before_files.get(inner) != after_files.get(inner)
                or before_untracked_map.get(inner)
                != after_untracked_map.get(inner)
            )
        }
        # Unstaged worktree edits keep the index shas unchanged, so the files
        # map misses them; the status text is the only signal they happened.
        worktree_modified = self._worktree_modified_paths(
            str((before_manifest.get("status") or ""))
        ) | self._worktree_modified_paths(
            str((after_manifest.get("status") or ""))
        )
        for inner in sorted(changed_by_index | worktree_modified):
            if not self._safe_inner_path(inner):
                continue
            old = before_files.get(inner)
            new = after_files.get(inner)
            old_untracked = inner in before_untracked_map
            new_untracked = inner in after_untracked_map
            if (
                old == new
                and old_untracked == new_untracked
                and before_untracked_map.get(inner)
                == after_untracked_map.get(inner)
                and inner not in worktree_modified
            ):
                continue
            if old is None and new is None and not (old_untracked or new_untracked):
                # Worktree-modified with no index spec on either side: nothing
                # to anchor the baseline content to — skip rather than fake it.
                continue
            old_content = (
                self._gitlink_file_before(before, path, inner, old)
                if old is not None or old_untracked
                else None
            )
            new_content = (
                self._gitlink_file_after(path, inner, new)
                if new is not None or new_untracked
                else None
            )
            label = f"{path}/{inner}"
            if old_content is None and (old is not None or old_untracked):
                # The baseline content is unavailable: the change is real (the
                # manifest hashes differ) but its exact before-side text is
                # not reconstructable — say so instead of faking an empty file.
                lines.append(
                    f"@@ {label} @@ (baseline content unavailable; "
                    "change detected via submodule index/status fingerprint)\n"
                )
                inner_rendered.append(label)
                continue
            if new_content is None and (new is not None or new_untracked):
                lines.append(
                    f"@@ {label} @@ (current content unavailable; "
                    "change detected via submodule index/status fingerprint)\n"
                )
                inner_rendered.append(label)
                continue
            if old_content is not None and old_content == new_content:
                # The file is only in the union because ONE side's status text
                # named it; the actual content is unchanged, so there is no
                # hunk to show.
                continue
            before_inner = {
                "tracked": True,
                "kind": "file",
                "mode": old[0] if old else "100644",
                "content": old_content,
            }
            after_inner = {
                "tracked": True,
                "kind": "file",
                "mode": new[0] if new else "100644",
                "content": new_content,
            }
            rendered, ranges, deleted_ranges = self._render_plain_file_diff(
                label, before_inner, after_inner
            )
            lines.append(rendered)
            inner_rendered.append(label)
            if ranges:
                inner_anchors[label] = ranges
            if deleted_ranges:
                inner_deletions[label] = deleted_ranges
        return "".join(lines), inner_anchors, inner_deletions, inner_rendered

    @staticmethod
    def _causal_ranges(diff_text: str) -> Tuple[List[List[int]], List[List[int]]]:
        # Added lines anchor evidence in the CURRENT file's numbering space;
        # deleted lines live in the OLD file's space. Merging the two (as a
        # single range list) would let a ``path:N`` citation land on an
        # unchanged current line after a deletion shifted everything below it
        # — fabricated grounding — so the spaces stay separate.
        causal: List[int] = []
        deleted: List[int] = []
        old_line = 0
        new_line = 0
        in_hunk = False
        for line in diff_text.splitlines():
            match = _HUNK_RE.match(line)
            if match:
                old_line = int(match.group("old"))
                new_line = int(match.group("new"))
                in_hunk = True
                continue
            if not in_hunk:
                continue
            # Inside a hunk the leading char IS the diff marker: the
            # ``---``/``+++`` file headers never occur here (``in_hunk``
            # excludes them), so a deleted line whose text began with ``--``
            # renders as ``----…`` and must still count as a deletion — the
            # content prefix must not flip it into the context branch.
            if line.startswith("+"):
                causal.append(new_line)
                new_line += 1
            elif line.startswith("-"):
                deleted.append(old_line)
                old_line += 1
            elif line.startswith("\\ No newline"):
                continue
            else:
                old_line += 1
                new_line += 1
        return ReviewScopeManager._merge_line_ranges(causal), ReviewScopeManager._merge_line_ranges(deleted)

    @staticmethod
    def _merge_line_ranges(numbers: List[int]) -> List[List[int]]:
        numbers = sorted(set(numbers))
        if not numbers:
            return []
        ranges: List[List[int]] = []
        start = previous = numbers[0]
        for value in numbers[1:]:
            if value == previous + 1:
                previous = value
                continue
            ranges.append([start, previous])
            start = previous = value
        ranges.append([start, previous])
        return ranges

    def _write_descriptor(self, baseline: ReviewBaseline) -> None:
        directory = self._baseline_dir(baseline.baseline_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "descriptor.json"
        temporary = directory / f".descriptor.{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(baseline.to_dict(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(str(temporary), str(target))

    @staticmethod
    def _read_descriptor(path: Path) -> Optional[ReviewBaseline]:
        try:
            return ReviewBaseline.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _write_diff_artifact(self, baseline_id: str, content: str) -> str:
        directory = self._baseline_dir(baseline_id) / "diffs"
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        target = directory / f"{digest}.diff"
        if not target.exists():
            temporary = directory / f".{digest}.{uuid.uuid4().hex}.tmp"
            temporary.write_text(content, encoding="utf-8")
            os.replace(str(temporary), str(target))
        return str(target)


@dataclass
class DiffSection:
    """One rendered per-file section of a reconstructed review diff."""

    path: str
    old_path: str
    text: str


_DIFF_HEADER_PREFIX = "diff --git "


def _split_header_paths(header: str) -> Tuple[str, str]:
    """Recover the (old, new) paths a ``diff --git`` header names.

    Paths are emitted unquoted and may contain spaces, so the split point is
    searched from the right: the b-side marker is always the last ``" b/"`` in
    a header the renderer produced.
    """
    rest = header[len(_DIFF_HEADER_PREFIX):].rstrip("\n")
    if not rest.startswith("a/"):
        return "", ""
    index = rest.rfind(" b/")
    while index != -1:
        old = rest[2:index]
        new = rest[index + 3:]
        if old and new:
            return old, new
        index = rest.rfind(" b/", 0, index)
    return "", ""


def split_diff_sections(diff_text: str) -> List[DiffSection]:
    """Split a reconstructed unified diff into its per-file sections.

    Presentation-only: the sections are the exact substrings of the rendered
    diff, so re-joining a selection reproduces byte-identical diff text rather
    than a re-rendered approximation of it.
    """
    sections: List[DiffSection] = []
    current: List[str] = []
    old_path = ""
    new_path = ""

    def flush() -> None:
        if not current:
            return
        sections.append(
            DiffSection(path=new_path, old_path=old_path, text="".join(current))
        )

    for line in diff_text.splitlines(keepends=True):
        if line.startswith(_DIFF_HEADER_PREFIX):
            flush()
            current = [line]
            old_path, new_path = _split_header_paths(line)
            continue
        if current:
            current.append(line)
    flush()
    return sections


def section_covers_path(section: DiffSection, path: str) -> bool:
    """Whether a section renders ``path``.

    A submodule section is named by the gitlink path while its hunks are
    labeled with inner paths, so an inner file is matched by containment — a
    filter naming a real changed file must never come back empty just because
    its diff lives inside its submodule's section.
    """
    if not path:
        return False
    for candidate in (section.path, section.old_path):
        if not candidate:
            continue
        if path == candidate or path.startswith(candidate + "/"):
            return True
    return False


def count_anchor_lines(ranges: Optional[Sequence[Sequence[int]]]) -> int:
    """Number of individual lines covered by merged inclusive line ranges."""
    total = 0
    for item in ranges or ():
        try:
            start, end = int(item[0]), int(item[1])
        except (IndexError, TypeError, ValueError):
            continue
        if end >= start:
            total += end - start + 1
    return total


def diff_stat(scope: ReviewScope) -> Dict[str, Tuple[int, int]]:
    """Per-path ``(insertions, deletions)`` of a reconstructed scope.

    Counted from the scope's own anchors rather than by re-parsing the diff
    text: the anchors are what evidence validation grounds against, so a stat
    view and a finding's grounding can never disagree about which lines
    changed. Paths with no countable lines (binary, mode-only, submodule
    pointer moves) stay in the table at 0/0 — omitting them would present a
    changed file as unchanged.
    """
    paths = (
        set(scope.changed_paths)
        | set(scope.causal_anchors)
        | set(scope.deletion_anchors)
    )
    return {
        path: (
            count_anchor_lines(scope.causal_anchors.get(path)),
            count_anchor_lines(scope.deletion_anchors.get(path)),
        )
        for path in sorted(paths)
    }
