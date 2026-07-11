"""Tests for the why-comment deletion guard in invariant_check (缺口二).

The invariant_check step's anchored audit catches "delete a comment AND violate
it" (the original quote survives via the baseline harvest). It did NOT catch a
diff that *silently deletes or rewrites* a why-comment without violating it — the
knowledge just evaporates. This module covers the two-channel guard that closes
that gap:

- **hard guard** — a deleted/rewritten comment tagged ``WHY:``/``INVARIANT:``
  synthesizes an anchored issue and routes to REVISION_NEEDED;
- **advisory** — every other comment deletion goes through a single LLM triage
  and lands in ``step.outputs['why_comment_losses']`` ONLY, never the fix loop;

plus the mechanical set-diff helper and the constitutional-amendment prompt
exemption clause.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from se3.engine.steps import invariant_check
from se3.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_recursion_guard(monkeypatch):
    """Clear ``SE3_TEST_RUNNING`` so nothing this module drives is skipped.

    The full suite runs under the ``se3 test`` step, which exports
    ``SE3_TEST_RUNNING=1`` before spawning pytest; leaving it set can make se3's
    own recursion-guarded command paths short-circuit inside a test.
    """
    monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo_with(tmp_path: Path, rel: str, content: str) -> str:
    """Create ``rel`` with ``content``, commit it, return the baseline commit sha."""
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(["init"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    _git(["add", rel], tmp_path)
    _git(["commit", "-m", "baseline"], tmp_path)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()


def _make_flow(project_root: Path, task: str = "Implement the widget") -> FlowInstance:
    flow = FlowInstance(
        task_description=task,
        task_type="feature",
        status=FlowStatus.INIT,
    )
    flow.change_path = project_root / "change"
    return flow


def _make_step(inputs: dict) -> Step:
    return Step(step_type=StepType.INVARIANT_CHECK, inputs=inputs)


def _install_fake_caller(monkeypatch, responses):
    """Replace ``invariant_check.LLMCaller`` with a scripted fake."""
    state = {"prompts": [], "responses": list(responses), "calls": 0}

    class FakeCaller:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, prompt, **kwargs):
            state["calls"] += 1
            state["prompts"].append(prompt)
            return state["responses"].pop(0)

    monkeypatch.setattr(invariant_check, "LLMCaller", FakeCaller)
    return state


CHARTER_TEXT = (
    "# Demo — Charter\n\n## Purpose\nDemo project.\n\n"
    "### Requirement: Coding Conventions\n"
    "- Python 代码遵循标准 PEP 8。\n"
)


# ---------------------------------------------------------------------------
# (1) mechanical set-diff helper — pure unit tests
# ---------------------------------------------------------------------------

def test_helper_reports_deleted_comment(tmp_path):
    baseline = _init_repo_with(
        tmp_path, "src/a.py",
        "# WHY: keep this rationale\ndef f():\n    return 1\n",
    )
    # working tree drops the comment
    (tmp_path / "src/a.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    out = invariant_check._deleted_comments_by_file(
        tmp_path, {"src/a.py"}, baseline
    )
    assert out == {"src/a.py": ["WHY: keep this rationale"]}


def test_helper_reports_rewritten_comment(tmp_path):
    """A rewrite makes the OLD body vanish → it counts as deleted."""
    baseline = _init_repo_with(
        tmp_path, "src/a.py",
        "# INVARIANT: balance must stay >= 0\ndef f():\n    return 1\n",
    )
    (tmp_path / "src/a.py").write_text(
        "# INVARIANT: balance must stay non-negative\ndef f():\n    return 1\n",
        encoding="utf-8",
    )

    out = invariant_check._deleted_comments_by_file(
        tmp_path, {"src/a.py"}, baseline
    )
    assert out == {"src/a.py": ["INVARIANT: balance must stay >= 0"]}


def test_helper_skips_new_file(tmp_path):
    """A file absent at baseline lost nothing → skipped silently."""
    baseline = _init_repo_with(
        tmp_path, "src/a.py", "# WHY: keep\ndef f():\n    return 1\n"
    )
    # brand-new file, never in baseline
    (tmp_path / "src/new.py").write_text("def g():\n    return 2\n", encoding="utf-8")

    out = invariant_check._deleted_comments_by_file(
        tmp_path, {"src/new.py"}, baseline
    )
    assert out == {}


def test_helper_no_baseline_returns_empty(tmp_path):
    out = invariant_check._deleted_comments_by_file(
        tmp_path, {"src/a.py"}, None
    )
    assert out == {}


def test_helper_deleted_file_loses_all_comments(tmp_path):
    """A file deleted outright loses every comment it carried at baseline."""
    baseline = _init_repo_with(
        tmp_path, "src/a.py",
        "# WHY: one\n# WHY: two\ndef f():\n    return 1\n",
    )
    (tmp_path / "src/a.py").unlink()

    out = invariant_check._deleted_comments_by_file(
        tmp_path, {"src/a.py"}, baseline
    )
    assert out == {"src/a.py": ["WHY: one", "WHY: two"]}


def test_helper_unchanged_comment_not_reported(tmp_path):
    baseline = _init_repo_with(
        tmp_path, "src/a.py",
        "# WHY: keep this\ndef f():\n    return 1\n",
    )
    # comment stays; only the body changed
    (tmp_path / "src/a.py").write_text(
        "# WHY: keep this\ndef f():\n    return 99\n", encoding="utf-8"
    )

    out = invariant_check._deleted_comments_by_file(
        tmp_path, {"src/a.py"}, baseline
    )
    assert out == {}


# ---------------------------------------------------------------------------
# (2) hard guard — WHY:/INVARIANT: prefix deletions route to REVISION_NEEDED
# ---------------------------------------------------------------------------

def test_marked_comment_deletion_triggers_revision(tmp_path, monkeypatch):
    baseline = _init_repo_with(
        tmp_path, "src/ledger.py",
        "# WHY: account balance must never go negative\n"
        "def debit(x):\n    return x\n",
    )
    # the diff deletes the marked comment (without violating it)
    (tmp_path / "src/ledger.py").write_text(
        "def debit(x):\n    return x\n", encoding="utf-8"
    )

    # main invariant audit finds nothing; no advisory call (only marked deletion).
    state = _install_fake_caller(monkeypatch, [json.dumps({"issues": [], "summary": "ok"})])
    flow = _make_flow(tmp_path, task="")
    flow.baseline_commit = baseline
    step = _make_step({
        "task_description": "",
        "charter": "",
        "changes_made": {"files_changed": ["src/ledger.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.REVISION_NEEDED
    assert state["calls"] == 1  # only the main audit; advisory skipped
    hard = step.outputs["why_comment_hard_violations"]
    assert len(hard) == 1
    # The synthesized issue anchors on the deleted comment body itself.
    assert hard[0]["expectation_source"]["type"] == "why_comment"
    assert hard[0]["expectation_source"]["verbatim_quote"] == (
        "WHY: account balance must never go negative"
    )
    assert step.outputs["actionable_count"] == 1
    assert step.outputs["fix_needed"] is True
    # The fix instruction surfaces the two accepted exits (restore / redeclare).
    detail = " ".join(
        i["expected_behavior"] for i in step.outputs["issues"]
    ).lower()
    assert "restore" in detail and "updated why:/invariant:" in detail


def test_restored_marked_comment_passes(tmp_path, monkeypatch):
    """If the comment is still present in the working tree, nothing is lost."""
    baseline = _init_repo_with(
        tmp_path, "src/ledger.py",
        "# INVARIANT: account balance must never go negative\n"
        "def debit(x):\n    return x\n",
    )
    # comment retained; only code changed
    (tmp_path / "src/ledger.py").write_text(
        "# INVARIANT: account balance must never go negative\n"
        "def debit(x):\n    return x - 0\n",
        encoding="utf-8",
    )

    state = _install_fake_caller(monkeypatch, [json.dumps({"issues": [], "summary": "ok"})])
    flow = _make_flow(tmp_path, task="")
    flow.baseline_commit = baseline
    step = _make_step({
        "task_description": "",
        "charter": "",
        "changes_made": {"files_changed": ["src/ledger.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert step.outputs["why_comment_hard_violations"] == []
    assert step.outputs["actionable_count"] == 0


def test_rewritten_marked_comment_with_updated_rationale_passes(tmp_path, monkeypatch):
    """Rewriting a marked comment into a NEW marked comment is a declared update.

    The old body vanishes (so ``_deleted_comments_by_file`` reports it), but the
    working tree now carries an updated WHY:/INVARIANT: comment declaring the new
    reason — the second accepted exit. This must NOT block the flow.
    """
    baseline = _init_repo_with(
        tmp_path, "src/proto.py",
        "# WHY: cache old protocol decision\ndef f():\n    return 1\n",
    )
    # rewrite the marked comment, explicitly re-declaring the updated rationale
    (tmp_path / "src/proto.py").write_text(
        "# WHY: cache new protocol decision after API migration\n"
        "def f():\n    return 1\n",
        encoding="utf-8",
    )

    # only the main audit runs; no advisory (the sole deletion is marked, and it
    # is suppressed by the replacement, so nothing routes to the advisory triage).
    state = _install_fake_caller(monkeypatch, [json.dumps({"issues": [], "summary": "ok"})])
    flow = _make_flow(tmp_path, task="")
    flow.baseline_commit = baseline
    step = _make_step({
        "task_description": "",
        "charter": "",
        "changes_made": {"files_changed": ["src/proto.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert step.outputs["why_comment_hard_violations"] == []
    assert step.outputs["actionable_count"] == 0


def test_rewritten_marked_comment_with_wholly_new_rationale_passes(tmp_path, monkeypatch):
    """A rewrite that records a COMPLETELY new reason for the same code is a
    declared update, not a silent loss.

    Regression for the token-overlap heuristic: replacing
    ``# WHY: use SQLite because deployment is single-node`` with
    ``# WHY: use Postgres because HA now requires shared storage`` shares almost no
    words, yet it explicitly re-declares intent over the SAME code line, so the
    code-slot correlation must recognise it as a rewrite and NOT block the flow.
    """
    baseline = _init_repo_with(
        tmp_path, "src/db.py",
        "# WHY: use SQLite because deployment is single-node\n"
        "engine = make_engine()\n",
    )
    (tmp_path / "src/db.py").write_text(
        "# WHY: use Postgres because HA now requires shared storage\n"
        "engine = make_engine()\n",
        encoding="utf-8",
    )

    state = _install_fake_caller(monkeypatch, [json.dumps({"issues": [], "summary": "ok"})])
    flow = _make_flow(tmp_path, task="")
    flow.baseline_commit = baseline
    step = _make_step({
        "task_description": "",
        "charter": "",
        "changes_made": {"files_changed": ["src/db.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert step.outputs["why_comment_hard_violations"] == []
    assert step.outputs["actionable_count"] == 0


def test_same_file_marked_addition_covers_deletion_one_to_one(tmp_path, monkeypatch):
    """A same-file marked addition covers a marked deletion one-to-one (net-count).

    The hard guard's authoritative rule is a purely mechanical 1:1 pairing: it
    only guarantees marked comments suffer no NET silent loss. Deleting
    ``# WHY: enforce tenant isolation`` while adding a NEW marked comment in the
    same file leaves the marked-comment count intact, so the guard does NOT block —
    whether the new reason is a semantically adequate replacement is the LLM main
    audit's job, not the hard guard's. Net LOSS (more deletions than new marked
    comments) is still caught — see ``test_partial_rewrite_in_shared_slot`` and
    ``test_sibling_marked_comment_in_same_slot_does_not_swallow_deletion``.
    """
    baseline = _init_repo_with(
        tmp_path, "src/multi.py",
        "# WHY: enforce tenant isolation\n"
        "def a():\n    return 1\n"
        "def b():\n    return 2\n",
    )
    # drop the protected comment; add a NEW marked comment elsewhere (1 for 1).
    (tmp_path / "src/multi.py").write_text(
        "def a():\n    return 1\n"
        "# WHY: cache parsed config\n"
        "def b():\n    return 2\n",
        encoding="utf-8",
    )

    state = _install_fake_caller(monkeypatch, [json.dumps({"issues": [], "summary": "ok"})])
    flow = _make_flow(tmp_path, task="")
    flow.baseline_commit = baseline
    step = _make_step({
        "task_description": "",
        "charter": "",
        "changes_made": {"files_changed": ["src/multi.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert step.outputs["why_comment_hard_violations"] == []
    assert invariant_check._rewritten_marked_exemptions(
        tmp_path, {"src/multi.py"}, baseline
    ) == {"src/multi.py": Counter({"WHY: enforce tenant isolation": 1})}


def test_sibling_marked_comment_in_same_slot_does_not_swallow_deletion(tmp_path, monkeypatch):
    """Two protected comments over the same code, one dropped, one kept verbatim.

    Regression for the per-slot exemption bug: the working tree kept
    ``# WHY: keep audit trail mandatory`` (a verbatim survivor, NOT a new rewrite)
    over the same ``def handle():``. The mere presence of a marked comment in that
    shared slot must not exempt the genuinely-deleted
    ``# WHY: enforce tenant isolation`` — it is a distinct intent that vanished, so
    it must route to REVISION_NEEDED.
    """
    baseline = _init_repo_with(
        tmp_path, "src/svc.py",
        "# WHY: enforce tenant isolation\n"
        "# WHY: keep audit trail mandatory\n"
        "def handle():\n    return 1\n",
    )
    # drop the tenant-isolation comment; keep the audit-trail one verbatim.
    (tmp_path / "src/svc.py").write_text(
        "# WHY: keep audit trail mandatory\n"
        "def handle():\n    return 1\n",
        encoding="utf-8",
    )

    state = _install_fake_caller(monkeypatch, [json.dumps({"issues": [], "summary": "ok"})])
    flow = _make_flow(tmp_path, task="")
    flow.baseline_commit = baseline
    step = _make_step({
        "task_description": "",
        "charter": "",
        "changes_made": {"files_changed": ["src/svc.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.REVISION_NEEDED
    hard = step.outputs["why_comment_hard_violations"]
    assert len(hard) == 1
    assert hard[0]["expectation_source"]["verbatim_quote"] == "WHY: enforce tenant isolation"

    # And the exemption helper alone must not list the dropped sibling.
    exemptions = invariant_check._rewritten_marked_exemptions(
        tmp_path, {"src/svc.py"}, baseline
    )
    assert exemptions == {}


def test_both_siblings_rewritten_in_same_slot_are_exempt(tmp_path):
    """When BOTH protected comments over the same code are genuinely rewritten,
    each gained its own replacement, so both old bodies are exempt."""
    baseline = _init_repo_with(
        tmp_path, "src/svc.py",
        "# WHY: enforce tenant isolation\n"
        "# WHY: keep audit trail mandatory\n"
        "def handle():\n    return 1\n",
    )
    (tmp_path / "src/svc.py").write_text(
        "# WHY: enforce strict tenant isolation across shards\n"
        "# WHY: retain the immutable audit log for compliance\n"
        "def handle():\n    return 1\n",
        encoding="utf-8",
    )

    out = invariant_check._rewritten_marked_exemptions(
        tmp_path, {"src/svc.py"}, baseline
    )
    assert out == {
        "src/svc.py": Counter({
            "WHY: enforce tenant isolation": 1,
            "WHY: keep audit trail mandatory": 1,
        })
    }


def test_partial_rewrite_in_shared_slot_guards_all_dropped(tmp_path):
    """Two dropped protected comments but only ONE new marked comment.

    One-to-one pairing: the single new marked comment exempts the deletion it is
    most similar to (``enforce tenant isolation``), and the unpaired deletion
    (``keep audit trail mandatory``) stays guarded — a net loss is never silently
    swallowed."""
    baseline = _init_repo_with(
        tmp_path, "src/svc.py",
        "# WHY: enforce tenant isolation\n"
        "# WHY: keep audit trail mandatory\n"
        "def handle():\n    return 1\n",
    )
    # both old bodies gone, only ONE new marked comment took their place.
    (tmp_path / "src/svc.py").write_text(
        "# WHY: enforce strict tenant isolation across shards\n"
        "def handle():\n    return 1\n",
        encoding="utf-8",
    )

    out = invariant_check._rewritten_marked_exemptions(
        tmp_path, {"src/svc.py"}, baseline
    )
    assert out == {"src/svc.py": Counter({"WHY: enforce tenant isolation": 1})}


def test_rewritten_marked_exemptions_pairs_rewrite_in_place(tmp_path):
    """A deleted marked comment is exempt when a new marked comment re-declares it.

    The old body is exempt because the working tree carries a NEW marked comment in
    the same file (plain, unmarked comments are ignored by the pairing). The
    replacement need not sit over byte-identical code — the 1:1 same-file pairing
    recognises it as an in-place re-declaration.
    """
    baseline = _init_repo_with(
        tmp_path, "src/proto.py",
        "# WHY: old reason\n# a plain note\ndef f():\n    return 1\n",
    )
    (tmp_path / "src/proto.py").write_text(
        "# WHY: new reason after migration\n"
        "# another plain note\n"
        "def f():\n    return 1\n",
        encoding="utf-8",
    )

    out = invariant_check._rewritten_marked_exemptions(
        tmp_path, {"src/proto.py"}, baseline
    )
    # the deleted marked comment re-declared by a new marked comment is exempt.
    assert out == {"src/proto.py": Counter({"WHY: old reason": 1})}


def test_rewrite_of_comment_and_its_code_together_is_exempt(tmp_path, monkeypatch):
    """A comment rewritten TOGETHER with the code it annotates is a re-declaration.

    Finding-1 regression: the retired code-slot correlation demanded the annotated
    code stay byte-identical, so rewriting ``# WHY: use sqlite`` above
    ``DATABASE = 'sqlite'`` into ``# WHY: use postgres for HA`` above
    ``DATABASE = 'postgres'`` computed a different slot and blocked the flow. The
    1:1 same-file pairing ignores the surrounding code, so this is correctly exempt.
    """
    baseline = _init_repo_with(
        tmp_path, "src/db.py",
        "# WHY: use sqlite\nDATABASE = 'sqlite'\n",
    )
    (tmp_path / "src/db.py").write_text(
        "# WHY: use postgres for HA\nDATABASE = 'postgres'\n",
        encoding="utf-8",
    )

    state = _install_fake_caller(monkeypatch, [json.dumps({"issues": [], "summary": "ok"})])
    flow = _make_flow(tmp_path, task="")
    flow.baseline_commit = baseline
    step = _make_step({
        "task_description": "",
        "charter": "",
        "changes_made": {"files_changed": ["src/db.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert step.outputs["why_comment_hard_violations"] == []
    assert invariant_check._rewritten_marked_exemptions(
        tmp_path, {"src/db.py"}, baseline
    ) == {"src/db.py": Counter({"WHY: use sqlite": 1})}


def test_eof_marked_comment_rewrite_is_exempt(tmp_path):
    """A marked comment at end-of-file (no following code) can still be re-declared.

    Finding-2 regression: the retired code-slot correlation skipped any slot whose
    following-code line was ``None`` (a comment at EOF), so rewriting a tail
    ``# WHY:`` rationale could never be exempted. The position-based pairing has no
    such requirement.
    """
    baseline = _init_repo_with(
        tmp_path, "src/tail.py",
        "def f():\n    return 1\n# WHY: legacy tail rationale\n",
    )
    (tmp_path / "src/tail.py").write_text(
        "def f():\n    return 1\n# WHY: new tail rationale\n",
        encoding="utf-8",
    )

    out = invariant_check._rewritten_marked_exemptions(
        tmp_path, {"src/tail.py"}, baseline
    )
    assert out == {"src/tail.py": Counter({"WHY: legacy tail rationale": 1})}


def test_cross_file_marked_move_is_not_exempt(tmp_path, monkeypatch):
    """A marked comment that vanishes from one file and reappears in another is NOT exempt.

    The authoritative rule is SAME-FILE only: a deleted marked comment is exempt iff
    it pairs one-to-one with a marked comment newly added/rewritten in the SAME file.
    A refactor that deletes ``a.py`` (dropping its ``# WHY:`` comment) and re-adds the
    function + comment verbatim in ``b.py`` leaves an UNPAIRED same-file deletion in
    ``a.py``, which must trigger REVISION_NEEDED regardless of the verbatim
    reappearance elsewhere. The accepted exit is to re-declare the rationale in the
    file that lost it (which same-file pairing then exempts).
    """
    baseline = _init_repo_with(
        tmp_path, "src/a.py",
        "# WHY: pin the retry budget\ndef work():\n    return 1\n",
    )
    # a.py deleted entirely; the function + its marked comment reappear in b.py.
    (tmp_path / "src/a.py").unlink()
    (tmp_path / "src/b.py").write_text(
        "# WHY: pin the retry budget\ndef work():\n    return 1\n",
        encoding="utf-8",
    )

    # No same-file addition in a.py, so the cross-file reappearance yields no exemption.
    out = invariant_check._rewritten_marked_exemptions(
        tmp_path, {"src/a.py", "src/b.py"}, baseline
    )
    assert out == {}

    # And the handler must route the unpaired deletion to the fix loop.
    state = _install_fake_caller(monkeypatch, [json.dumps({"issues": [], "summary": "ok"})])
    flow = _make_flow(tmp_path, task="")
    flow.baseline_commit = baseline
    step = _make_step({
        "task_description": "",
        "charter": "",
        "changes_made": {"files_changed": ["src/a.py", "src/b.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.REVISION_NEEDED
    violations = step.outputs["why_comment_hard_violations"]
    assert len(violations) == 1
    assert violations[0]["missing_in"] == ["src/a.py"]
    assert "WHY: pin the retry budget" in violations[0]["actual_behavior"]


def test_duplicate_marked_comment_one_rewritten_guards_the_other(tmp_path, monkeypatch):
    """Two IDENTICAL marked comments, both dropped, only ONE re-declared.

    Occurrence-accounting regression: a set-valued exemption would store the
    single body once and skip BOTH identical deletions, silently swallowing the
    unpaired loss. With per-occurrence counting the one re-declaration exempts
    exactly one occurrence and the second deletion still routes to the fix loop.
    """
    baseline = _init_repo_with(
        tmp_path, "src/dup.py",
        "# WHY: preserve cache invariant\n"
        "def a():\n    return 1\n"
        "# WHY: preserve cache invariant\n"
        "def b():\n    return 2\n",
    )
    # both identical marked comments removed; only ONE new marked comment added.
    (tmp_path / "src/dup.py").write_text(
        "# WHY: updated cache invariant\n"
        "def a():\n    return 1\n"
        "def b():\n    return 2\n",
        encoding="utf-8",
    )

    # The exemption helper credits exactly one occurrence, not both.
    out = invariant_check._rewritten_marked_exemptions(
        tmp_path, {"src/dup.py"}, baseline
    )
    assert out == {"src/dup.py": Counter({"WHY: preserve cache invariant": 1})}

    # The per-file deletion multiset counts BOTH removed occurrences.
    deleted = invariant_check._deleted_comments_by_file(
        tmp_path, {"src/dup.py"}, baseline
    )
    assert deleted["src/dup.py"].count("WHY: preserve cache invariant") == 2

    # One occurrence exempt, the other still a hard violation.
    issues = invariant_check._build_why_comment_guard_issues(deleted, out)
    marked = [
        i for i in issues
        if i["expectation_source"]["verbatim_quote"] == "WHY: preserve cache invariant"
    ]
    assert len(marked) == 1

    state = _install_fake_caller(monkeypatch, [json.dumps({"issues": [], "summary": "ok"})])
    flow = _make_flow(tmp_path, task="")
    flow.baseline_commit = baseline
    step = _make_step({
        "task_description": "",
        "charter": "",
        "changes_made": {"files_changed": ["src/dup.py"]},
    })
    result = invariant_check.invariant_check_handler(step, flow)
    assert result is StepStatus.REVISION_NEEDED
    hard = step.outputs["why_comment_hard_violations"]
    assert len(hard) == 1
    assert hard[0]["expectation_source"]["verbatim_quote"] == "WHY: preserve cache invariant"


# ---------------------------------------------------------------------------
# (3) advisory channel — plain comments never block, only inform
# ---------------------------------------------------------------------------

def test_plain_comment_deletion_is_advisory_only(tmp_path, monkeypatch):
    baseline = _init_repo_with(
        tmp_path, "src/svc.py",
        "# tune the batch size for throughput\ndef run():\n    return 1\n",
    )
    (tmp_path / "src/svc.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    # response #1: main audit (clean). response #2: advisory re-judge keeps it.
    advisory = {
        "losses": [{
            "file": "src/svc.py",
            "comment": "tune the batch size for throughput",
            "why_it_matters": "records a throughput tuning rationale",
        }]
    }
    state = _install_fake_caller(monkeypatch, [
        json.dumps({"issues": [], "summary": "ok"}),
        json.dumps(advisory),
    ])
    flow = _make_flow(tmp_path)
    flow.baseline_commit = baseline
    step = _make_step({
        "charter": CHARTER_TEXT,
        "changes_made": {"files_changed": ["src/svc.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert state["calls"] == 2  # main audit + one advisory triage
    # Advisory result lands in outputs only; never a hard violation / fix loop.
    assert step.outputs["why_comment_hard_violations"] == []
    losses = step.outputs["why_comment_losses"]
    assert len(losses) == 1
    assert losses[0]["comment"] == "tune the batch size for throughput"
    assert step.outputs["actionable_count"] == 0


def test_advisory_llm_failure_degrades_silently(tmp_path, monkeypatch):
    """A crashing advisory re-judge must not disturb the main audit's verdict."""
    baseline = _init_repo_with(
        tmp_path, "src/svc.py",
        "# tune the batch size\ndef run():\n    return 1\n",
    )
    (tmp_path / "src/svc.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    calls = {"n": 0}

    class FlakyCaller:
        def __init__(self, *a, **k):
            pass

        def call(self, prompt, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return json.dumps({"issues": [], "summary": "ok"})
            raise RuntimeError("advisory model down")

    monkeypatch.setattr(invariant_check, "LLMCaller", FlakyCaller)
    flow = _make_flow(tmp_path)
    flow.baseline_commit = baseline
    step = _make_step({
        "charter": CHARTER_TEXT,
        "changes_made": {"files_changed": ["src/svc.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert step.outputs["why_comment_losses"] == []
    assert step.outputs["why_comment_hard_violations"] == []


def test_no_baseline_guard_silently_skips(tmp_path, monkeypatch):
    """With no baseline commit, the guard can't diff — it skips, no false block."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "svc.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    state = _install_fake_caller(monkeypatch, [json.dumps({"issues": [], "summary": "ok"})])
    flow = _make_flow(tmp_path)  # no baseline_commit
    step = _make_step({
        "charter": CHARTER_TEXT,
        "changes_made": {"files_changed": ["src/svc.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert state["calls"] == 1  # main audit only; guard did not call the LLM
    assert step.outputs["why_comment_hard_violations"] == []
    assert step.outputs["why_comment_losses"] == []


# ---------------------------------------------------------------------------
# (4) prompt exemption clause + charter amendment
# ---------------------------------------------------------------------------

def test_prompt_carries_amendment_exemption():
    prompt = invariant_check.INVARIANT_CHECK_PROMPT
    assert "Constitutional-amendment exemption" in prompt
    assert "se3/charter.md" in prompt
    # It must scope the exemption to task-directed clauses only.
    assert "explicitly calls for that charter change" in prompt


def test_charter_declares_marker_prefix_convention():
    charter = (Path(__file__).resolve().parents[1] / "se3" / "charter.md").read_text(
        encoding="utf-8"
    )
    assert "WHY:" in charter and "INVARIANT:" in charter
    assert "invariant_check" in charter
