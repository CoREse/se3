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
