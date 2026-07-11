"""Unit matrix for the anchored-patch mechanical validator + applier.

These are pure functions in ``charter_freshness`` — no LLM, no I/O. They carry
the whole deletion-defence line for the charter auto-update: every removal must
quote the exact on-disk text, verbatim and unique, and the applier only ever
runs on a validated patch.
"""

from __future__ import annotations

from se3.engine.steps import charter_freshness as cf


CHARTER = (
    "# Charter\n\n"
    "## Purpose\n"
    "The alpha subsystem drives the widget loop.\n\n"
    "## Conventions\n"
    "- Use tabs everywhere.\n"
    "- Log via logging.\n"
)


# ---------------------------------------------------------------------------
# accept paths
# ---------------------------------------------------------------------------

def test_empty_patch_is_valid_and_candidate_equals_original():
    ok, reason = cf._validate_anchored_patch(CHARTER, [])
    assert ok is True
    assert reason == ""
    assert cf._apply_patch(CHARTER, []) == CHARTER


def test_unique_replace_generates_candidate():
    ops = [{
        "op": "replace",
        "old_text": "- Use tabs everywhere.",
        "new_text": "- Use 4-space indentation everywhere.",
    }]
    ok, reason = cf._validate_anchored_patch(CHARTER, ops)
    assert ok is True, reason
    candidate = cf._apply_patch(CHARTER, ops)
    assert "4-space indentation" in candidate
    assert "Use tabs everywhere" not in candidate
    # Everything else is untouched.
    assert "The alpha subsystem drives the widget loop." in candidate


def test_insert_after_pure_insertion():
    ops = [{
        "op": "insert_after",
        "anchor": "- Log via logging.\n",
        "new_text": "- Type-annotate with future annotations.\n",
    }]
    ok, reason = cf._validate_anchored_patch(CHARTER, ops)
    assert ok is True, reason
    candidate = cf._apply_patch(CHARTER, ops)
    assert candidate.startswith(CHARTER)  # pure append after the last line
    assert "future annotations" in candidate
    # No original text was removed.
    assert CHARTER in candidate


def test_multiple_nonoverlapping_ops_apply_in_one_pass():
    ops = [
        {"op": "replace", "old_text": "alpha subsystem", "new_text": "beta subsystem"},
        {"op": "insert_after", "anchor": "- Log via logging.\n",
         "new_text": "- Prefer pathlib.\n"},
    ]
    ok, reason = cf._validate_anchored_patch(CHARTER, ops)
    assert ok is True, reason
    candidate = cf._apply_patch(CHARTER, ops)
    assert "beta subsystem" in candidate
    assert "Prefer pathlib." in candidate
    assert "alpha subsystem" not in candidate


# ---------------------------------------------------------------------------
# reject paths — each returns a feedable reason
# ---------------------------------------------------------------------------

def test_replace_quote_not_on_disk_is_rejected():
    ops = [{"op": "replace", "old_text": "text that is not present", "new_text": "x"}]
    ok, reason = cf._validate_anchored_patch(CHARTER, ops)
    assert ok is False
    assert "old_text" in reason
    assert "verbatim" in reason


def test_replace_quote_matching_multiple_places_is_rejected():
    # "subsystem" appears once here, so craft a charter with two matches.
    text = "line subsystem one\nline subsystem two\n"
    ops = [{"op": "replace", "old_text": "subsystem", "new_text": "module"}]
    ok, reason = cf._validate_anchored_patch(text, ops)
    assert ok is False
    assert "unique" in reason


def test_insert_after_anchor_not_found_is_rejected():
    ops = [{"op": "insert_after", "anchor": "no such anchor", "new_text": "x"}]
    ok, reason = cf._validate_anchored_patch(CHARTER, ops)
    assert ok is False
    assert "anchor" in reason


def test_insert_after_ambiguous_anchor_is_rejected():
    text = "repeat\nrepeat\n"
    ops = [{"op": "insert_after", "anchor": "repeat", "new_text": "x"}]
    ok, reason = cf._validate_anchored_patch(text, ops)
    assert ok is False
    assert "unique" in reason


def test_unknown_op_kind_is_rejected():
    ops = [{"op": "delete", "old_text": "- Use tabs everywhere."}]
    ok, reason = cf._validate_anchored_patch(CHARTER, ops)
    assert ok is False
    assert "unknown op" in reason


def test_missing_old_text_on_replace_is_rejected():
    ops = [{"op": "replace", "new_text": "x"}]
    ok, reason = cf._validate_anchored_patch(CHARTER, ops)
    assert ok is False
    assert "old_text" in reason


def test_over_char_budget_is_rejected():
    big = "y" * (cf.MAX_PATCH_NEW_CHARS + 1)
    ops = [{"op": "insert_after", "anchor": "- Log via logging.\n", "new_text": big}]
    ok, reason = cf._validate_anchored_patch(CHARTER, ops)
    assert ok is False
    assert "too much text" in reason


def test_too_many_ops_is_rejected():
    ops = [
        {"op": "insert_after", "anchor": "- Log via logging.\n", "new_text": "x"}
        for _ in range(cf.MAX_PATCH_OPS + 1)
    ]
    ok, reason = cf._validate_anchored_patch(CHARTER, ops)
    assert ok is False
    assert "too many operations" in reason


def test_overlapping_ops_are_rejected():
    # A replace covering "Use tabs everywhere" and another replace covering the
    # nested substring "tabs" overlap the same region.
    ops = [
        {"op": "replace", "old_text": "Use tabs everywhere", "new_text": "A"},
        {"op": "replace", "old_text": "tabs", "new_text": "B"},
    ]
    ok, reason = cf._validate_anchored_patch(CHARTER, ops)
    assert ok is False
    assert "overlap" in reason


def test_patch_not_a_list_is_rejected():
    ok, reason = cf._validate_anchored_patch(CHARTER, {"op": "replace"})
    assert ok is False
    assert "list" in reason


def test_non_string_new_text_is_rejected():
    ops = [{"op": "insert_after", "anchor": "- Log via logging.\n", "new_text": 123}]
    ok, reason = cf._validate_anchored_patch(CHARTER, ops)
    assert ok is False
    assert "new_text" in reason
