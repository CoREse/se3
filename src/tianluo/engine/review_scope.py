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
import shutil
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
# A flow id also becomes a path segment in the snapshot store, but unlike a
# baseline id it is not minted here — it arrives from persisted flow state and
# from CLI arguments. Reclaiming a snapshot directory is the only destructive
# operation in this module, so the id is held to the same single-segment shape
# rule: a leading alphanumeric rules out "." and "..", and no separator is
# accepted at all.
_SAFE_FLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Single name for the per-flow snapshot store, so the reclaim guard below and
# the path construction can never drift apart.
_SNAPSHOT_STORE_DIR = "review-scopes"

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

# Every character ``str.splitlines`` — the section reader's own splitter —
# treats as a line ending. Quoting is triggered by those plus the two
# characters a quoted token could not otherwise carry (``"`` and ``\``).
_LINE_BREAK_CHARS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
_DIFF_PATH_NEEDS_QUOTING_RE = re.compile(
    '[\x00-\x1f\x7f"\\\\\u0085\u2028\u2029\ud800-\udfff]'
)
_NAMED_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_NAMED_UNESCAPES = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}


def _diff_path_escape_bytes(char: str) -> bytes:
    """The byte spelling one escaped pathname character is rendered as.

    WHY the ``surrogateescape`` handler rather than a plain ``str.encode``: a
    POSIX pathname is bytes, and every path in this module reaches Python
    through ``decode("utf-8", "surrogateescape")`` (git's ``-z`` output, the
    index, the status porcelain), so a Git-visible byte that is not valid
    UTF-8 arrives here as a lone surrogate. Plain encoding raises on it, and
    the raised error — swallowed by the reconstruction's own catch-all — turns
    a perfectly real baseline-to-current change into an undecidable empty
    scope. The escape handler yields back the original byte instead, which is
    both git's own quoting spelling for that name and the exact input
    :func:`decode_quoted_diff_path` inverts.
    """
    try:
        return char.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        # A surrogate outside the escape range encodes to no byte sequence at
        # all, so it names no file on any filesystem and can only arrive from
        # a fabricated declared path. WTF-8 keeps the render total (the point
        # of this helper) rather than re-raising; the decoder resolves such a
        # token to the byte-accurate name, never back to the fabricated one.
        return char.encode("utf-8", "surrogatepass")


def quote_diff_path(label: str) -> str:
    """Render one pathname as a single, unambiguous diff-header token.

    WHY: the rendered diff is split back into per-file sections by reading it
    LINE BY LINE (see :func:`split_diff_sections`), so a pathname that itself
    contains a line break would tear its own ``diff --git`` header in two and
    the section would lose the path it names — while ``diff_stat`` keeps the
    exact pathname, leaving the ``--stat`` and diff views of one ``--path``
    filter disagreeing about which files changed. Such a name is escaped into
    a quoted token instead, in git's own C-quoting spelling, so the header
    stays one line and :func:`unquote_diff_path` recovers the exact name.
    Ordinary paths are emitted verbatim, exactly as before.

    WHY the second trigger — a leading or trailing whitespace character, which
    a single-line record CAN carry: every surface that shows a path is also a
    surface a checker copies citations off, and a citation is read back after
    ``str.strip()`` (see ``_evidence_path_candidates``), which is exactly the
    spelling the edge whitespace does not survive. Rendered raw, such a name
    would be presented as a spelling that can never ground — the silent
    bad-evidence drop the quoting exists to prevent — and would additionally
    be indistinguishable from its stripped namesake when both changed. The
    quoted token pins the edge characters inside the quotes, so the stripped
    citation still decodes to the exact name.

    WHY the third trigger — a lone surrogate, which is how a Git-visible path
    byte that is not valid UTF-8 reaches Python: rendered raw, that character
    makes the whole diff un-encodable, and the ``UnicodeEncodeError`` the
    artifact write then raises is swallowed upstream into an undecidable empty
    scope, silently hiding that file's real change from SELF_CHECK. Escaped to
    its own byte (see :func:`_diff_path_escape_bytes`), the token is plain
    ASCII, the diff and its artifact stay exactly reconstructable, and the
    name still round-trips.
    """
    text = str(label)
    if not _DIFF_PATH_NEEDS_QUOTING_RE.search(text) and text == text.strip():
        return text
    out = ['"']
    for char in text:
        named = _NAMED_ESCAPES.get(char)
        if named is not None:
            out.append(named)
        elif (
            ord(char) < 0x20
            or ord(char) == 0x7f
            or 0xD800 <= ord(char) <= 0xDFFF
            or char in _LINE_BREAK_CHARS
        ):
            # Octal per pathname byte, as git does, so the token survives a
            # round trip through byte-oriented tooling as well.
            out.extend(f"\\{byte:03o}" for byte in _diff_path_escape_bytes(char))
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def decode_quoted_diff_path(token: str) -> Optional[str]:
    """Decode *token* iff it is a well-formed :func:`quote_diff_path` token.

    Returns ``None`` — not a repaired string — for anything else: a token that
    is not quoted at all, or one whose escape sequences this renderer could
    never have emitted (an unknown escape, a lone trailing backslash, an
    unescaped inner quote, or an octal escape outside byte range).

    INVARIANT: decoding never invents a spelling. A caller uses the decoded
    name to select a real file (a diff section, an evidence anchor), so
    silently dropping the backslash of an unknown escape would let a token
    that names nothing — ``"src\\q.py"`` — alias the unrelated real path
    ``srcq.py``, either selecting the wrong file or passing evidence whose
    cited spelling was never presented. Rejection keeps the malformed token as
    the caller's own problem, matching nothing.

    INVARIANT: the octal escapes are inverted BYTEWISE, with the very
    ``surrogateescape`` handler every path in this module was decoded by — so
    non-UTF-8 octal bytes are a decodable name, not a malformed token. A
    POSIX pathname is bytes, so ``"\\301\\301"`` is the exact rendering of a
    real Git-visible file (see :func:`_diff_path_escape_bytes`); rejecting it
    here would leave the presented token grounding nothing, which is the same
    silent bad-evidence drop the quoting exists to prevent. This does not
    weaken the invariant above: the byte-to-name mapping is the platform's own
    and injective, so a decoded name is always precisely the file whose bytes
    the token spelled and can never alias a different real path.
    """
    text = str(token)
    if len(text) < 2 or not text.startswith('"') or not text.endswith('"'):
        return None
    body = text[1:-1]
    chunks: List[str] = []
    pending = bytearray()

    def flush() -> None:
        if pending:
            chunks.append(bytes(pending).decode("utf-8", "surrogateescape"))
            pending.clear()

    index = 0
    while index < len(body):
        char = body[index]
        if char == '"':
            # An unescaped quote closes a token; one inside the body means the
            # real token ended earlier and this text is not that token.
            return None
        if char != "\\":
            flush()
            chunks.append(char)
            index += 1
            continue
        index += 1
        if index >= len(body):
            return None
        marker = body[index]
        if marker in _NAMED_UNESCAPES:
            flush()
            chunks.append(_NAMED_UNESCAPES[marker])
            index += 1
            continue
        digits = body[index:index + 3]
        if len(digits) == 3 and all(digit in "01234567" for digit in digits):
            value = int(digits, 8)
            if value > 0xFF:
                return None
            pending.append(value)
            index += 3
            continue
        return None
    flush()
    return "".join(chunks)


def unquote_diff_path(token: str) -> str:
    """Recover the pathname :func:`quote_diff_path` rendered as *token*.

    An unquoted token is its own pathname; anything malformed is returned
    verbatim rather than repaired, because a section path is presentation
    state and inventing a name would select the wrong file. Callers that must
    tell "not a quoted token" from "decoded a quoted token" use
    :func:`decode_quoted_diff_path` directly.
    """
    text = str(token)
    decoded = decode_quoted_diff_path(text)
    return text if decoded is None else decoded


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
    # WHY the fallback records WHICH domain failed: an incremental round grounds
    # findings in two domains, and either one going missing routes to the same
    # full/undecidable fallback. Without this, the round can only say "the fix
    # baseline was untrustworthy" — which is a false accusation, and a
    # misleading availability claim, when the fix baseline rebuilt fine and it
    # was the implementation-baseline half that was missing or corrupt.
    # ``fix_baseline`` / ``task_baseline``; empty when no fallback happened.
    fallback_cause: str = ""
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
    # WHY a full round carries a SECOND, narrower anchor set: its own domain
    # spans the whole task, so the checker cannot tell which hunks it already
    # reviewed in the previous full round from the ones the fixes since then
    # added. These fields answer exactly that, and nothing else — they are
    # PRESENTATION data for the scope manifest and must never narrow evidence
    # grounding, which stays the round's own (whole-task) domain. They are
    # derived from persisted baselines alone, so they carry git facts only: no
    # fix-iteration count, trigger type, or closed-finding history. Empty on an
    # incremental round, where the round's own anchors ARE the fix delta.
    fix_delta_baseline_id: str = ""
    fix_delta_changed_paths: List[str] = field(default_factory=list)
    fix_delta_causal_anchors: Dict[str, List[List[int]]] = field(
        default_factory=dict
    )
    fix_delta_deletion_anchors: Dict[str, List[List[int]]] = field(
        default_factory=dict
    )
    fix_delta_available: bool = False
    fix_delta_diagnostic: str = ""
    # WHY a THIRD, purely path-level fact travels with the two anchor sets:
    # the manifest tells "this delta touched the path" from "the path also
    # carries earlier work" by subtracting added ranges, and an anchor-less
    # path (binary, mode-only, rename-only, deletion-only) has no ranges to
    # subtract — it would always read as delta-only. These are the delta paths
    # the two baseline SNAPSHOTS prove work already touched before the delta
    # baseline was taken (inside a captured submodule too) — a recorded git
    # fact of the same kind as the anchors: no fix-iteration count, trigger
    # type or closed-finding history rides in.
    prior_work_paths: List[str] = field(default_factory=list)
    # INVARIANT: the changed paths that entered this scope from a step's
    # self-report alone, because git cannot see them at all (ignored files) and
    # no baseline snapshot can hold them. They produce no diff anchor under ANY
    # baseline comparison the round grounds on — a verdict from ONE
    # reconstruction is not enough, since a file tracked at one capture and
    # ignored by the next is declared-only for the later snapshot while the
    # earlier one anchors it, which is why the attach helpers subtract every
    # path a grounding comparison placed. Hence NOTHING persisted can say which
    # domain they belong to — the manifest lists them by path with no domain mark rather
    # than guessing one, and no execution-side bookkeeping is introduced to
    # manufacture the missing attribution.
    declared_only_paths: List[str] = field(default_factory=list)

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


def _anchored_paths(
    changed_paths: Sequence[str], declared_only_paths: Sequence[str]
) -> set:
    """The paths a reconstruction's baseline comparison actually placed.

    WHY the subtraction is spelled once, here: "the comparison placed this
    path" is exactly "it entered ``changed_paths`` other than as a declared-only
    self-report", and both attach helpers must apply the same test to keep the
    manifest's anchor-less list equal to the paths NO grounding comparison can
    reach.
    """
    return set(_clean_path_list(list(changed_paths))) - set(
        _clean_path_list(list(declared_only_paths))
    )


def _clean_path_list(value: Any) -> List[str]:
    # INVARIANT: drop blanks, never rewrite a nonempty entry. These are
    # repository-relative paths whose only identity is their exact spelling —
    # a file really named with a leading or trailing space is reachable under
    # that name and no other — so ``strip()`` is the emptiness test here, not
    # a normalization.
    if not isinstance(value, list):
        return []
    return [
        item
        for item in value
        if isinstance(item, str) and item.strip()
    ]


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
        self.root = (
            runtime_dir(self.project_root)
            / "state"
            / _SNAPSHOT_STORE_DIR
            / self.flow_id
        )

    def load_baseline(self, baseline_id: str) -> Optional[ReviewBaseline]:
        """Load one captured baseline descriptor from the runtime store."""
        try:
            descriptor_path = self._baseline_dir(baseline_id) / "descriptor.json"
        except ValueError:
            return None
        baseline = self._read_descriptor(descriptor_path)
        if baseline is None or not self._owns_descriptor(baseline, str(baseline_id)):
            return None
        return baseline

    def _owns_descriptor(
        self, baseline: ReviewBaseline, expected_id: str
    ) -> bool:
        """Whether a descriptor really is THIS flow's snapshot of that id.

        INVARIANT: a descriptor's persisted identity must agree with where it
        was found before anything reconstructs from it. Location alone is not
        evidence of identity — a descriptor is a plain JSON file that a copy, a
        restored backup or a hand edit can put under another id or another
        flow's store — and every consumer downstream treats what loads here as
        the baseline the caller asked for. Accepting a foreign one would let a
        different flow's snapshot be rendered as this flow's review scope, an
        answer that is wrong while looking entirely healthy; refusing it routes
        the file through the corrupt-descriptor diagnostic instead, which names
        a real defect the operator can act on.
        """
        return (
            baseline.baseline_id == expected_id
            and baseline.flow_id == self.flow_id
        )

    def store_exists(self) -> bool:
        """Whether this flow's baseline snapshot directory is still on disk."""
        return self.root.is_dir()

    def discard_snapshots(self) -> bool:
        """Reclaim this flow's whole baseline snapshot directory.

        Baselines are heavy — a descriptor plus a content blob for every dirty
        or untracked file at capture time, once per implementation and once per
        fix iteration — and nothing else ever removes them, so a project that
        runs many flows accumulates every snapshot it ever took.

        INVARIANT: this may only be called at a flow's *terminal* point. Any
        flow ``luo run --resume`` still offers (which is every status but
        COMPLETED — a FAILED flow is offered as a retry) must keep its
        baselines, because the SELF_CHECK round it resumes into has nothing
        left to diff against once they are gone.

        Returns True when a directory was actually removed. A flow id that is
        not a safe single path segment raises :class:`ValueError` — a reclaim
        must never be talked into walking out of the store — while any other
        failure is reported as False and logged: freeing disk space must never
        be the reason a flow fails to terminate.
        """
        if not _SAFE_FLOW_ID_RE.match(self.flow_id):
            raise ValueError(f"unsafe review scope flow id: {self.flow_id!r}")
        root = self.root
        # Independent confirmation that the resolved path really is one flow's
        # entry in the snapshot store: the id check above constrains the
        # segment, this constrains where the segment sits.
        if root.parent.name != _SNAPSHOT_STORE_DIR:
            raise ValueError(f"refusing to reclaim outside the store: {root}")
        if not root.is_dir():
            return False
        try:
            shutil.rmtree(root)
        except OSError as exc:
            logger.warning(
                "Could not reclaim review baselines for flow %s at %s: %s",
                self.flow_id, root, exc,
            )
            return False
        logger.debug("Reclaimed review baselines for flow %s", self.flow_id)
        return True

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
            # Same ownership rule as ``load_baseline``: a role resolved by
            # scanning the store must not pick up a descriptor that belongs
            # somewhere else.
            if baseline is not None and self._owns_descriptor(baseline, entry.name):
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

    def earliest_fix_baseline_after_full_round(
        self, scope_context: Optional[Dict[str, Any]]
    ) -> Optional[ReviewBaseline]:
        """The earliest fix baseline captured since the last full round ran.

        A full round diffs from the implementation baseline, so its own diff
        cannot say which hunks arrived after the previous full round already
        reviewed the code. Diffing from this baseline does: everything it holds
        is work the fixes since that round produced. ``full_round_fix_head`` is
        the fix-history position recorded when the previous full round started
        (see ``StateMachine._attach_review_scope``); an empty/unknown marker
        means no full round has consumed any fix yet, so the whole fix history
        is "since the last full round".

        Presentation only: a missing answer costs the manifest an annotation
        and never changes what a round reviews or grounds on.

        INVARIANT: only the EARLIEST baseline after the marker may answer, and
        an unloadable one answers None rather than deferring to the next entry.
        A later baseline was captured after some of the post-full-round fixes
        had already landed, so its diff holds only the tail of the cumulative
        delta — annotating with it would label the skipped fixes' hunks
        "already present at the last full round", which is exactly the claim
        this annotation exists to make and exactly the one that would be false.
        Losing the annotation is presentation-only; asserting a wrong one is
        not.
        """
        if not isinstance(scope_context, dict):
            return None
        head = str(scope_context.get("full_round_fix_head", "") or "")
        history = scope_context.get("fix_baseline_history")
        ordered = [
            str(entry.get("baseline_id") or "")
            for entry in (history if isinstance(history, list) else [])
            if isinstance(entry, dict) and entry.get("baseline_id")
        ]
        if ordered:
            if head in ordered:
                remaining = ordered[ordered.index(head) + 1:]
            else:
                remaining = ordered
            if not remaining:
                return None
            return self.load_fix_baselines(scope_context).get(remaining[0])
        # Pre-history persisted flows and synthetic callers carry only the
        # latest fix baseline; it is the whole history they have.
        latest = scope_context.get("latest_fix_baseline")
        latest_id = (
            str(latest.get("baseline_id") or "")
            if isinstance(latest, dict) else ""
        )
        if latest_id and latest_id != head:
            return ReviewBaseline.from_dict(latest)
        return None

    @staticmethod
    def declared_changed_paths(
        flow_context: Optional[Dict[str, Any]],
    ) -> List[str]:
        """The implement-reported paths the flow's rounds were scoped with.

        WHY the flow persists them at all: ``reconstruct`` consults declared
        paths for exactly one case — a file git ignores, which baseline capture
        provably cannot hold — and a later FIX reports only ITS own files, so a
        round rebuilt from the last report alone would lose every ignored file
        an earlier step created. The accumulated union is what each round is
        scoped with, and it is read back here so a round is re-prepared (on a
        resume, or on a later pass) with the same input it first ran on.

        INVARIANT: one flat, flow-wide union — never split or attributed per
        baseline. Such a path produces no diff anchor under ANY baseline
        comparison, so no persisted git fact can say which domain it belongs
        to; inferring one would take execution-side bookkeeping of who declared
        what. Consumers therefore either present these paths WITHOUT a domain
        mark (the round manifest — see ``ReviewScope.declared_only_paths``) or
        leave them out entirely, as ``luo review-scope diff`` does: a view that
        claims to be one baseline's comparison may not carry a path that
        comparison cannot place.
        """
        scope_context: Dict[str, Any] = {}
        if isinstance(flow_context, dict):
            candidate = flow_context.get("review_scope")
            if isinstance(candidate, dict):
                scope_context = candidate
        return sorted(set(
            _clean_path_list(scope_context.get("declared_changed_paths"))
        ))

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
                # INVARIANT: "capture failed" outranks "reclaimed" when the
                # descriptor is absent. A capture that raised never wrote one —
                # the state machine synthesizes an ``available=False`` record
                # into the flow context instead (see
                # ``_ensure_implementation_review_baseline``) — so the absence
                # on disk IS that failure, not a later cleanup. Reporting it as
                # reclaimed would swallow the capture diagnostic and send the
                # operator after a snapshot that never existed; the two stay
                # separately reportable, including when a later reclaim runs
                # over a store that already held no descriptor for this id.
                failure = self._recorded_capture_failure(scope_context, baseline_id)
                if failure is not None:
                    return BaselineLookup(
                        status=BASELINE_STATUS_UNAVAILABLE,
                        selector=selector,
                        baseline_id=baseline_id,
                        diagnostic=failure,
                    )
                # The flow still names this baseline, so it WAS captured, and
                # the store tells which of the two remaining situations it is.
                # INVARIANT: "reclaimed" is claimed ONLY when the whole store
                # is gone, because reclaiming is all-or-nothing — cleanup
                # removes this flow's entire snapshot directory (see
                # ``discard_snapshots``). A store still on disk means cleanup
                # never ran, so a descriptor that will not load there is a
                # damaged or hand-removed snapshot of a flow that may still be
                # resumable; telling its operator the snapshots were reclaimed
                # at flow termination names an event that did not happen and
                # points the remedy at the wrong thing.
                if self.store_exists():
                    return BaselineLookup(
                        status=BASELINE_STATUS_UNAVAILABLE,
                        selector=selector,
                        baseline_id=baseline_id,
                        diagnostic=self._descriptor_defect(baseline_id),
                    )
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

    def _descriptor_defect(self, baseline_id: str) -> str:
        """Why a named baseline's descriptor did not load, for the operator.

        Missing and corrupt are separated because the remedies differ: a
        descriptor file that is simply not there points at something outside
        the flow having removed it, while one that is present but unreadable
        points at a truncated or partially written file.
        """
        try:
            descriptor_path = self._baseline_dir(baseline_id) / "descriptor.json"
        except ValueError:
            return f"unsafe review baseline id: {baseline_id!r}"
        if not descriptor_path.exists():
            return (
                "baseline snapshot descriptor is missing while the flow's "
                f"snapshot store is still present: {descriptor_path}"
            )
        return (
            "baseline snapshot descriptor is unreadable or corrupt: "
            f"{descriptor_path}"
        )

    @staticmethod
    def _recorded_capture_failure(
        scope_context: Dict[str, Any], baseline_id: str
    ) -> Optional[str]:
        """The capture diagnostic the flow recorded for ``baseline_id``, if any.

        Returns ``None`` when the flow's own records do not say the capture
        failed — the id may simply not appear, or appear as a healthy capture.
        An empty diagnostic list still yields a (generic) string, because the
        distinction the caller needs is "was this ever a usable snapshot", and
        a failed capture that recorded no detail is still a failed capture.

        WHY every record naming the id is read instead of the first: the same
        baseline is written to more than one place (the role slot AND the fix
        history entry), and the copies do not all carry the diagnostic — the
        history entry has it while the ``latest_fix_baseline`` slot may not.
        Stopping at the first match would report the generic fallback while the
        real reason sat one record away.
        """
        records: List[Dict[str, Any]] = []
        for key in ("implementation_baseline", "latest_fix_baseline"):
            value = scope_context.get(key)
            if isinstance(value, dict):
                records.append(value)
        history = scope_context.get("fix_baseline_history")
        if isinstance(history, list):
            records.extend(item for item in history if isinstance(item, dict))
        failed = False
        details: List[str] = []
        for record in records:
            if str(record.get("baseline_id") or "") != baseline_id:
                continue
            if record.get("available") is not False:
                continue
            failed = True
            diagnostics = record.get("diagnostics")
            if not isinstance(diagnostics, list):
                continue
            for item in diagnostics:
                text = str(item)
                if text and text not in details:
                    details.append(text)
        if not failed:
            return None
        return "; ".join(details) or "baseline capture failed (no diagnostic recorded)"

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
        fix_delta_baseline: Optional[ReviewBaseline] = None,
        declared_paths: Optional[Sequence[str]] = None,
    ) -> ReviewScope:
        """Reconstruct a scope, falling incremental failures back to full.

        On a decidable incremental round the implementation baseline is
        reconstructed a SECOND time and attached to the result as the
        ``task_*`` fields. ``full_baseline`` is therefore no longer only the
        undecidable-fallback source: it is also the whole-task evidence domain
        the round grounds findings against (see ``ReviewScope``).

        ``fix_delta_baseline`` plays the mirror-image role on a full round: it
        is the earliest fix baseline captured since the previous full round, and
        it is reconstructed only so the scope manifest can mark which of this
        round's hunks are new since that round. It never narrows the round.

        INVARIANT: an incremental round is decidable only when BOTH of its
        evidence domains were rebuilt. The whole-task domain is not an optional
        widening of the fix delta — it is half of the domain the round grounds
        findings in, so losing it would silently narrow the evidence rule back
        to the fix delta while the round still reported itself decidable, and a
        finding anchored on real earlier task work would be dropped as bad
        evidence. Losing it therefore takes the SAME established route as an
        undecidable incremental baseline: fall back to full, where the round's
        own baseline is the implementation baseline and the whole task is in
        scope by construction.
        """
        mode = "incremental" if requested_mode == "incremental" else "full"
        result = self.reconstruct(mode, baseline, declared_paths=declared_paths)
        if mode != "incremental":
            self._attach_fix_delta_scope(
                result,
                fix_delta_baseline,
                round_baseline=baseline,
                declared_paths=declared_paths,
            )
            return result

        if not result.undecidable:
            if self._attach_task_scope(
                result,
                full_baseline,
                round_baseline=baseline,
                declared_paths=declared_paths,
            ):
                return result
            incremental_diagnostic = result.task_scope_diagnostic
            fallback_cause = "task_baseline"
            # WHY the two causes are worded apart: naming the fix baseline as
            # the failure when it rebuilt cleanly sends the reader (and the
            # checker reading the prompt built from this) after a healthy
            # snapshot, and hides that the implementation-baseline domain — the
            # very one the fallback routes to — is what is missing.
            failure = (
                "the fix baseline rebuilt, but the whole-task half of the "
                "incremental evidence domain did not"
            )
        else:
            incremental_diagnostic = result.diagnostic
            fallback_cause = "fix_baseline"
            failure = "the fix baseline was undecidable"

        full = self.reconstruct(
            "full", full_baseline, declared_paths=declared_paths
        )
        full.requested_mode = "incremental"
        full.fallback_from_incremental = True
        full.fallback_cause = fallback_cause
        prefix = (
            f"Incremental round could not be reconstructed ({failure}); review "
            "safely fell back to the implementation baseline "
            f"({incremental_diagnostic})."
        )
        full.diagnostic = f"{prefix} {full.diagnostic}".strip()
        self._attach_fix_delta_scope(
            full,
            fix_delta_baseline,
            round_baseline=full_baseline,
            declared_paths=declared_paths,
        )
        return full

    def _attach_fix_delta_scope(
        self,
        result: ReviewScope,
        fix_delta_baseline: Optional[ReviewBaseline],
        *,
        round_baseline: Optional[ReviewBaseline] = None,
        declared_paths: Optional[Sequence[str]] = None,
    ) -> None:
        """Attach the since-last-full-round fix anchors to a full scope.

        WHY a failure here never degrades the round, and never writes an
        artifact: this set is read by the manifest renderer alone. Losing it
        costs one annotation; letting it turn a usable full round undecidable —
        or grow the flow's on-disk diff record — would be a real regression for
        a purely descriptive input.
        """
        if fix_delta_baseline is None:
            return
        if fix_delta_baseline.baseline_id == result.baseline_id:
            return
        delta = self.reconstruct(
            "incremental",
            fix_delta_baseline,
            declared_paths=declared_paths,
            write_artifact=False,
        )
        if delta.undecidable:
            result.fix_delta_diagnostic = (
                "changes since the last full round could not be isolated "
                f"({delta.diagnostic})"
            )
            return
        result.fix_delta_baseline_id = delta.baseline_id
        result.fix_delta_changed_paths = list(delta.changed_paths)
        result.fix_delta_causal_anchors = delta.causal_anchors
        result.fix_delta_deletion_anchors = delta.deletion_anchors
        result.fix_delta_available = True
        # INVARIANT: a path this round's OWN (grounding) comparison anchored
        # never joins the declared-only set, however the auxiliary
        # reconstruction classified it. A file tracked when the implementation
        # baseline was captured and git-ignored by the time the fix baseline
        # was, then self-reported, is declared-only for the fix reconstruction
        # alone — unioning that verdict in would make the manifest present an
        # anchor-bearing path as anchor-less, suppressing its counts, ranges
        # and deletion lines and inviting the one citation form
        # ``_validate_evidence`` then drops as bad evidence.
        result.declared_only_paths = sorted(
            (set(result.declared_only_paths) | set(delta.declared_only_paths))
            - _anchored_paths(result.changed_paths, result.declared_only_paths)
        )
        # Earlier here means "already present at the previous full round": the
        # round's own (implementation) baseline is the earlier snapshot, the
        # pinned fix baseline the later one.
        result.prior_work_paths = self.paths_changed_between(
            round_baseline, fix_delta_baseline, delta.changed_paths
        )

    def _attach_task_scope(
        self,
        result: ReviewScope,
        full_baseline: Optional[ReviewBaseline],
        *,
        round_baseline: Optional[ReviewBaseline] = None,
        declared_paths: Optional[Sequence[str]] = None,
    ) -> bool:
        """Attach the implementation-baseline anchor set to an incremental scope.

        Returns whether the whole-task evidence domain is now available on
        ``result``.

        WHY a failure here is NOT absorbed as a lost annotation: the whole-task
        diff is one of the two domains an incremental round grounds findings
        in, not a decoration on top of the fix delta. Keeping the round
        decidable without it would silently narrow the evidence rule back to
        the fix delta — a finding anchored on real earlier task work would then
        be dropped as bad evidence, with nothing in the round saying the domain
        was missing. The caller therefore routes a ``False`` into the same
        full/undecidable fallback an undecidable incremental baseline takes,
        where the whole task is in scope by construction.
        """
        if full_baseline is None:
            result.task_scope_diagnostic = (
                "implementation baseline is missing, so the whole-task "
                "evidence domain could not be reconstructed"
            )
            return False
        if full_baseline.baseline_id == result.baseline_id:
            # The round already diffs from the implementation baseline, so its
            # own anchors ARE the whole-task anchors.
            return True
        task = self.reconstruct(
            "full", full_baseline, declared_paths=declared_paths
        )
        if task.undecidable:
            result.task_scope_diagnostic = (
                "whole-task diff could not be reconstructed "
                f"({task.diagnostic})"
            )
            return False
        result.task_baseline_id = task.baseline_id
        result.task_changed_paths = list(task.changed_paths)
        result.task_causal_anchors = task.causal_anchors
        result.task_deletion_anchors = task.deletion_anchors
        result.task_artifact_path = task.artifact_path
        result.task_scope_available = True
        # INVARIANT: an incremental round grounds on BOTH comparisons, so a
        # path either of them anchored never joins the declared-only set. A
        # file tracked at implementation-baseline capture and git-ignored by
        # fix-baseline capture is declared-only for the fix reconstruction
        # only, while the whole-task comparison holds real anchors for it —
        # unioning that verdict in would advertise an anchor-bearing path as
        # anchor-less and turn a bare-path citation into a silent bad-evidence
        # drop.
        result.declared_only_paths = sorted(
            (set(result.declared_only_paths) | set(task.declared_only_paths))
            - _anchored_paths(result.changed_paths, result.declared_only_paths)
            - _anchored_paths(task.changed_paths, task.declared_only_paths)
        )
        # Earlier here means "done by this task before the current fix": the
        # implementation baseline is the earlier snapshot, this round's own fix
        # baseline the later one.
        result.prior_work_paths = self.paths_changed_between(
            full_baseline, round_baseline, result.changed_paths
        )
        return True

    def paths_changed_between(
        self,
        earlier: Optional[ReviewBaseline],
        later: Optional[ReviewBaseline],
        paths: Sequence[str],
    ) -> List[str]:
        """Which of ``paths`` differ between two captured baseline snapshots.

        WHY this exists next to the anchor sets: a manifest domain mark that is
        read off added ranges alone cannot classify an anchor-less path
        (binary, mode-only, rename-only, deletion-only), because such a path
        owns no range to attribute. Two snapshots of the SAME path, however,
        answer "did work already touch this before the later baseline was
        taken" directly — and the snapshots are exactly the persisted git facts
        the round is already built on.

        Read-only and total: a snapshot whose blob can no longer be read leaves
        its path out of the answer rather than failing the round, because the
        result is a presentation annotation and losing one mark must never cost
        a usable review.
        """
        if earlier is None or later is None:
            return []
        out: List[str] = []
        for path in sorted({str(item) for item in paths if str(item)}):
            try:
                if self._baseline_entry_differs(earlier, later, path):
                    out.append(path)
            except Exception as exc:  # noqa: BLE001 - annotation, never a gate
                logger.debug(
                    "Review baseline comparison unavailable for %s: %s", path, exc
                )
        return out

    def _baseline_entry_differs(
        self, earlier: ReviewBaseline, later: ReviewBaseline, path: str
    ) -> bool:
        before = earlier.tracked.get(path) or earlier.untracked.get(path)
        after = later.tracked.get(path) or later.untracked.get(path)
        before_absent = not before or before.get("storage") == "missing"
        after_absent = not after or after.get("storage") == "missing"
        if before_absent and after_absent:
            # A submodule's inner files are rendered as changed paths in their
            # own right, but a snapshot holds them under the PARENT gitlink's
            # manifest, never under the inner path itself. Look there before
            # concluding anything, or every anchor-less inner path (a binary
            # both IMPLEMENT and this fix rewrote, say) would read as
            # untouched-before and be credited entirely to this fix.
            parent = self._gitlink_parent(earlier, later, path)
            if parent is not None:
                return self._gitlink_inner_differs(
                    earlier, later, parent, path[len(parent) + 1:]
                )
            # Neither snapshot holds the path. A file git ignores is invisible
            # to both captures, so "same" is the only claim the snapshots
            # support — never an assertion that it was changed. Such a path
            # carries no domain mark at all rather than a guessed one; see
            # ``ReviewScope.declared_only_paths``.
            return False
        if before_absent != after_absent:
            return True
        assert before is not None and after is not None
        before_ref = self._descriptor_identity(before)
        after_ref = self._descriptor_identity(after)
        if (
            before_ref is not None
            and after_ref is not None
            and before_ref[0] == after_ref[0]
        ):
            # Same storage form on both sides: the recorded reference is
            # content-addressed within that one space, so it decides equality
            # without reading a single blob back.
            return before_ref != after_ref
        return self._entry_identity(
            self._load_baseline_entry(earlier, before)
        ) != self._entry_identity(self._load_baseline_entry(later, after))

    @staticmethod
    def _gitlink_descriptor(
        baseline: ReviewBaseline, path: str
    ) -> Optional[Dict[str, Any]]:
        descriptor = baseline.tracked.get(path) or baseline.untracked.get(path)
        if not descriptor or descriptor.get("storage") == "missing":
            return None
        if str(descriptor.get("kind", "file")) != "gitlink":
            return None
        return descriptor

    def _gitlink_parent(
        self, earlier: ReviewBaseline, later: ReviewBaseline, path: str
    ) -> Optional[str]:
        """The submodule one rendered inner path lives in, if any.

        The longest ancestor wins so a nested submodule is attributed to the
        checkout that actually records the file, not to the outer one.
        """
        segments = str(path).split("/")
        for cut in range(len(segments) - 1, 0, -1):
            candidate = "/".join(segments[:cut])
            if (
                self._gitlink_descriptor(earlier, candidate) is not None
                or self._gitlink_descriptor(later, candidate) is not None
            ):
                return candidate
        return None

    @staticmethod
    def _gitlink_inner_identity(
        descriptor: Optional[Dict[str, Any]], inner: str
    ) -> Optional[Tuple[str, ...]]:
        """One inner file's recorded identity inside a captured submodule.

        The worktree hashes win over the index entry because the manifest
        records them exactly when the checkout's content differs from what the
        index says — the index sha would then name a version that was never on
        disk at capture. ``None`` means the submodule did not hold the file.
        """
        if not descriptor:
            return None
        manifest = descriptor.get("submodule_manifest")
        if not isinstance(manifest, dict):
            return None
        for key in ("dirty_hashes", "untracked"):
            table = manifest.get(key)
            if isinstance(table, dict) and inner in table:
                return ("worktree", str(table[inner]))
        files = manifest.get("files")
        if isinstance(files, dict) and inner in files:
            spec = files[inner]
            if isinstance(spec, (list, tuple)) and len(spec) >= 2:
                return ("index", str(spec[0]), str(spec[1]))
        return None

    def _gitlink_inner_differs(
        self,
        earlier: ReviewBaseline,
        later: ReviewBaseline,
        parent: str,
        inner: str,
    ) -> bool:
        """Whether the snapshots recorded different content for an inner file."""
        if not inner or not self._safe_inner_path(inner):
            return False
        before_desc = self._gitlink_descriptor(earlier, parent)
        after_desc = self._gitlink_descriptor(later, parent)
        before_id = self._gitlink_inner_identity(before_desc, inner)
        after_id = self._gitlink_inner_identity(after_desc, inner)
        if before_id is None and after_id is None:
            return False
        if before_id is None or after_id is None:
            return True
        if before_id[0] == after_id[0]:
            # Same recorded form on both sides: the reference is
            # content-addressed within that one space and decides equality
            # outright, with no blob read.
            return before_id != after_id
        # The two sides recorded the file in different spaces (a git blob sha1
        # against a worktree sha256), which are not comparable as references —
        # only the bytes themselves answer, so a checkout that committed the
        # very content the earlier snapshot saw dirty is not mis-marked.
        before_bytes = self._gitlink_inner_content(earlier, parent, inner)
        after_bytes = self._gitlink_inner_content(later, parent, inner)
        if before_bytes is None or after_bytes is None:
            return True
        return before_bytes != after_bytes

    def _gitlink_inner_content(
        self, baseline: ReviewBaseline, parent: str, inner: str
    ) -> Optional[bytes]:
        """One inner file's captured bytes, or ``None`` when unreadable."""
        descriptor = self._gitlink_descriptor(baseline, parent)
        if descriptor is None:
            return None
        entry = self._load_baseline_entry(baseline, descriptor)
        if entry is None:
            return None
        spec: Optional[List[str]] = None
        manifest = entry.get("manifest")
        if isinstance(manifest, dict):
            files = manifest.get("files")
            raw = files.get(inner) if isinstance(files, dict) else None
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                spec = [str(raw[0]), str(raw[1])]
        return self._gitlink_file_before(entry, parent, inner, spec)

    @staticmethod
    def _descriptor_identity(descriptor: Dict[str, Any]) -> Optional[Tuple[str, ...]]:
        """A descriptor's content-addressed identity, when it has one.

        ``None`` means the descriptor cannot be compared without loading it
        (an unknown storage form, or a gitlink whose identity is its submodule
        manifest rather than the recorded reference).
        """
        storage = str(descriptor.get("storage", ""))
        kind = str(descriptor.get("kind", "file"))
        if kind == "gitlink":
            return None
        if storage == "git":
            reference = str(descriptor.get("object_id", ""))
        elif storage == "blob":
            reference = str(descriptor.get("blob_sha256", ""))
        else:
            return None
        if not reference:
            return None
        return (storage, kind, str(descriptor.get("mode", "")), reference)

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

        # Loaded through the ownership check rather than read raw: the caller's
        # copy may itself have come from a persisted flow record, so "the two
        # agree" only proves the snapshot is this flow's when the file on disk
        # was accepted as this flow's first (see ``_owns_descriptor``).
        on_disk = self.load_baseline(baseline.baseline_id)
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
            result.declared_only_paths = list(ignored)
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
            result.declared_only_paths = []
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
            # WHY the reported spelling is tried before the trimmed one, and
            # why the first spelling that RESOLVES ends the search: a
            # repository path may legitimately carry a leading or trailing
            # space, and such a file is anchor-less by definition — no diff can
            # correct the name, so trimming it unconditionally would silently
            # substitute a nonexistent path and lose the real change. The
            # trimmed spelling stays as a fallback because a report is
            # LLM-authored text that just as often carries stray whitespace;
            # it is consulted only where the reported spelling names nothing
            # this repository holds. A spelling git already accounts for — one
            # the reconstruction already carries, one listed on either side of
            # the comparison, or one excluded as runtime state — has resolved
            # just as decisively as one found on disk: falling through it would
            # let a real changed ``"a.py "`` resolve a second time to an
            # unrelated ignored ``a.py`` and put a file the flow never touched
            # into the manifest. Separator normalization (``\`` → ``/``) is one
            # more fallback of exactly the same shape and for the same reason:
            # on POSIX a backslash is an ordinary filename character, so a
            # changed ``tracked\file.py`` must be resolved as spelled before
            # the Windows reading is tried — rewriting it up front would
            # silently resolve an unrelated ignored ``tracked/file.py`` and
            # drop the real change from the manifest. Every test applied to a
            # candidate obeys that same order, runtime-state exclusion
            # included: it reads each spelling with this platform's
            # separators only, so the Windows reading of
            # ``tianluo\state\note.py`` is judged runtime state only once
            # that exact spelling has resolved nowhere and the normalized
            # candidate is reached.
            candidates: List[str] = []
            for spelling in (
                raw,
                raw.strip(),
                raw.replace("\\", "/"),
                raw.strip().replace("\\", "/"),
            ):
                if spelling and spelling not in candidates:
                    candidates.append(spelling)
            for path in candidates:
                # Trim a leading "./" only — a character-class strip would eat
                # the leading dot of a legitimate dotfile like ``.env.local``.
                while path.startswith("./"):
                    path = path[2:]
                if not path:
                    continue
                try:
                    self._validate_relative_path(path)
                except ValueError:
                    continue
                if (
                    path in known
                    or path in current_tracked
                    or path in current_untracked
                    or path in baseline.tracked
                    or path in baseline.untracked
                    or self._is_runtime_untracked(path)
                ):
                    break
                absolute = self.project_root / path
                try:
                    if not absolute.is_file():
                        continue
                except OSError:
                    continue
                out.append(path)
                break
        return sorted(set(out))

    @staticmethod
    def _render_ignored_notes(paths: Sequence[str]) -> str:
        """Explicit notes for git-ignored declared paths (never a fake diff)."""
        lines: List[str] = []
        for path in paths:
            token = quote_diff_path(path)
            lines.append(f"diff --git a/{token} b/{token}\n")
            lines.append(
                f"@@ {token} @@ (git-ignored path reported changed by the "
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
        """Whether a repository path names runtime state left out of capture.

        WHY separators are read platform-literally and ``\\`` is never
        rewritten to ``/``: on POSIX a backslash is an ordinary filename
        character, and both path sources this predicate serves spell paths
        that way. Git enumerates every path with ``/``, so a backslash it
        reports is part of the name — rewriting it would drop a real changed
        ``tianluo\\state\\note.py`` from baseline capture as though it were
        runtime state, leaving that file's baseline-to-current diff
        unreconstructable. A DECLARED path is resolved as spelled first for
        the same reason, and its caller offers the separator-normalized
        spelling as a later candidate, so the Windows reading of such a
        spelling is still classified — only after the exact spelling has
        resolved nowhere. Honouring just the separators this platform actually
        has therefore serves both sources with one reading.
        """
        runtime = runtime_dir_name(self.project_root).rstrip("/")
        spelling = path.replace(os.sep, "/")
        if os.altsep:
            spelling = spelling.replace(os.altsep, "/")
        parts = [part for part in spelling.split("/") if part]
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
            old_token = quote_diff_path(old_path)
            new_token = quote_diff_path(new_path)
            pieces.append(
                f"diff --git a/{old_token} b/{new_token}\n"
                "similarity index 100%\n"
                f"rename from {old_token}\nrename to {new_token}\n"
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
        token = quote_diff_path(path)
        old_label = f"a/{token}" if before is not None else "/dev/null"
        new_label = f"b/{token}" if after is not None else "/dev/null"
        lines = [f"diff --git a/{token} b/{token}\n"]
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
        token = quote_diff_path(path)
        lines = [f"diff --git a/{token} b/{token}\n"]
        if before is None or after is None:
            lines.append(
                f"{'new' if before is None else 'deleted'} submodule {token}\n"
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
            label_token = quote_diff_path(label)
            if old_content is None and (old is not None or old_untracked):
                # The baseline content is unavailable: the change is real (the
                # manifest hashes differ) but its exact before-side text is
                # not reconstructable — say so instead of faking an empty file.
                #
                # INVARIANT: a degraded note carries its OWN ``diff --git``
                # header, exactly as a reconstructable inner file does. The
                # header is what splits the rendered diff into per-path
                # sections, so without one this note would ride inside the
                # parent gitlink section (or, when a reconstructable inner
                # file was rendered first, inside THAT file's section) and a
                # single-file view of a sibling path would expose an unrelated
                # changed file the ``--stat`` view of the same filter does not
                # list. Both views resolve a filter through path containment
                # alone, so every path the diff names must be splittable out
                # under its own name.
                lines.append(
                    f"diff --git a/{label_token} b/{label_token}\n"
                )
                lines.append(
                    f"@@ {label_token} @@ (baseline content unavailable; "
                    "change detected via submodule index/status fingerprint)\n"
                )
                inner_rendered.append(label)
                continue
            if new_content is None and (new is not None or new_untracked):
                lines.append(
                    f"diff --git a/{label_token} b/{label_token}\n"
                )
                lines.append(
                    f"@@ {label_token} @@ (current content unavailable; "
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
        # WHY the descriptor is written (and read) through
        # ``surrogateescape``: its keys are pathnames, and a POSIX pathname is
        # bytes — a Git-visible byte that is not valid UTF-8 arrives as a lone
        # surrogate that plain UTF-8 encoding refuses. This write is the LAST
        # step of ``capture`` and sits outside its degradation guard, so
        # raising here would abort the whole IMPLEMENT/FIX step rather than
        # degrade; the escape handler restores the original byte instead,
        # keeping the descriptor an exact record of what was captured.
        temporary.write_text(
            json.dumps(baseline.to_dict(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
            errors="surrogateescape",
        )
        os.replace(str(temporary), str(target))

    @staticmethod
    def _read_descriptor(path: Path) -> Optional[ReviewBaseline]:
        try:
            return ReviewBaseline.from_dict(
                json.loads(path.read_text(encoding="utf-8", errors="surrogateescape"))
            )
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


def _quoted_token_end(text: str) -> Optional[int]:
    """Index of the closing quote of a quoted token starting at position 0."""
    index = 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index
        index += 1
    return None


def _split_header_paths(header: str) -> Tuple[str, str]:
    """Recover the (old, new) paths a ``diff --git`` header names.

    A side whose pathname could not be written literally is a quoted token
    (see :func:`quote_diff_path`); a quoted token is self-delimiting, so it is
    read out directly and only the all-literal header needs the scan below.

    WHY the split point of an all-literal header is not simply the last (or
    first) ``" b/"``: literal paths may themselves contain that sequence, so a
    file named ``pkg b/generated.py`` yields ``a/pkg b/generated.py b/pkg
    b/generated.py`` and either end-anchored scan cuts inside a filename.
    Every candidate split is therefore considered and the one whose two sides
    agree wins — the only self-consistent reading, and the shape of every
    header except a rename. A rename is disambiguated by its own ``rename
    from``/``rename to`` lines (see :func:`split_diff_sections`), which carry
    one path each and need no splitting at all; the leftmost candidate is the
    fallback here so the a-side stays the shortest reading rather than
    swallowing the b-side.
    """
    rest = header[len(_DIFF_HEADER_PREFIX):].rstrip("\n")
    if not rest.startswith("a/"):
        return "", ""
    body = rest[2:]
    if body.startswith('"'):
        end = _quoted_token_end(body)
        if end is None or not body[end + 1:].startswith(" b/"):
            return "", ""
        old = unquote_diff_path(body[:end + 1])
        new = unquote_diff_path(body[end + 4:])
        return (old, new) if old and new else ("", "")
    # A literal a-side carries no quote character at all (that is precisely
    # what forces quoting), so the first ``" b/\""`` in it can only be the
    # separator ahead of a quoted b-side.
    marker = body.find(' b/"')
    if marker != -1:
        old, new = body[:marker], unquote_diff_path(body[marker + 3:])
        return (old, new) if old and new else ("", "")
    candidates: List[Tuple[str, str]] = []
    index = body.find(" b/")
    while index != -1:
        old, new = body[:index], body[index + 3:]
        if old and new:
            if old == new:
                return old, new
            candidates.append((old, new))
        index = body.find(" b/", index + 1)
    return candidates[0] if candidates else ("", "")


# Emitted by the renderer for an exact rename, one path per line, so they name
# the two sides unambiguously even when a path contains the header's own
# separator sequence.
_RENAME_FROM_PREFIX = "rename from "
_RENAME_TO_PREFIX = "rename to "


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
        # The rename lines win over the header split: they are unambiguous,
        # while the header packs both paths onto one line. Only the section
        # preamble is scanned, so a hunk's content can never be mistaken for
        # one (every body line carries a diff prefix, but the bound is free).
        old_name, new_name = old_path, new_path
        for line in current[1:]:
            if line.startswith("@@") or line.startswith("--- "):
                break
            if line.startswith(_RENAME_FROM_PREFIX):
                old_name = unquote_diff_path(
                    line[len(_RENAME_FROM_PREFIX):].rstrip("\n")
                )
            elif line.startswith(_RENAME_TO_PREFIX):
                new_name = unquote_diff_path(
                    line[len(_RENAME_TO_PREFIX):].rstrip("\n")
                )
        sections.append(
            DiffSection(path=new_name, old_path=old_name, text="".join(current))
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


def normalize_scope_path(value: str) -> str:
    """Reduce one path to the spelling the scope's changed-path table uses.

    WHY: that table holds repository-relative POSIX paths carrying no trailing
    separator and no redundant segments, so equivalent spellings of the same
    directory have to collapse onto that one spelling BEFORE any containment
    question is asked. Comparing raw strings instead made ``--path pkg/`` test
    for the prefix ``pkg//`` and refuse a directory the scope does hold.

    INVARIANT: a leading ``/`` survives normalization. An absolute path is not
    repository-relative, so it must keep naming nothing in this scope and be
    refused as out of scope, rather than be silently re-rooted onto a changed
    file that merely shares its tail.

    ``..`` segments are likewise left literal: resolving them here would let a
    filter climb out of the scope and land back inside it under a name the
    scope never held, which is the one-way containment rule below in disguise.

    INVARIANT: normalization touches SEPARATORS and dot segments only — never
    the characters of a name. Space is a legal filename character on every
    platform this runs on, so trimming surrounding whitespace would rewrite
    both sides of the containment question into paths the repository does not
    hold: ``--path pkg`` would be admitted against the changed file
    ``" pkg/mod.py"``, and a name ending in a space would select a different
    file than the one asked for. A filter that names no changed path must stay
    unmatched here so the command refuses it (exit 6).
    """
    text = str(value or "")
    if not text:
        return ""
    normalized = "/".join(
        segment for segment in text.split("/") if segment and segment != "."
    )
    return "/" + normalized if text.startswith("/") else normalized


def paths_related(candidate: str, requested: str) -> bool:
    """Whether *candidate* is the requested path itself or lives beneath it.

    INVARIANT: containment runs ONE way — a filter selects the changed paths at
    or under it, never the other way round. The reverse direction would admit a
    filter that names nothing: ``--path src/foo.py/not-real`` is not a subtree
    of the changed file ``src/foo.py``, it is a path that does not exist, and
    answering it with that file's diff reports changes the operator never asked
    for under a name the scope never held.

    INVARIANT: this is the predicate that decides ``--path`` ADMISSION, and it
    resolves the filter against the scope's changed-path table — the very set
    ``--stat`` renders and evidence validation grounds on. Both views resolve
    the filter through the same membership question, so neither can report "no
    changes" for a filter the other accepted. Normalization is applied HERE,
    on both sides, for the same reason: every caller — admission and selection
    alike — then decides containment on one spelling of each path instead of
    normalizing separately and drifting apart.
    """
    left = normalize_scope_path(candidate)
    right = normalize_scope_path(requested)
    if not left or not right:
        return False
    return left == right or left.startswith(right + "/")


def section_covers_path(section: DiffSection, path: str) -> bool:
    """Whether a section is AT or UNDER ``path``, on either side of a rename.

    Selection only, never admission (see ``paths_related``), but deliberately
    the SAME containment relation admission uses: the ``--stat`` table keeps
    the changed paths at or under the filter, so resolving sections through any
    wider relation would let the two views of one filter disagree about which
    files they cover.

    INVARIANT: containment does not run the other way — a section is never
    selected because the filter names something INSIDE it. A submodule's inner
    files used to reach the diff view only through that reverse direction, so
    an inner-path filter pulled in the whole parent gitlink section and with it
    every sibling inner path the section happened to render. Every inner label
    now carries its own ``diff --git`` header (degraded notes included, see
    ``_render_gitlink_diff``), so it splits out under its own name and the
    reverse direction buys nothing but that leak.
    """
    return any(
        paths_related(candidate, path)
        for candidate in (section.path, section.old_path)
        if candidate
    )


def select_filtered_view(
    sections: Sequence[DiffSection],
    table: Dict[str, Tuple[int, int]],
    requested: Sequence[str],
) -> Tuple[List[DiffSection], Dict[str, Tuple[int, int]]]:
    """Resolve one ``--path`` filter into the (sections, stat rows) it selects.

    INVARIANT: both views of a filter are resolved HERE, together, so they
    cover the same files by construction rather than by two containment scans
    that happen to agree. They do not always agree on their own: an exact
    rename is ONE change rendered as ONE section naming two paths, and the
    rendered text cannot be cut in half, so selecting it by either side selects
    the whole change — while a per-key scan of the stat table keeps only the
    named side's row. The section selection is therefore authoritative, and the
    table takes every path the selected sections name.

    The widening runs one way only: a section pulls in its own other side, it
    never pulls in a path it does not name, and a table row is never invented
    for a path the reconstruction did not record as changed.
    """
    selected = [
        section for section in sections
        if any(section_covers_path(section, path) for path in requested)
    ]
    keys = {
        key for key in table
        if any(paths_related(key, path) for path in requested)
    }
    for section in selected:
        for candidate in (section.path, section.old_path):
            if candidate in table:
                keys.add(candidate)
    return selected, {
        key: value for key, value in table.items() if key in keys
    }


def normalize_line_ranges(
    ranges: Optional[Sequence[Sequence[int]]],
) -> List[List[int]]:
    """Well-formed ``[start, end]`` pairs of an anchor list, sorted.

    Anchor lists are persisted state and survive a resume, so a pair that is
    unusable (non-integer, inverted) is dropped rather than repaired: evidence
    validation already treats such a pair as no line space at all, and a
    presentation layer that invented a range would show the checker an anchor
    it cannot cite.
    """
    clean: List[List[int]] = []
    for item in ranges or ():
        try:
            start, end = int(item[0]), int(item[1])
        except (IndexError, TypeError, ValueError):
            continue
        if end >= start:
            clean.append([start, end])
    clean.sort()
    return clean


def union_line_ranges(
    *groups: Optional[Sequence[Sequence[int]]],
) -> List[List[int]]:
    """Every line the given anchor lists cover, as merged ranges.

    Adjacent ranges are merged: two anchor sets rebuilt from different
    baselines can split one contiguous block at the seam between them, and a
    reader shown ``10-12, 13-20`` would read a boundary that does not exist.
    """
    merged: List[List[int]] = []
    for group in groups:
        merged.extend(normalize_line_ranges(group))
    merged.sort()
    result: List[List[int]] = []
    for start, end in merged:
        if result and start <= result[-1][1] + 1:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def subtract_line_ranges(
    outer: Optional[Sequence[Sequence[int]]],
    inner: Optional[Sequence[Sequence[int]]],
) -> List[List[int]]:
    """``outer`` minus every line ``inner`` covers, as merged ranges."""
    remaining = normalize_line_ranges(outer)
    for cut_start, cut_end in normalize_line_ranges(inner):
        carried: List[List[int]] = []
        for start, end in remaining:
            if cut_end < start or cut_start > end:
                carried.append([start, end])
                continue
            if start < cut_start:
                carried.append([start, cut_start - 1])
            if end > cut_end:
                carried.append([cut_end + 1, end])
        remaining = carried
    remaining.sort()
    return remaining


def intersect_line_ranges(
    left: Optional[Sequence[Sequence[int]]],
    right: Optional[Sequence[Sequence[int]]],
) -> List[List[int]]:
    """Only the lines BOTH anchor lists cover, as merged ranges.

    Used to clip an annotation domain down to the domain a round actually
    grounds evidence against: a presentation layer may narrate which slice of
    the citable line space a later step touched, but it must never advertise a
    line outside that space (see ``_format_scope_manifest``).
    """
    clipped: List[List[int]] = []
    rights = normalize_line_ranges(right)
    for start, end in normalize_line_ranges(left):
        for other_start, other_end in rights:
            low, high = max(start, other_start), min(end, other_end)
            if low <= high:
                clipped.append([low, high])
    return union_line_ranges(clipped)


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


def flow_snapshot_relpath(project_root: Any, flow_id: Any) -> Optional[str]:
    """Where one flow's baselines live, *relative* to *project_root*.

    For termination sites that must keep the snapshot store out of a copy of
    the project tree (the worktree archive) rather than only delete it in
    place: a best-effort :func:`discard_flow_snapshots` that failed would
    otherwise have the archive preserve the baselines for good.

    WHY relative: the consumer matches this against paths derived textually
    from its own (possibly unresolved) project path, which an absolute path
    resolved through a different symlink prefix would not match. Returns None
    when the id is not a safe single path segment — the same rule that stops
    a reclaim from walking out of the store.
    """
    try:
        if not _SAFE_FLOW_ID_RE.match(str(flow_id)):
            return None
        manager = ReviewScopeManager(Path(project_root), str(flow_id))
        return manager.root.relative_to(manager.project_root).as_posix()
    except Exception:  # noqa: BLE001 - a locating helper never fails a flow
        logger.debug(
            "Could not locate review baselines for flow %s", flow_id, exc_info=True
        )
        return None


def discard_flow_snapshots(project_root: Any, flow_id: Any) -> bool:
    """Reclaim one flow's review baselines from a flow-termination site.

    The shared entry point for every channel that disposes of a flow (the
    engine's completion landing, ``luo salvage``, ``luo end-session``), so all
    of them reclaim the same directory under the same safety rule.

    Total by contract: an unsafe id, an unreadable root, a permission error —
    none of them may propagate, because every caller is finishing a flow off
    and a failed reclaim is a disk-space problem, not a flow outcome. The
    return value says whether anything was actually removed.
    """
    try:
        manager = ReviewScopeManager(Path(project_root), str(flow_id))
        return manager.discard_snapshots()
    except Exception:  # noqa: BLE001 - reclaim is never worth failing a flow for
        logger.debug(
            "Could not reclaim review baselines for flow %s", flow_id, exc_info=True
        )
        return False
