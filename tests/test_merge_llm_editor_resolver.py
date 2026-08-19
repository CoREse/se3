"""Regression tests for the LLM-as-editor batch conflict resolver (G7 task 1).

Covers tasks (i), (ii), (iii) and (vi) from the design doc:

* (i)   ``fast`` mode resolves a single conflict file in one LLM call;
        the final file is free of conflict markers and the legacy
        take-theirs path is never invoked.
* (ii)  ``fast`` mode batched over multiple conflicting files where
        the first LLM round only clears some of them: the remainder go
        into a second round and the whole batch eventually converges.
* (iii) ``fast`` mode that never converges within
        ``max_conflict_resolve_iterations``: the resolver reports an
        unsuccessful outcome and the orchestrator surface fails the
        merge without escalating to a human MCP call.
* (vi)  Historical scenarios that would previously have walked the
        take-theirs fallback no longer do — the LLM cleared the markers
        instead and ``_record_take_theirs_event`` is not called from
        any production module.

Stub LLM callers are injected via ``monkeypatch``; no real Claude
subprocess is launched.
"""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
from pathlib import Path

import pytest

from tianluo.engine.merge.conflict_context import ConflictFile
from tianluo.engine.merge.conflict_resolver import (
    BatchContext,
    ConflictResolver,
    MergeStrategy,
)
from tianluo.engine.merge.strategy import DecisionAction, StrategyDecider


# --------- git helpers ---------


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")


def _commit(path: Path, message: str) -> None:
    _git(path, "add", "-A")
    _git(path, "commit", "-m", message)


def _current_branch(path: Path) -> str:
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _setup_conflict_file(
    tmp_path: Path,
    rel_path: str,
    base: str,
    ours: str,
    theirs: str,
) -> tuple[str, str]:
    """Create a repo with an existing file/conflict.

    Note: this function only sets up the working-tree files on the
    *ours* side. Tests that need a conflict-marker file simply write
    one directly; ``resolve_batch`` works off of on-disk content.

    Returns (ours_branch, theirs_branch).
    """
    _init_repo(tmp_path)
    (tmp_path / rel_path).write_text(base)
    _commit(tmp_path, "base")
    ours_branch = _current_branch(tmp_path)
    _git(tmp_path, "checkout", "-b", "theirs-branch")
    (tmp_path / rel_path).write_text(theirs)
    _commit(tmp_path, "theirs change")
    _git(tmp_path, "checkout", ours_branch)
    (tmp_path / rel_path).write_text(ours)
    _commit(tmp_path, "ours change")
    return ours_branch, "theirs-branch"


def _write_with_markers(path: Path, ours: str, theirs: str) -> None:
    """Write a file containing standard git conflict markers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<<<<<<< HEAD\n"
        f"{ours}"
        "=======\n"
        f"{theirs}"
        ">>>>>>> theirs-branch\n"
    )


def _make_conflict_file(rel_path: str, working_content: str) -> ConflictFile:
    """Build a minimal ConflictFile suitable for resolve_batch."""
    return ConflictFile(
        path=rel_path,
        base_content="",
        ours_content="",
        theirs_content="",
        working_content=working_content,
        base_exists=True,
        ours_exists=True,
        theirs_exists=True,
        is_binary=False,
    )


def _make_batch_context(tmp_path: Path, strategy: MergeStrategy) -> BatchContext:
    return BatchContext(
        project_root=tmp_path,
        ours_branch="ours",
        theirs_branch="theirs-branch",
        merge_base="deadbeef",
        ours_head_sha="aaaa",
        theirs_head_sha="bbbb",
        ours_head_message="ours change",
        theirs_head_message="theirs change",
        ours_log_oneline=["aaaa ours"],
        theirs_log_oneline=["bbbb theirs"],
        strategy=strategy,
    )


# --------- stub LLM caller ---------


class _ScriptedEditor:
    """A scripted LLM stub that "edits" the working tree via Python.

    Each entry in ``actions`` is called once per iteration with the
    project root and returns the response preview string. Iterating
    past the end of ``actions`` raises ``AssertionError`` so the test
    catches unexpected extra calls.
    """

    def __init__(self, actions):
        self.actions = list(actions)
        self.call_count = 0

    def __call__(self, prompt: str) -> str:
        if self.call_count >= len(self.actions):
            raise AssertionError(
                f"LLM stub exhausted (call #{self.call_count + 1})"
            )
        action = self.actions[self.call_count]
        self.call_count += 1
        return action(prompt) or ""


def _install_stub(monkeypatch, stub: _ScriptedEditor) -> None:
    """Replace ``ConflictResolver._call_llm`` with the stub."""
    monkeypatch.setattr(
        ConflictResolver,
        "_call_llm",
        lambda self, prompt: stub(prompt),
    )


# ---------------------------------------------------------------------
# Task (i): single-file single-iteration success
# ---------------------------------------------------------------------


def test_fast_single_file_one_iteration_success(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "shared.txt"
    _write_with_markers(target, "ours-line\n", "theirs-line\n")

    def edit_action(prompt: str) -> str:
        assert "Iteration 1 of" in prompt
        # The "LLM" writes the resolved content directly to disk.
        target.write_text("ours-line\ntheirs-line\n")
        return "rewrote shared.txt to merge both sides"

    stub = _ScriptedEditor([edit_action])
    _install_stub(monkeypatch, stub)

    resolver = ConflictResolver(tmp_path)
    ctx = _make_batch_context(tmp_path, MergeStrategy.FAST)
    cf = _make_conflict_file(
        "shared.txt",
        "<<<<<<< HEAD\nours-line\n=======\ntheirs-line\n>>>>>>> theirs-branch\n",
    )

    outcome = resolver.resolve_batch([cf], ctx, max_iterations=10)

    assert outcome.success is True
    assert outcome.unresolved == []
    assert outcome.iterations_used == 1
    assert outcome.escalation_reason is None
    assert "<<<<<<<" not in target.read_text()
    assert stub.call_count == 1


def test_fast_strategy_decider_accepts_single_file(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "code.py"
    _write_with_markers(target, "def f(): return 1\n", "def f(): return 2\n")

    def edit_action(prompt: str) -> str:
        target.write_text("def f(): return 12\n")
        return ""

    _install_stub(monkeypatch, _ScriptedEditor([edit_action]))

    resolver = ConflictResolver(tmp_path)
    ctx = _make_batch_context(tmp_path, MergeStrategy.FAST)
    cf = _make_conflict_file("code.py", target.read_text())

    decider = StrategyDecider()
    decision = decider.resolve_and_decide(
        resolver, [cf], ctx, max_iterations=10,
    )
    assert decision.action == DecisionAction.ACCEPT
    assert "<<<<<<<" not in target.read_text()


# ---------------------------------------------------------------------
# Task (ii): multiple files, multi-iteration convergence
# ---------------------------------------------------------------------


def test_fast_multi_file_multi_round_convergence(tmp_path: Path, monkeypatch) -> None:
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f3 = tmp_path / "c.txt"
    _write_with_markers(f1, "a-ours\n", "a-theirs\n")
    _write_with_markers(f2, "b-ours\n", "b-theirs\n")
    _write_with_markers(f3, "c-ours\n", "c-theirs\n")

    # Round 1: only clears f1; leaves f2 and f3 with markers.
    # Round 2: clears f2.
    # Round 3: clears f3.
    def round1(prompt: str) -> str:
        f1.write_text("a-ours\na-theirs\n")
        return ""

    def round2(prompt: str) -> str:
        # Sanity: round 2 prompt should mention the iteration counter.
        assert "Iteration 2 of" in prompt
        # And it should only list the files that still have markers.
        assert "a.txt" not in prompt or "Previous Iteration Outcomes" in prompt
        f2.write_text("b-ours\nb-theirs\n")
        return ""

    def round3(prompt: str) -> str:
        assert "Iteration 3 of" in prompt
        f3.write_text("c-ours\nc-theirs\n")
        return ""

    stub = _ScriptedEditor([round1, round2, round3])
    _install_stub(monkeypatch, stub)

    resolver = ConflictResolver(tmp_path)
    ctx = _make_batch_context(tmp_path, MergeStrategy.FAST)
    files = [
        _make_conflict_file("a.txt", f1.read_text()),
        _make_conflict_file("b.txt", f2.read_text()),
        _make_conflict_file("c.txt", f3.read_text()),
    ]

    outcome = resolver.resolve_batch(files, ctx, max_iterations=10)

    assert outcome.success is True
    assert outcome.iterations_used == 3
    assert outcome.unresolved == []
    for p in (f1, f2, f3):
        assert "<<<<<<<" not in p.read_text(), f"{p.name} still has markers"
    # The history should record markers_remaining for the first two rounds.
    kinds = [h.kind for h in outcome.history]
    assert kinds.count("markers_remaining") == 2


# ---------------------------------------------------------------------
# Task (iii): fast hits the iteration cap → merge fails, no human call
# ---------------------------------------------------------------------


def test_fast_exhausts_iterations_fails_without_human_call(
    tmp_path: Path, monkeypatch,
) -> None:
    target = tmp_path / "doomed.txt"
    _write_with_markers(target, "ours\n", "theirs\n")
    original = target.read_text()

    def noop(prompt: str) -> str:
        # The "LLM" tries to edit but produces nothing useful; markers
        # remain on disk every iteration.
        return "I couldn't figure it out"

    actions = [noop] * 10
    stub = _ScriptedEditor(actions)
    _install_stub(monkeypatch, stub)

    resolver = ConflictResolver(tmp_path)
    ctx = _make_batch_context(tmp_path, MergeStrategy.FAST)
    cf = _make_conflict_file("doomed.txt", original)

    decider = StrategyDecider()
    decision = decider.resolve_and_decide(
        resolver, [cf], ctx, max_iterations=10,
    )

    assert decision.action == DecisionAction.REJECT
    assert decision.outcome is not None
    assert decision.outcome.iterations_used == 10
    assert decision.outcome.escalation_reason == "fast_failed"
    assert Path("doomed.txt").name in {p.name for p in decision.unresolved_files}
    # No human-call surface for fast.
    assert decision.action != DecisionAction.HUMAN_CALL
    # File still has markers — outcome correctly tracked the failure.
    assert "<<<<<<<" in target.read_text()


# ---------------------------------------------------------------------
# Task (vi): historical take-theirs scenarios are no longer wired up
# ---------------------------------------------------------------------


def test_no_take_theirs_helper_remains_in_merge_package() -> None:
    """The orchestrator and strategy modules must not expose a
    ``_robust_take_theirs_commit`` or ``_record_take_theirs_event``
    callable any more — the entire take-theirs fallback was excised in
    G4, and this test guards against accidental reintroduction.
    """
    import tianluo.engine.merge as merge_pkg

    seen: list[str] = []
    for _finder, name, _ispkg in pkgutil.iter_modules(merge_pkg.__path__):
        mod = importlib.import_module(f"tianluo.engine.merge.{name}")
        for forbidden in (
            "_robust_take_theirs_commit",
            "_record_take_theirs_event",
        ):
            if hasattr(mod, forbidden):
                seen.append(f"{name}.{forbidden}")
    assert not seen, (
        "Found take-theirs helpers that should have been removed: "
        + ", ".join(seen)
    )


def test_replay_resolves_via_llm_without_take_theirs(
    tmp_path: Path, monkeypatch,
) -> None:
    """A scenario that historically would have walked the take-theirs
    fallback — LLM returns an "I can't help" first round, then resolves
    the file on the second round.  No take-theirs side effect occurs;
    the merge resolves cleanly through the new editor loop.
    """
    target = tmp_path / "replay.txt"
    _write_with_markers(target, "ours\n", "theirs\n")
    captured_calls: list[str] = []

    def first_round(prompt: str) -> str:
        captured_calls.append("r1")
        # Don't touch the file: this would historically have failed parsing.
        return "I cannot resolve this"

    def second_round(prompt: str) -> str:
        captured_calls.append("r2")
        target.write_text("ours\ntheirs\n")
        return "merged both"

    stub = _ScriptedEditor([first_round, second_round])
    _install_stub(monkeypatch, stub)

    # Spy on any callable named ``_record_take_theirs_event`` that
    # might be added in the future.  At present there is no such hook,
    # so we simply make sure none gets imported and called via the
    # merge package surface.
    resolver = ConflictResolver(tmp_path)
    ctx = _make_batch_context(tmp_path, MergeStrategy.FAST)
    cf = _make_conflict_file("replay.txt", target.read_text())

    outcome = resolver.resolve_batch([cf], ctx, max_iterations=10)

    assert outcome.success is True
    assert outcome.iterations_used == 2
    assert captured_calls == ["r1", "r2"]
    assert "<<<<<<<" not in target.read_text()


def test_synthesis_osfailure_does_not_synthesise_deletion(
    tmp_path: Path, monkeypatch,
) -> None:
    """Regression: a successful LLM resolution whose read-back fails
    with :class:`OSError` MUST flag the file for human review rather
    than silently degrading to an empty ``resolved_content`` (which
    ``_apply_resolution`` treats as a delete request).

    Without this guard a transient EACCES / EIO on a successfully
    resolved file would cause ``git rm -f`` to erase the file the LLM
    had just fixed.
    """
    from tianluo.engine.merge.conflict_context import ConflictContext
    from tianluo.engine.merge.conflict_resolver import (
        BatchResolveOutcome,
        Confidence,
    )

    target = tmp_path / "resolved.txt"
    target.write_text("LLM produced this clean content\n")

    resolver = ConflictResolver(tmp_path)
    cf = _make_conflict_file(
        "resolved.txt", "(working content with markers)",
    )
    ctx = ConflictContext(
        project_root=tmp_path,
        ours_branch="ours",
        theirs_branch="theirs-branch",
        merge_base="deadbeef",
        files=[cf],
    )
    # LLM cleared the markers — ``resolve_batch`` returns success with
    # the file listed under ``resolved``.
    outcome = BatchResolveOutcome(
        resolved=[target],
        unresolved=[],
        iterations_used=1,
    )

    # Inject an OSError on read_text for the resolved file so the
    # synthesiser's read-back path fails.
    original_read_text = Path.read_text

    def failing_read_text(self, *args, **kwargs):
        if self == target:
            raise OSError("EIO simulated")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)

    resolution = resolver._synthesize_resolution_from_outcome(outcome, ctx)

    # The single file must be flagged for human review and the
    # ``resolved_content`` must NOT be empty (that would be a delete
    # request).  Carrying the working content with markers preserves
    # the disputed state for the reviewer.
    assert len(resolution.files) == 1
    fr = resolution.files[0]
    assert fr.flags.get("requires_human_review") is True
    assert fr.resolved_content != ""
    assert fr.overall_confidence == Confidence.LOW
    # The overall resolution must also flag for human review so safe
    # strategy escalates and fast strategy rejects.
    assert resolution.flags.get("requires_human_review") is True
