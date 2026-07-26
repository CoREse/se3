"""Unit tests for the pure adjudication logic (fingerprints, ledger, triggers).

Co-located with ``adjudication.py`` per the charter's engine-internal test
exception: these cover private helpers and the mechanical trigger logic that is
tightly coupled to the ledger structure.
"""

from __future__ import annotations

from tianluo.engine import adjudication as adj


# --------------------------------------------------------------------------- #
# Issue builders
# --------------------------------------------------------------------------- #

def _issue(file="src/foo.py", line=42, quote="do the thing", expected="return None"):
    """Build a minimal self_check issue dict."""
    return {
        "severity": "high",
        "actual_behavior": "returns 0",
        "expected_behavior": expected,
        "divergence": "when x is None",
        "expectation_source": {"type": "task_description", "verbatim_quote": quote},
        "evidence_lines": [f"{file}:{line}"],
        "missing_in": [],
    }


# --------------------------------------------------------------------------- #
# Fingerprint / position key
# --------------------------------------------------------------------------- #

def test_fingerprint_stable_under_line_drift():
    """Same logical location keeps its fingerprint when the line number drifts."""
    a = _issue(line=42)
    b = _issue(line=118)  # code shifted after a fix iteration
    assert adj.fingerprint(a) == adj.fingerprint(b)
    assert adj.position_key(a) == adj.position_key(b)


def test_fingerprint_differs_on_expected():
    """Same position, opposing expectation ⇒ different fingerprint, same position."""
    a = _issue(expected="return None")
    b = _issue(expected="raise ValueError")
    assert adj.fingerprint(a) != adj.fingerprint(b)
    assert adj.position_key(a) == adj.position_key(b)


def test_normalization_matches_source_pool_semantics():
    """NFKC / smart quotes / whitespace / literal \\n normalize identically."""
    # Smart quotes + collapsed whitespace + literal \n vs. real formatting.
    a = _issue(quote="the  “quoted”\nvalue")
    b = _issue(quote='the "quoted" value')
    assert adj.position_key(a) == adj.position_key(b)


def test_missing_in_fallback_for_unimplemented():
    """A wholly-missing task grounds its file via missing_in, not evidence_lines."""
    issue = _issue()
    issue["evidence_lines"] = []
    issue["missing_in"] = ["src/bar.py"]
    assert adj.position_key(issue).startswith("src/bar.py")


# --------------------------------------------------------------------------- #
# Ledger read/write
# --------------------------------------------------------------------------- #

def test_empty_ledger_on_legacy_context():
    """A legacy engine.json without the ledger key tolerates as empty."""
    ctx = {}  # no adjudication_ledger key
    decision = adj.evaluate_triggers(ctx, [_issue()], fix_iteration=0)
    assert decision.triggered is False
    assert adj.LEDGER_KEY in ctx  # initialized lazily


def test_record_accumulates_across_rounds_only_grows():
    ctx = {}
    adj.record_self_check_round(ctx, [_issue()])
    adj.record_self_check_round(ctx, [_issue()])
    ledger = ctx[adj.LEDGER_KEY]
    assert ledger["round_count"] == 2
    assert len(ledger["observations"]) == 2  # one per round, never cleared


def test_record_dedupes_within_round():
    ctx = {}
    adj.record_self_check_round(ctx, [_issue(), _issue()])  # identical twice
    assert len(ctx[adj.LEDGER_KEY]["observations"]) == 1


def test_record_idempotent_on_round_id_replay():
    """A --resume replay of the same round_id does not double-count."""
    ctx = {}
    adj.record_self_check_round(ctx, [_issue()], round_id="r1")
    adj.record_self_check_round(ctx, [_issue()], round_id="r1")  # replay
    ledger = ctx[adj.LEDGER_KEY]
    assert ledger["round_count"] == 1
    assert len(ledger["observations"]) == 1


# --------------------------------------------------------------------------- #
# Trigger (a) candidate oscillation
# --------------------------------------------------------------------------- #

def test_trigger_a_oscillation_fires_on_flipped_expected():
    ctx = {}
    # Round 1: expects None at this position.
    adj.record_self_check_round(ctx, [_issue(expected="return None")])
    # Round 2: same position now expects the opposite.
    flip = _issue(expected="raise ValueError")
    adj.record_self_check_round(ctx, [flip])
    decision = adj.evaluate_triggers(ctx, [flip], fix_iteration=1)
    assert decision.triggered
    assert adj.REASON_OSCILLATION in decision.reasons


def test_trigger_a_no_fire_when_expected_stable():
    ctx = {}
    same = _issue(expected="return None")
    adj.record_self_check_round(ctx, [same])
    adj.record_self_check_round(ctx, [same])
    decision = adj.evaluate_triggers(ctx, [same], fix_iteration=1)
    assert adj.REASON_OSCILLATION not in decision.reasons


def test_trigger_a_no_fire_first_time_seen():
    ctx = {}
    issue = _issue()
    adj.record_self_check_round(ctx, [issue])
    decision = adj.evaluate_triggers(ctx, [issue], fix_iteration=0)
    assert adj.REASON_OSCILLATION not in decision.reasons


def test_trigger_a_only_structural_not_truth():
    """Oscillation is a structural signal only — suppress_convergence is set."""
    ctx = {}
    adj.record_self_check_round(ctx, [_issue(expected="A")])
    flip = _issue(expected="B")
    adj.record_self_check_round(ctx, [flip])
    decision = adj.evaluate_triggers(ctx, [flip], fix_iteration=1)
    assert decision.suppress_convergence is True


# --------------------------------------------------------------------------- #
# Trigger (b) contradiction / 打脸 — machine-side previous_issue_resolutions
# --------------------------------------------------------------------------- #

def test_trigger_b_contradiction_reads_previous_resolutions():
    ctx = {}
    original = _issue(expected="return None")
    adj.record_self_check_round(ctx, [original])
    # A fix round declares the original resolved ("fixed").
    adj.record_fix_resolutions(
        ctx, [{"status": "fixed", "issue": original}]
    )
    # New round re-opens the SAME position with the opposite expectation.
    reopened = _issue(expected="raise ValueError")
    adj.record_self_check_round(ctx, [reopened])
    decision = adj.evaluate_triggers(ctx, [reopened], fix_iteration=2)
    assert adj.REASON_CONTRADICTION in decision.reasons
    assert decision.suppress_convergence is True


def test_trigger_b_fires_when_same_fixed_fingerprint_reopens():
    """Re-reporting the exact fixed fingerprint is an immediate 打脸.

    A resolution declares an issue ``fixed`` yet the same round's self_check
    re-flags the identical path+quote+expected: the "fixed" claim and the live
    review are in direct conflict, so (b) must fire now rather than waiting for
    the slower reproduction threshold (c) to accumulate three post-fix recurrences.
    """
    ctx = {}
    original = _issue(expected="return None")
    adj.record_self_check_round(ctx, [original])
    adj.record_fix_resolutions(ctx, [{"status": "fixed", "issue": original}])
    adj.record_self_check_round(ctx, [original])  # same fingerprint again
    decision = adj.evaluate_triggers(ctx, [original], fix_iteration=2)
    assert adj.REASON_CONTRADICTION in decision.reasons
    assert decision.suppress_convergence is True


def test_trigger_b_ignores_still_present_resolutions():
    ctx = {}
    original = _issue(expected="return None")
    adj.record_self_check_round(ctx, [original])
    adj.record_fix_resolutions(
        ctx, [{"status": "still_present", "issue": original}]
    )
    reopened = _issue(expected="raise ValueError")
    adj.record_self_check_round(ctx, [reopened])
    decision = adj.evaluate_triggers(ctx, [reopened], fix_iteration=2)
    assert adj.REASON_CONTRADICTION not in decision.reasons


def test_trigger_b_accepts_inlined_resolution_fields():
    """Resolution may carry issue fields inline rather than a nested issue."""
    ctx = {}
    original = _issue(expected="return None")
    adj.record_self_check_round(ctx, [original])
    inlined = dict(original)
    inlined["status"] = "fixed"
    adj.record_fix_resolutions(ctx, [inlined])
    reopened = _issue(expected="raise ValueError")
    adj.record_self_check_round(ctx, [reopened])
    decision = adj.evaluate_triggers(ctx, [reopened], fix_iteration=2)
    assert adj.REASON_CONTRADICTION in decision.reasons


# --------------------------------------------------------------------------- #
# Trigger (c) reproduction
# --------------------------------------------------------------------------- #

def test_trigger_c_reproduction_third_appearance_after_fix():
    ctx = {}
    issue = _issue()
    adj.record_self_check_round(ctx, [issue])                      # round 0 (flagged)
    adj.record_fix_resolutions(ctx, [{"status": "fixed", "issue": issue}])
    adj.record_self_check_round(ctx, [issue])                      # post-fix #1
    adj.record_self_check_round(ctx, [issue])                      # post-fix #2
    # Only two genuine post-fix recurrences → below threshold, must not fire yet.
    decision = adj.evaluate_triggers(ctx, [issue], fix_iteration=3)
    assert adj.REASON_REPRODUCTION not in decision.reasons
    adj.record_self_check_round(ctx, [issue])                      # post-fix #3
    decision = adj.evaluate_triggers(ctx, [issue], fix_iteration=4)
    assert adj.REASON_REPRODUCTION in decision.reasons


def test_trigger_c_no_fire_before_third_or_without_fix():
    ctx = {}
    issue = _issue()
    adj.record_self_check_round(ctx, [issue])
    adj.record_self_check_round(ctx, [issue])  # 2nd, but never declared fixed
    decision = adj.evaluate_triggers(ctx, [issue], fix_iteration=2)
    assert adj.REASON_REPRODUCTION not in decision.reasons


def test_trigger_c_pre_fix_flags_do_not_inflate_count():
    """Flags BEFORE (and AT) the fix must not count toward the threshold.

    A fingerprint flagged twice, THEN declared fixed, THEN recurring twice is
    only two post-fix reproductions — not a third occurrence. Counting either the
    earlier pre-fix flag or the fixed flag itself would fire (c) up to two rounds
    early (the bug). Reproduction is scored STRICTLY AFTER the fixed appearance.
    """
    ctx = {}
    issue = _issue()
    adj.record_self_check_round(ctx, [issue])                      # round 0 (early flag)
    adj.record_self_check_round(ctx, [issue])                      # round 1 (flag that gets fixed)
    adj.record_fix_resolutions(ctx, [{"status": "fixed", "issue": issue}])  # fix_round 1
    adj.record_self_check_round(ctx, [issue])                      # round 2 (post-fix #1)
    adj.record_self_check_round(ctx, [issue])                      # round 3 (post-fix #2)
    # Only two post-fix recurrences (rounds 2, 3 > fix_round 1) → below 3.
    decision = adj.evaluate_triggers(ctx, [issue], fix_iteration=4)
    assert adj.REASON_REPRODUCTION not in decision.reasons
    # A third post-fix recurrence (round 4 > fix_round 1) finally fires.
    adj.record_self_check_round(ctx, [issue])                      # round 4 (post-fix #3)
    decision = adj.evaluate_triggers(ctx, [issue], fix_iteration=5)
    assert adj.REASON_REPRODUCTION in decision.reasons


def test_trigger_c_quoteless_fingerprint_never_fires():
    """A quoteless (regression-type) fingerprint must not fire (c).

    Its position collapses to per-file identity, which the adjudicator's
    position-based candidate/rejection machinery cannot close out — so firing
    (c) would re-trigger ADJUDICATE on every subsequent round the issue recurs.
    Excluded here, mirroring triggers (a)/(b).
    """
    ctx = {}
    issue = _issue(quote="")  # regression-type issue with empty verbatim_quote
    adj.record_self_check_round(ctx, [issue])                      # 1st
    adj.record_fix_resolutions(ctx, [{"status": "fixed", "issue": issue}])
    adj.record_self_check_round(ctx, [issue])                      # 2nd
    adj.record_self_check_round(ctx, [issue])                      # 3rd
    decision = adj.evaluate_triggers(ctx, [issue], fix_iteration=3)
    assert adj.REASON_REPRODUCTION not in decision.reasons


# --------------------------------------------------------------------------- #
# Periodic backstop
# --------------------------------------------------------------------------- #

def test_period_backstop_fires_every_n_iterations():
    ctx = {}
    novel = _issue(expected="stable")
    adj.record_self_check_round(ctx, [novel])
    # No structural signal, but fix_iteration reached the period.
    decision = adj.evaluate_triggers(ctx, [novel], fix_iteration=10, period_n=10)
    assert decision.triggered
    assert adj.REASON_PERIOD in decision.reasons
    # Periodic backstop alone must not suppress convergence.
    assert decision.suppress_convergence is False


def test_period_backstop_measures_from_baseline():
    ctx = {}
    novel = _issue(expected="stable")
    adj.record_self_check_round(ctx, [novel])
    # An adjudication just ran at iteration 10 → baseline reset.
    adj.note_adjudication_ran(ctx, fix_iteration=10)
    decision = adj.evaluate_triggers(ctx, [novel], fix_iteration=15, period_n=10)
    assert adj.REASON_PERIOD not in decision.reasons  # only 5 since baseline
    decision = adj.evaluate_triggers(ctx, [novel], fix_iteration=20, period_n=10)
    assert adj.REASON_PERIOD in decision.reasons


# --------------------------------------------------------------------------- #
# abolished marking
# --------------------------------------------------------------------------- #

def test_abolished_entries_excluded_from_triggers():
    ctx = {}
    # Build a would-be oscillation.
    adj.record_self_check_round(ctx, [_issue(expected="A")])
    flip = _issue(expected="B")
    adj.record_self_check_round(ctx, [flip])
    # Abolish both fingerprints at that position (clause was overridden).
    adj.mark_abolished(
        ctx,
        [adj.fingerprint(_issue(expected="A")), adj.fingerprint(flip)],
    )
    decision = adj.evaluate_triggers(ctx, [flip], fix_iteration=1)
    assert adj.REASON_OSCILLATION not in decision.reasons


def test_abolished_excludes_reproduction():
    ctx = {}
    issue = _issue()
    adj.record_self_check_round(ctx, [issue])                      # round 0 (flagged)
    adj.record_fix_resolutions(ctx, [{"status": "fixed", "issue": issue}])
    # Three genuine post-fix recurrences would fire (c) — abolish must veto it.
    adj.record_self_check_round(ctx, [issue])                      # post-fix #1
    adj.record_self_check_round(ctx, [issue])                      # post-fix #2
    adj.record_self_check_round(ctx, [issue])                      # post-fix #3
    adj.mark_abolished(ctx, [adj.fingerprint(issue)])
    decision = adj.evaluate_triggers(ctx, [issue], fix_iteration=4)
    assert adj.REASON_REPRODUCTION not in decision.reasons


def test_rejected_candidate_position_not_retriggered():
    ctx = {}
    adj.record_self_check_round(ctx, [_issue(expected="A")])
    flip = _issue(expected="B")
    adj.record_self_check_round(ctx, [flip])
    adj.record_rejected_candidates(ctx, [adj.position_key(flip)])
    decision = adj.evaluate_triggers(ctx, [flip], fix_iteration=1)
    assert adj.REASON_OSCILLATION not in decision.reasons


# --------------------------------------------------------------------------- #
# should_suppress_convergence
# --------------------------------------------------------------------------- #

def test_should_suppress_convergence_true_on_oscillation():
    ctx = {}
    adj.record_self_check_round(ctx, [_issue(expected="A")])
    flip = _issue(expected="B")
    adj.record_self_check_round(ctx, [flip])
    assert adj.should_suppress_convergence(ctx, [flip]) is True


def test_should_suppress_convergence_false_when_no_signal():
    ctx = {}
    stable = _issue(expected="A")
    adj.record_self_check_round(ctx, [stable])
    adj.record_self_check_round(ctx, [stable])
    assert adj.should_suppress_convergence(ctx, [stable]) is False


def test_should_suppress_convergence_true_on_due_periodic_backstop():
    """A due periodic backstop must suppress convergence too — otherwise the
    convergence shortcut short-circuits to COMPLETED before the state machine's
    REVISION_NEEDED branch ever evaluates the backstop, silently skipping the
    every-N-iteration safety net (issue: periodic backstop skipped under
    convergence)."""
    ctx = {}
    stable = _issue(expected="A")
    adj.record_self_check_round(ctx, [stable])
    adj.record_self_check_round(ctx, [stable])  # converged, no signal
    # Below the period: still allowed to converge.
    assert adj.should_suppress_convergence(
        ctx, [stable], fix_iteration=5, period_n=10
    ) is False
    # At the period edge: convergence must yield so ADJUDICATE can run.
    assert adj.should_suppress_convergence(
        ctx, [stable], fix_iteration=10, period_n=10
    ) is True


def test_ledger_survives_dict_round_trip():
    """The ledger is plain JSON-able dict content (State.from_dict continuity)."""
    import json

    ctx = {}
    adj.record_self_check_round(ctx, [_issue()])
    adj.record_fix_resolutions(ctx, [{"status": "fixed", "issue": _issue()}])
    restored = json.loads(json.dumps(ctx))
    # Triggers evaluate identically on the restored context.
    d1 = adj.evaluate_triggers(ctx, [_issue()], fix_iteration=0)
    d2 = adj.evaluate_triggers(restored, [_issue()], fix_iteration=0)
    assert d1.reasons == d2.reasons
