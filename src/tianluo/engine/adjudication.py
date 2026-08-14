"""Pure logic for the luo fix-loop adjudication ("警察") mechanism.

This module holds the LLM-free core of the adjudication feature: fingerprint
normalization, the cross-round issue *ledger* persisted on ``flow.state.context``,
the three structural oscillation triggers, the periodic backstop, ``abolished``
marking, and the convergence-suppression decision. Everything here is a pure
function over a plain dict (the ledger sub-tree of ``flow.state.context``) so it
is trivially unit-testable and carries no dependency on the state machine, the
step handlers, or any LLM call.

Why a separate module rather than living inside ``steps/self_check.py`` or
``state_machine.py``: both of those call into this logic, and putting the shared
core here keeps the trigger judgement mechanical and side-effect-free while the
*truth* verdict (is a candidate oscillation a real contradiction?) is delegated
to the ADJUDICATE step's LLM. The split mirrors the project's "program drives,
LLM only fills the thinking gap" boundary.

Ledger lifecycle (distinct from ``self_check_deferred_issues``): the deferred
stash is a *single-round* structure that resets on every pass-#1 and is drained
into ``fix_instructions`` on REVISION_NEEDED. The ledger, by contrast, is a
*cross-round* structure that only ever grows (until the flow ends): every
SELF_CHECK round's issues and every fix round's ``previous_issue_resolutions``
verdicts are appended. Both merely share the "persist under ``context``"
mechanism; their lifetimes differ, so they use independent keys.

Fingerprint key = file path + normalized ``verbatim_quote`` + normalized
expected content. ``evidence_lines`` line numbers are deliberately *excluded*
from the key: they drift with every fix iteration, and including them would
prevent "the same logical location" from aligning across rounds (breaking
oscillation / reproduction detection). The path portion of ``evidence_lines``
is kept; only the trailing ``:N`` is dropped.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# The single key under ``flow.state.context`` that holds the ledger. Chosen to
# be distinct from ``self_check_deferred_issues`` so the two structures never
# collide despite sharing the context-persistence mechanism.
LEDGER_KEY = "adjudication_ledger"

# Field separator for composed keys. Uses ASCII unit/record separators which
# cannot appear in a file path or a normalized quote, so a key round-trips
# unambiguously through JSON persistence.
_KEY_SEP = "\x1f"

# Trigger reason identifiers (also the audit vocabulary surfaced to the
# ADJUDICATE step and tests).
REASON_OSCILLATION = "candidate_oscillation"  # trigger (a)
REASON_CONTRADICTION = "contradiction"        # trigger (b) — "打脸"
REASON_REPRODUCTION = "reproduction"          # trigger (c)
REASON_PERIOD = "period"                      # periodic backstop

# The structural fingerprint triggers (a)/(b)/(c). Distinguished from the
# periodic backstop because only these suppress the convergence shortcut — a
# periodic sweep must never silently kill a genuinely-converged flow.
_SIGNAL_REASONS = (REASON_OSCILLATION, REASON_CONTRADICTION, REASON_REPRODUCTION)

# Reproduction (c) fires when the SAME full fingerprint reappears for a third
# time after having been reported fixed at least once. Third appearance = the
# fix demonstrably did not stick twice over.
_REPRODUCTION_THRESHOLD = 3


def _normalize_for_quote_match(s: Any) -> str:
    """Symmetric normalization for fingerprint alignment.

    Intentionally mirrors ``steps/self_check._normalize_for_quote_match`` step
    for step (literal ``\\n`` → real newline FIRST, then NFKC, smart-quote
    replacement, whitespace collapse). It is duplicated here rather than
    imported to keep this pure module free of any import edge to the step
    layer (``self_check`` imports *this* module, so importing back would be a
    cycle). The two must stay in sync: the ledger key and the self_check
    source-pool check have to normalize identically or a fixed clause would
    fail to align with the issue that later re-cites it.
    """
    if not isinstance(s, str):
        return ""
    s = s.replace("\\n", "\n")
    s = unicodedata.normalize("NFKC", s)
    for smart, plain in (
        ("“", '"'), ("”", '"'),
        ("‘", "'"), ("’", "'"),
    ):
        s = s.replace(smart, plain)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def relaxed_quote_candidates(norm_quote: str) -> List[str]:
    """Relaxed substring-match variants of a normalized quote.

    Single source of truth for the quote-acceptance relaxations shared by
    ``self_check._validate_and_filter_issues`` (the source-pool check) and
    ``steps/adjudicate._positions_with_dead_clause`` (the dead-clause check).
    ``_format_task_groups`` renders each plan task as a prompt-visible line
    ``- [<id>] <description>`` and may cap an oversized description with a
    trailing ellipsis, so a reviewer copying the visible line carries a leading
    bullet/id prefix and/or a trailing ellipsis the raw pool lacks. Emitting the
    same relaxed candidates from both callers keeps abolition ("would a re-quote
    now fail validation?") aligned with what validation actually accepts — a
    clause self_check would still accept must never be judged dead.
    """
    if not norm_quote:
        return []
    candidates = [norm_quote]
    trimmed = norm_quote.rstrip(". ").rstrip()
    if trimmed and trimmed != norm_quote:
        candidates.append(trimmed)
    for c in list(candidates):
        unprefixed = re.sub(r"^-\s*(\[[^\]]*\]\s*)?", "", c)
        if unprefixed and unprefixed != c:
            candidates.append(unprefixed)
    return candidates


def _pk_quote(position_key_str: str) -> str:
    """Return the normalized-quote component of a ``position_key`` (or "")."""
    parts = position_key_str.split(_KEY_SEP)
    return parts[1] if len(parts) > 1 else ""


def _issue_file(issue: Dict[str, Any]) -> str:
    """Derive the fingerprint's file-path component from an issue dict.

    Prefers the path of the first ``evidence_lines`` entry (stripping the
    trailing ``:N`` line number, which is excluded from the key because it
    drifts across fix iterations); falls back to the first ``missing_in`` path
    for wholly-unimplemented findings that have no changed line to cite.
    """
    evidence = issue.get("evidence_lines") or []
    if isinstance(evidence, list):
        for entry in evidence:
            if isinstance(entry, str) and entry.strip():
                # Drop the ``:N`` suffix; keep everything before the last colon.
                return entry.rsplit(":", 1)[0] if ":" in entry else entry
    missing = issue.get("missing_in") or []
    if isinstance(missing, list):
        for entry in missing:
            if isinstance(entry, str) and entry.strip():
                return entry
    return ""


def _issue_quote(issue: Dict[str, Any]) -> str:
    source = issue.get("expectation_source") or {}
    if isinstance(source, dict):
        return _normalize_for_quote_match(source.get("verbatim_quote", ""))
    return ""


def _issue_expected(issue: Dict[str, Any]) -> str:
    return _normalize_for_quote_match(issue.get("expected_behavior", ""))


def position_key(issue: Dict[str, Any]) -> str:
    """Location identity: file path + normalized quote (expected excluded).

    Two issues sharing a position may still disagree on *what* the code should
    do — that disagreement is exactly the oscillation/contradiction signal, so
    the position key must NOT include expected content.
    """
    return _KEY_SEP.join((_issue_file(issue), _issue_quote(issue)))


def fingerprint(issue: Dict[str, Any]) -> str:
    """Full fingerprint: file path + normalized quote + normalized expected.

    Same position + same expected content ⇒ same fingerprint ⇒ the "same
    finding" for reproduction counting (trigger c). Differing expected at a
    shared position yields *different* fingerprints but the *same* position
    key — the basis for oscillation (a) and contradiction (b).
    """
    return _KEY_SEP.join(
        (_issue_file(issue), _issue_quote(issue), _issue_expected(issue))
    )


# --------------------------------------------------------------------------- #
# Ledger read/write
# --------------------------------------------------------------------------- #

def _ensure_ledger(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Return the ledger sub-tree of ``ctx``, initializing it if absent.

    Tolerates a legacy ``engine.json`` that predates the feature (no ledger
    key): such flows start from an empty ledger. Idempotent — re-invocation on
    an already-initialized ledger never resets accumulated observations.
    """
    ledger = ctx.get(LEDGER_KEY)
    if not isinstance(ledger, dict):
        ledger = {}
        ctx[LEDGER_KEY] = ledger
    # ``observations`` — one entry per (round, issue). ``resolutions`` — one per
    # fix-round previous_issue_resolution verdict. Both append-only.
    ledger.setdefault("observations", [])
    ledger.setdefault("resolutions", [])
    ledger.setdefault("round_count", 0)
    # Fix-iteration count at which the last periodic backstop fired; the period
    # trigger measures elapsed iterations from here, not from flow start.
    ledger.setdefault("period_baseline", 0)
    # Recorded round ids, so a --resume replay of the same round is idempotent.
    ledger.setdefault("recorded_rounds", [])
    # Position keys the ADJUDICATE LLM ruled to be NON-contradictions; excluded
    # from trigger (a) so a rejected candidate never re-fires forever.
    ledger.setdefault("rejected_positions", [])
    return ledger


def _observation_from_issue(issue: Dict[str, Any], round_index: int) -> Dict[str, Any]:
    return {
        "round": round_index,
        "file": _issue_file(issue),
        "quote_norm": _issue_quote(issue),
        "expected_norm": _issue_expected(issue),
        "position_key": position_key(issue),
        "fingerprint": fingerprint(issue),
        "abolished": False,
    }


def record_self_check_round(
    ctx: Dict[str, Any],
    issues: List[Dict[str, Any]],
    round_id: Optional[str] = None,
) -> int:
    """Append one SELF_CHECK round's issues to the ledger; return round index.

    A "round" is one SELF_CHECK execution (it accumulates across fix iterations
    and multi-pass chains). Each call assigns the next round index and appends
    an observation per issue. Within a round duplicate identical fingerprints
    collapse (an issue list rarely repeats itself, but we keep the ledger
    clean).

    ``round_id`` is an optional idempotency token: on a ``--resume`` replay the
    caller passes the same stable id it used originally, and the duplicate call
    is skipped so a replayed round is not double-counted (which would corrupt
    reproduction counting). Without an id every call is a fresh round.
    """
    ledger = _ensure_ledger(ctx)

    if round_id is not None and round_id in ledger["recorded_rounds"]:
        # Idempotent replay: already recorded, do nothing.
        return ledger["round_count"] - 1 if ledger["round_count"] else 0

    round_index = ledger["round_count"]
    seen_fps: set = set()
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        obs = _observation_from_issue(issue, round_index)
        if obs["fingerprint"] in seen_fps:
            continue
        seen_fps.add(obs["fingerprint"])
        ledger["observations"].append(obs)

    ledger["round_count"] = round_index + 1
    if round_id is not None:
        ledger["recorded_rounds"].append(round_id)
    return round_index


def record_fix_resolutions(
    ctx: Dict[str, Any],
    resolutions: List[Dict[str, Any]],
    round_id: Optional[str] = None,
) -> None:
    """Append a fix round's ``previous_issue_resolutions`` verdicts to the ledger.

    Each resolution is the LLM's verdict on a previously-reported issue. Because
    the raw ``previous_issue_resolutions`` schema only carries a prose paraphrase
    (``prev_issue_summary`` + ``status``) with no machine-readable identity, the
    caller (state_machine / self_check) pairs each verdict with the full previous
    issue it refers to and passes it here as ``issue`` (or inlines the issue
    fields directly). This module is the first production consumer of that
    array — trigger (b) reads these ``fixed`` verdicts back and compares them by
    fingerprint against the current round's issues.

    A resolution dict is accepted in either shape:
      - ``{"status": "fixed"|"still_present", "issue": {<full issue dict>}}``
      - ``{"status": ..., "file"/"expectation_source"/"expected_behavior": ...}``
      - ``{"status": ..., "fingerprint": ..., "position_key": ...}`` (pre-computed)
    Only ``status == "fixed"`` verdicts are actionable for trigger (b); others
    are still stored for audit but carry no contradiction weight.
    """
    ledger = _ensure_ledger(ctx)

    if round_id is not None and round_id in ledger.get("recorded_rounds", []):
        return

    for res in resolutions or []:
        if not isinstance(res, dict):
            continue
        status = res.get("status", "")
        # ``src`` is always a dict: either the paired previous-issue dict or the
        # resolution itself carrying inlined issue fields.
        src = res.get("issue") if isinstance(res.get("issue"), dict) else res
        fp = res.get("fingerprint") or fingerprint(src)
        pk = res.get("position_key") or position_key(src)
        # The round of the observation this resolution declares fixed — the
        # LATEST already-recorded observation of the fingerprint (resolutions are
        # recorded before the current round's issues per the data flow, so the
        # ledger holds every prior appearance). Reproduction (trigger c) counts
        # only appearances STRICTLY AFTER this round, so neither this fixed flag
        # nor any EARLIER pre-fix flag of the same fingerprint can inflate the
        # count and fire (c) rounds early. Absent any prior observation, anchor
        # to the current round.
        fix_round = None
        for obs in ledger["observations"]:
            if obs.get("fingerprint") == fp:
                r = obs.get("round")
                if isinstance(r, int) and (fix_round is None or r > fix_round):
                    fix_round = r
        if fix_round is None:
            fix_round = ledger["round_count"]
        ledger["resolutions"].append(
            {
                "status": status,
                "fingerprint": fp,
                "position_key": pk,
                "expected_norm": _issue_expected(src),
                "fix_round": fix_round,
                "abolished": False,
            }
        )


# --------------------------------------------------------------------------- #
# abolished marking + rejected-candidate recording
# --------------------------------------------------------------------------- #

def mark_abolished(ctx: Dict[str, Any], abolished_fingerprints: List[str]) -> int:
    """Flag ledger entries whose fingerprint is in ``abolished_fingerprints``.

    Entries are kept (audit trail) but flagged ``abolished=True`` so they no
    longer count toward triggers (a)/(b)/(c). Called after an adjudication
    overrides a task-description clause: issues grounded on the now-defunct
    clause must stop driving the fix loop. Returns the number of entries
    flagged.
    """
    ledger = _ensure_ledger(ctx)
    targets = set(abolished_fingerprints or [])
    if not targets:
        return 0
    count = 0
    for obs in ledger["observations"]:
        if not obs.get("abolished") and obs.get("fingerprint") in targets:
            obs["abolished"] = True
            count += 1
    for res in ledger["resolutions"]:
        if not res.get("abolished") and res.get("fingerprint") in targets:
            res["abolished"] = True
            count += 1
    return count


def record_rejected_candidates(ctx: Dict[str, Any], positions: List[str]) -> None:
    """Record position keys the ADJUDICATE LLM ruled to be non-contradictions.

    A rejected candidate position is excluded from trigger (a) thereafter, so a
    legitimately-oscillating-looking-but-benign location never re-fires the
    adjudicator forever.
    """
    ledger = _ensure_ledger(ctx)
    rejected = ledger["rejected_positions"]
    for pk in positions or []:
        if pk not in rejected:
            rejected.append(pk)


# --------------------------------------------------------------------------- #
# Trigger evaluation
# --------------------------------------------------------------------------- #

@dataclass
class AdjudicationDecision:
    """Structural verdict of the trigger layer (no truth judgement).

    ``triggered`` says whether the ADJUDICATE step should run this round.
    ``reasons`` lists which triggers fired. ``suppress_convergence`` is True
    whenever any *signal* trigger (a/b/c) fired — the periodic backstop alone
    does not suppress convergence.
    """

    triggered: bool = False
    reasons: List[str] = field(default_factory=list)
    triggering_positions: List[str] = field(default_factory=list)
    triggering_fingerprints: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def suppress_convergence(self) -> bool:
        return any(r in _SIGNAL_REASONS for r in self.reasons)


def _active_observations(ledger: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Observations that still count (not abolished)."""
    return [o for o in ledger["observations"] if not o.get("abolished")]


def _current_round_index(ledger: Dict[str, Any]) -> int:
    """Index of the most recently recorded round (the "current" round).

    ``record_self_check_round`` is called before ``evaluate_triggers`` per the
    data flow, so the current round is ``round_count - 1``.
    """
    return ledger["round_count"] - 1 if ledger["round_count"] else 0


def _detect_oscillation(
    ledger: Dict[str, Any], current_pos: Dict[str, str]
) -> List[str]:
    """Trigger (a): same position flagged across rounds with differing expected.

    ``current_pos`` maps position_key → this-round expected_norm. A position
    oscillates when it was also flagged in some *prior* round with an expected
    that, normalized, is materially different from this round's. Rejected
    (adjudicator-cleared) positions and quoteless positions are skipped.
    """
    rejected = set(ledger.get("rejected_positions", []))
    current_round = _current_round_index(ledger)
    active = _active_observations(ledger)
    hits: List[str] = []
    for pk, cur_expected in current_pos.items():
        if pk in rejected:
            continue
        # A quoteless position collapses to per-file identity (all quoteless
        # regressions in one file share ``file\x1f``), so unrelated findings
        # would spuriously look like an A/B flip. Only quote-anchored positions
        # have a stable enough identity to trust the oscillation signal.
        if not _pk_quote(pk):
            continue
        # Prior-round expected values seen at this position.
        prior_expected = {
            o["expected_norm"]
            for o in active
            if o["position_key"] == pk and o["round"] < current_round
        }
        if not prior_expected:
            continue  # never seen before this round → not (yet) oscillating
        # Materially different = SOME prior round demanded an expected other than
        # this round's. Using set-difference (rather than "cur not in the union")
        # keeps a genuine cross-round A/B flip detectable even when one earlier
        # round self-contradicted (carried both A and B at this position): that
        # round contributes both to ``prior_expected``, and ``prior - {cur}`` is
        # still non-empty, so the flip fires instead of being masked by the union.
        if prior_expected - {cur_expected}:
            hits.append(pk)
    return hits


def _detect_contradiction(
    ledger: Dict[str, Any], current_pos: Dict[str, str], current_fps: set
) -> List[str]:
    """Trigger (b) "打脸": current issue re-opens a previously-fixed position.

    Reads the machine-side ``previous_issue_resolutions`` (via the ``fixed``
    resolutions recorded on the ledger). A contradiction is ANY current issue at
    a position that carries a ``fixed`` resolution — two shapes, both 打脸:

    * the same fingerprint that was declared fixed re-appears in this round's
      issues (you said it was resolved, yet self_check re-flags the identical
      path+quote+expected), or
    * a *different* fingerprint appears at that position (the location now
      demands the opposite of what was declared resolved).

    Both mean the "fixed" claim and the live review are in direct conflict, so
    the mere presence of a current fingerprint at a fixed position fires (b)
    immediately — waiting for the slower reproduction threshold (c) would miss
    the required same-fingerprint fixed-resolution signal.
    """
    # Positions that carry a live (non-abolished) "fixed" resolution. Only the
    # position matters now — any current fingerprint landing on one is 打脸, so
    # the specific fixed fingerprint no longer needs to be retained.
    fixed_positions = {
        res["position_key"]
        for res in ledger["resolutions"]
        if not res.get("abolished") and res.get("status") == "fixed"
    }

    # A position the adjudicator already ruled benign must never re-fire ANY
    # trigger (including 打脸): otherwise a review_divergence ruling — which
    # abolishes nothing — leaves the ``fixed`` resolution live and (b) re-fires
    # every round, livelocking adjudicate↔self_check. Quoteless positions share a
    # per-file identity, so they cannot be trusted for the position match either.
    rejected = set(ledger.get("rejected_positions", []))
    hits: List[str] = []
    for pk, _cur_expected in current_pos.items():
        if pk in rejected or not _pk_quote(pk):
            continue
        if pk not in fixed_positions:
            continue
        # ANY current fingerprint at a position that was declared fixed is 打脸:
        # a matching fingerprint means the "fixed" issue is re-flagged verbatim,
        # a differing one means the location now demands the opposite. Either way
        # the resolved claim and the live review conflict, so fire immediately
        # instead of set-differencing (which would drop the same-fingerprint case).
        if any(fp.rsplit(_KEY_SEP, 1)[0] == pk for fp in current_fps):
            hits.append(pk)
    return hits


def _detect_reproduction(
    ledger: Dict[str, Any], current_fps: set
) -> List[str]:
    """Trigger (c): the same full fingerprint reappears a third time after a fix.

    Counts only the non-abolished flag occurrences that fall STRICTLY AFTER the
    round the fingerprint was declared ``fixed``; fires when that post-fix count
    reaches ``_REPRODUCTION_THRESHOLD`` (3). The fixed observation itself is the
    pre-fix flag, not a post-fix occurrence, so it (and any earlier flag) is
    excluded — a fingerprint flagged twice, THEN fixed, THEN recurring twice is
    only two post-fix reproductions, not a third occurrence; counting the fixed
    flag would fire (c) one round early.
    """
    # Earliest round each fingerprint was declared fixed. Reproduction is scored
    # from the FIRST fix forward but excludes the fixed round itself, so a
    # fingerprint fixed early and repeatedly re-opened accrues only its genuine
    # post-fix recurrences. Missing ``fix_round`` (legacy ledgers) falls back to 0.
    fixed_round_by_fp: Dict[str, int] = {}
    for res in ledger["resolutions"]:
        if res.get("abolished") or res.get("status") != "fixed":
            continue
        fp = res["fingerprint"]
        fr = res.get("fix_round", 0)
        if not isinstance(fr, int):
            fr = 0
        if fp not in fixed_round_by_fp or fr < fixed_round_by_fp[fp]:
            fixed_round_by_fp[fp] = fr

    counts: Dict[str, int] = {}
    for obs in _active_observations(ledger):
        fp = obs["fingerprint"]
        if fp not in fixed_round_by_fp:
            continue
        r = obs.get("round")
        if isinstance(r, int) and r > fixed_round_by_fp[fp]:
            counts[fp] = counts.get(fp, 0) + 1

    # A fingerprint whose position the adjudicator ruled benign must not re-fire
    # (c) either — same livelock hazard as (b). ``fingerprint`` = ``position_key``
    # + expected, so its position is the fingerprint minus the trailing field.
    rejected = set(ledger.get("rejected_positions", []))
    hits: List[str] = []
    for fp in current_fps:
        pos = fp.rsplit(_KEY_SEP, 1)[0]
        if pos in rejected:
            continue
        # Skip quoteless fingerprints, mirroring (a)/(b). A quoteless position
        # collapses to per-file identity, which the adjudicator machinery
        # (candidate presentation, rejection, abolition) is all position-based
        # and cannot properly close: the candidate scan (_candidate_positions)
        # drops quoteless positions, so the LLM never rules on one, never records
        # it rejected, and the ``pos in rejected`` guard above can never match —
        # leaving (c) to re-fire on every subsequent round the issue recurs. Not
        # trusting the quoteless signal here is the consistent, loop-free choice.
        if not _pk_quote(pos):
            continue
        if fp in fixed_round_by_fp and counts.get(fp, 0) >= _REPRODUCTION_THRESHOLD:
            hits.append(fp)
    return hits


def evaluate_triggers(
    ctx: Dict[str, Any],
    current_issues: List[Dict[str, Any]],
    fix_iteration: int,
    period_n: int = 10,
) -> AdjudicationDecision:
    """Evaluate all four triggers and aggregate into an ``AdjudicationDecision``.

    Pure structural judgement only — whether a candidate oscillation is a *real*
    contradiction is left to the ADJUDICATE step's LLM. Assumes the current
    round's issues were already recorded via ``record_self_check_round`` (per
    the data flow), so the ledger reflects history including this round.

    ``period_n`` is the periodic backstop: force an adjudication every N fix
    iterations to catch oscillations the structural signals missed. The backstop
    fires when ``fix_iteration - period_baseline >= period_n``.
    """
    ledger = _ensure_ledger(ctx)
    decision = AdjudicationDecision()

    # Current round's active (non-abolished) positions/fingerprints, taken from
    # the freshly-supplied issues rather than the ledger so evaluation is robust
    # to recording order.
    current_pos: Dict[str, str] = {}
    current_fps: set = set()
    for issue in current_issues or []:
        if not isinstance(issue, dict):
            continue
        fp = fingerprint(issue)
        pk = position_key(issue)
        current_fps.add(fp)
        current_pos[pk] = _issue_expected(issue)

    osc = _detect_oscillation(ledger, current_pos)
    contra = _detect_contradiction(ledger, current_pos, current_fps)
    repro = _detect_reproduction(ledger, current_fps)

    if osc:
        decision.reasons.append(REASON_OSCILLATION)
        decision.details[REASON_OSCILLATION] = osc
        decision.triggering_positions.extend(osc)
    if contra:
        decision.reasons.append(REASON_CONTRADICTION)
        decision.details[REASON_CONTRADICTION] = contra
        decision.triggering_positions.extend(contra)
    if repro:
        decision.reasons.append(REASON_REPRODUCTION)
        decision.details[REASON_REPRODUCTION] = repro
        decision.triggering_fingerprints.extend(repro)

    # Periodic backstop — force a sweep every N fix iterations regardless of
    # structural signals. Does not, on its own, suppress convergence.
    if period_n and fix_iteration - ledger.get("period_baseline", 0) >= period_n:
        decision.reasons.append(REASON_PERIOD)
        decision.details[REASON_PERIOD] = {
            "fix_iteration": fix_iteration,
            "period_baseline": ledger.get("period_baseline", 0),
            "period_n": period_n,
        }

    decision.triggered = bool(decision.reasons)
    return decision


def note_adjudication_ran(ctx: Dict[str, Any], fix_iteration: int) -> None:
    """Reset the periodic-backstop baseline after an adjudication runs.

    Anchors the next periodic sweep to the current fix iteration so the backstop
    measures *elapsed* iterations since the last adjudication, not since flow
    start.
    """
    ledger = _ensure_ledger(ctx)
    ledger["period_baseline"] = fix_iteration
