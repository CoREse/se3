"""Co-located tests for concurrent code-index LLM summarisation (group G1).

The (re)build fans its per-file LLM summary calls out across a
``ThreadPoolExecutor`` bounded by ``code_index.max_concurrency``. These tests pin
the four invariants that make that parallelism safe:

- **each concurrent group builds its OWN ``LLMCaller``** — never a shared one (the
  charter reserves command rotation to a single caller, whose state is not
  thread-safe);
- the in-flight call count **reaches** ``max_concurrency`` and **never exceeds** it;
- one group's failure **degrades to the heuristic** for that group only, leaving
  the rest intact and the build un-aborted;
- the summariser's ``{id: summary}`` output and the whole ``code-index.md`` are
  **identical whether built serially or concurrently** (order-independent).

They live beside the engine source as a controlled co-located exception (see base
*Engine Co-located Test Modules*).
"""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path

import pytest

from tianluo.config import (
    DEFAULT_CODE_INDEX_MAX_CONCURRENCY,
    CodeIndexConfig,
)
from tianluo.engine import code_index
from tianluo.engine import context_builder as _context_builder
from tianluo.engine.code_index import (
    SummaryTarget,
    _make_llm_summarizer,
    build_index,
)

# Capture the REAL ensure_code_index_fresh at import time — before the autouse
# ``_no_real_code_index_refresh`` conftest fixture stubs the module attribute to
# a no-op for every test. These tests specifically exercise the real hook (its
# flow-context progress emitter), so they call this captured original directly
# rather than the patched module attribute.
_REAL_ENSURE_CODE_INDEX_FRESH = _context_builder.ensure_code_index_fresh


# ---------------------------------------------------------------------------
# Fake LLMCaller stubs (monkeypatched over tianluo.engine.llm_caller.LLMCaller,
# which _make_llm_summarizer imports lazily inside each worker task)
# ---------------------------------------------------------------------------

# The prompt embeds each target as ``- id='<id>' ...`` (via ``t.id!r``); recover
# the ids so an echo stub can answer without an LLM.
_PROMPT_ID_RE = re.compile(r"id='([^']*)'")


def _ids_in_prompt(prompt: str) -> list[str]:
    return _PROMPT_ID_RE.findall(prompt)


class _EchoCaller:
    """Deterministic stub: echoes each requested id back as ``sum:<id>``.

    Records every constructed instance on the class so a test can prove that each
    concurrent group constructed its OWN caller (never a shared one).
    """

    instances: list["_EchoCaller"] = []
    _lock = threading.Lock()

    def __init__(self, **kwargs) -> None:
        with _EchoCaller._lock:
            _EchoCaller.instances.append(self)

    def call(self, prompt: str, json_mode: str | None = None) -> str:
        import json

        return json.dumps({i: f"sum:{i}" for i in _ids_in_prompt(prompt)})

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls.instances = []


class _Recorder:
    """Builds a stub-caller class that records peak concurrency via a barrier.

    Every ``call`` bumps a shared in-flight counter (tracking the peak) and then
    waits on a ``Barrier(workers)`` so that exactly ``workers`` tasks are proven
    in-flight simultaneously — making the peak observation deterministic rather
    than sleep-timing-dependent. Also records each constructed instance so the
    "one caller per group" invariant is checkable.
    """

    def __init__(self, workers: int) -> None:
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.instances: list[object] = []
        # A barrier sized to the expected concurrency: waves of `workers` tasks
        # rendezvous and release together, so the peak provably equals `workers`.
        self.barrier = threading.Barrier(workers)

    def caller_cls(self) -> type:
        rec = self

        class _BarrierCaller:
            def __init__(self, **kwargs) -> None:
                with rec.lock:
                    rec.instances.append(self)

            def call(self, prompt: str, json_mode: str | None = None) -> str:
                with rec.lock:
                    rec.in_flight += 1
                    rec.peak = max(rec.peak, rec.in_flight)
                try:
                    rec.barrier.wait(timeout=5)
                except threading.BrokenBarrierError:  # pragma: no cover
                    pass
                with rec.lock:
                    rec.in_flight -= 1
                return "{}"  # empty → heuristic fallback (irrelevant here)

        return _BarrierCaller


def _file_targets(n: int) -> list[SummaryTarget]:
    """`n` file-level targets on distinct paths → `n` independent by_file groups."""
    return [
        SummaryTarget(
            id=f"f{i}.py", path=f"f{i}.py", kind="file", name=f"f{i}.py",
            content=f"content {i}", level="file",
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Task 1 — config: max_concurrency field + fault-tolerant coerce
# ---------------------------------------------------------------------------

class TestMaxConcurrencyConfig:
    def test_default_is_four(self):
        assert CodeIndexConfig().max_concurrency == 4
        assert DEFAULT_CODE_INDEX_MAX_CONCURRENCY == 4
        assert CodeIndexConfig.from_dict({}).max_concurrency == 4

    def test_valid_value_read(self):
        assert CodeIndexConfig.from_dict({"max_concurrency": 12}).max_concurrency == 12

    @pytest.mark.parametrize("bad", [0, -3, "8", 4.0, True, None])
    def test_illegal_value_falls_back_with_warning(self, bad, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            cfg = CodeIndexConfig.from_dict({"max_concurrency": bad})
        assert cfg.max_concurrency == 4
        assert any("max_concurrency" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Task 2 — concurrent summariser: limit, per-task caller, degradation, parity
# ---------------------------------------------------------------------------

class TestConcurrentSummarizer:
    def test_reaches_exactly_max_concurrency(self, tmp_path):
        max_conc = 4
        rec = _Recorder(workers=max_conc)
        import tianluo.engine.llm_caller as llm_mod

        orig = llm_mod.LLMCaller
        llm_mod.LLMCaller = rec.caller_cls()
        try:
            # 2*max_conc groups → two clean waves of `max_conc`, so the peak
            # provably reaches — and never exceeds — the configured ceiling.
            summ = _make_llm_summarizer(tmp_path, max_conc)
            summ(_file_targets(2 * max_conc))
        finally:
            llm_mod.LLMCaller = orig

        assert rec.peak == max_conc
        # One caller per group, and all distinct instances (never shared).
        assert len(rec.instances) == 2 * max_conc
        assert len({id(c) for c in rec.instances}) == 2 * max_conc

    def test_serial_when_max_concurrency_one(self, tmp_path):
        rec = _Recorder(workers=1)
        import tianluo.engine.llm_caller as llm_mod

        orig = llm_mod.LLMCaller
        llm_mod.LLMCaller = rec.caller_cls()
        try:
            summ = _make_llm_summarizer(tmp_path, 1)
            summ(_file_targets(5))
        finally:
            llm_mod.LLMCaller = orig

        assert rec.peak == 1
        # Still one caller per group even on the inline (serial) path.
        assert len(rec.instances) == 5

    def test_each_group_builds_its_own_caller(self, tmp_path):
        _EchoCaller.reset()
        import tianluo.engine.llm_caller as llm_mod

        orig = llm_mod.LLMCaller
        llm_mod.LLMCaller = _EchoCaller
        try:
            summ = _make_llm_summarizer(tmp_path, 4)
            summ(_file_targets(6))
        finally:
            llm_mod.LLMCaller = orig

        # Six groups → six distinct caller instances; none shared across tasks.
        assert len(_EchoCaller.instances) == 6
        assert len({id(c) for c in _EchoCaller.instances}) == 6

    def test_results_identical_serial_vs_concurrent(self, tmp_path):
        import tianluo.engine.llm_caller as llm_mod

        targets = _file_targets(8)
        orig = llm_mod.LLMCaller
        llm_mod.LLMCaller = _EchoCaller
        try:
            serial = _make_llm_summarizer(tmp_path, 1)(list(targets))
            concurrent = _make_llm_summarizer(tmp_path, 8)(list(targets))
        finally:
            llm_mod.LLMCaller = orig

        assert serial == concurrent
        assert serial == {f"f{i}.py": f"sum:f{i}.py" for i in range(8)}

    def test_single_group_failure_degrades_to_heuristic(self, tmp_path):
        import tianluo.engine.llm_caller as llm_mod

        class _PartlyFailingCaller:
            def __init__(self, **kwargs) -> None:
                pass

            def call(self, prompt: str, json_mode: str | None = None) -> str:
                import json

                # The one poisoned group blows up; its peers answer normally.
                if "Path: bad.py" in prompt:
                    raise RuntimeError("boom")
                return json.dumps({i: f"sum:{i}" for i in _ids_in_prompt(prompt)})

        targets = [
            SummaryTarget(id="a", path="a.py", kind="function", name="a",
                          content="x", level="symbol"),
            SummaryTarget(id="b", path="bad.py", kind="function", name="foo",
                          content="x", level="symbol"),
            SummaryTarget(id="c", path="c.py", kind="function", name="c",
                          content="x", level="symbol"),
        ]
        orig = llm_mod.LLMCaller
        llm_mod.LLMCaller = _PartlyFailingCaller
        try:
            out = _make_llm_summarizer(tmp_path, 4)(targets)
        finally:
            llm_mod.LLMCaller = orig

        # Healthy groups summarised; the failed group degrades to the heuristic
        # (``"<kind> <name>"``) and the whole call still returns every id.
        assert out["a"] == "sum:a"
        assert out["c"] == "sum:c"
        assert out["b"] == "function foo"


# ---------------------------------------------------------------------------
# Task 3 — Pass 2 batched build: md byte-identical across concurrency levels
# ---------------------------------------------------------------------------

def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True
    )


class _DeterministicSummarizer:
    """Injected summariser (bypasses the LLM path) returning ``S:<name>`` per
    target — so any md difference is attributable to Pass 2 batching alone."""

    def __call__(self, targets):
        return {t.id: f"S:{t.name}" for t in targets}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Tester")
    (root / ".gitignore").write_text("/tianluo/*\n", encoding="utf-8")
    (root / "pkg").mkdir()
    # Several files so a batch of size < file-count exercises multi-batch flushing.
    for i in range(7):
        (root / "pkg" / f"m{i}.py").write_text(
            f"def fn{i}():\n    return {i}\n\n\n"
            f"class C{i}:\n    def meth(self):\n        return {i}\n",
            encoding="utf-8",
        )
    (root / "README.md").write_text("# Title\n\nintro\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "snapshot")
    return root


class TestBatchedPass2:
    def test_md_byte_identical_across_concurrency(self, project: Path):
        summ = _DeterministicSummarizer()

        build_index(project, summarizer=summ, force=True,
                    cfg=CodeIndexConfig(max_concurrency=1))
        md_serial = code_index.md_path(project).read_text(encoding="utf-8")

        build_index(project, summarizer=summ, force=True,
                    cfg=CodeIndexConfig(max_concurrency=8))
        md_concurrent = code_index.md_path(project).read_text(encoding="utf-8")

        assert md_serial == md_concurrent

    def test_partial_flush_is_a_superset_resume_point(self, project: Path):
        # A crash mid-Pass-2 leaves a partial md; the next incremental build must
        # reuse every already-summarised node (Pass 1 seed) and re-summarise
        # nothing untouched. Build fully, then rebuild incrementally with a
        # summariser that would blow up if asked to touch an unchanged node.
        build_index(project, summarizer=_DeterministicSummarizer(), force=True,
                    cfg=CodeIndexConfig(max_concurrency=3))

        class _NoWorkExpected:
            def __call__(self, targets):
                assert not targets, (
                    f"incremental rebuild re-summarised unchanged nodes: "
                    f"{[t.id for t in targets]}"
                )
                return {}

        # No source changed → every node reused → summariser never gets targets.
        build_index(project, summarizer=_NoWorkExpected(),
                    cfg=CodeIndexConfig(max_concurrency=3))


class _ScatterRecorder:
    """Peak-concurrency recorder that gates ONLY the calls for a known set of
    edited files on a barrier sized to that set.

    On an incremental rebuild the stale files are scattered across sort order, so
    the barrier's waiters are exactly the edited-file summary calls (the parent
    dir is reused — its child list is unchanged — so no dir call competes). If the
    edited files are batched together they rendezvous and the peak reaches the
    edit count; if they are isolated into single-file positional batches (the old
    bug) each call waits alone and the barrier times out, leaving peak == 1.
    """

    _PATH_RE = re.compile(r"Path: (\S+)")

    def __init__(self, edited_paths: set[str]) -> None:
        self.edited = set(edited_paths)
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.barrier = threading.Barrier(len(self.edited))

    def caller_cls(self) -> type:
        rec = self

        class _Caller:
            def __init__(self, **kwargs) -> None:
                pass

            def call(self, prompt: str, json_mode: str | None = None) -> str:
                import json

                m = rec._PATH_RE.search(prompt)
                gated = bool(m) and m.group(1) in rec.edited
                if gated:
                    with rec.lock:
                        rec.in_flight += 1
                        rec.peak = max(rec.peak, rec.in_flight)
                    try:
                        rec.barrier.wait(timeout=5)
                    except threading.BrokenBarrierError:  # pragma: no cover
                        pass
                    with rec.lock:
                        rec.in_flight -= 1
                return json.dumps({i: f"sum:{i}" for i in _ids_in_prompt(prompt)})

        return _Caller


class TestIncrementalConcurrency:
    def test_scattered_incremental_edits_run_concurrently(self, tmp_path: Path):
        # Regression: the commit-time incremental path must reach max_concurrency
        # even when the touched files are scattered across the sorted file list.
        # Batching stale FILES (not positional slices of the full list) is what
        # makes this hold; positional batching isolated each scattered edit into
        # its own single-file batch and ran fully serial (peak == 1).
        import tianluo.engine.llm_caller as llm_mod

        root = tmp_path / "proj"
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "Tester")
        (root / ".gitignore").write_text("/tianluo/*\n", encoding="utf-8")
        # 15 root files: edits at f00/f07/f14 are >max_concurrency apart in sort
        # order, so positional batches of 4 would hold at most ONE stale file each.
        for i in range(15):
            (root / f"f{i:02d}.py").write_text(
                f"def fn{i}():\n    return {i}\n", encoding="utf-8"
            )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "snap")

        orig = llm_mod.LLMCaller
        llm_mod.LLMCaller = _EchoCaller
        try:
            build_index(root, force=True, cfg=CodeIndexConfig(max_concurrency=4))
        finally:
            llm_mod.LLMCaller = orig

        # Rename each edited file's symbol so BOTH its symbol node and its file
        # node (list-fp keyed on child names) go stale and need re-summarising.
        edited = {"f00.py", "f07.py", "f14.py"}
        for name in edited:
            (root / name).write_text(
                f"def changed_{name[1:3]}():\n    return 'x'\n", encoding="utf-8"
            )

        rec = _ScatterRecorder(edited_paths=edited)
        llm_mod.LLMCaller = rec.caller_cls()
        try:
            build_index(root, cfg=CodeIndexConfig(max_concurrency=4))
        finally:
            llm_mod.LLMCaller = orig

        assert rec.peak == len(edited), (
            f"scattered incremental edits must summarise concurrently "
            f"(peak == {len(edited)}); got peak={rec.peak} — positional batching "
            f"would serialise them to peak 1"
        )


# ---------------------------------------------------------------------------
# Group G2, Task 2 — per-node build progress callback
# ---------------------------------------------------------------------------

class TestProgressCallback:
    def test_fires_once_per_file_and_dir_node(self, project: Path):
        # force=True → every file + dir node is (re)summarised, so the callback
        # count equals the file+dir node count exactly.
        calls: list[tuple] = []
        build_index(
            project, summarizer=_DeterministicSummarizer(), force=True,
            cfg=CodeIndexConfig(max_concurrency=3),
            progress=lambda *a: calls.append(a),
        )
        assert calls, "expected progress callbacks"
        # (path, kind, done, total, phase)
        totals = {c[3] for c in calls}
        assert len(totals) == 1, "total must stay constant across the whole build"
        total = totals.pop()
        assert total == len(calls), "exactly one callback per summarised file/dir node"
        dones = [c[2] for c in calls]
        assert dones == list(range(1, total + 1)), "done monotonic 1..total, no repeats"
        phases = {c[4] for c in calls}
        assert phases <= {"file", "dir"}
        assert "file" in phases and "dir" in phases
        # Symbol nodes are never reported (only file/dir orientation nodes are).
        assert all(k for _p, k, *_ in calls)
        # Pass 2 (files) fully precedes Pass 3 (dirs).
        file_idx = [i for i, c in enumerate(calls) if c[4] == "file"]
        dir_idx = [i for i, c in enumerate(calls) if c[4] == "dir"]
        assert max(file_idx) < min(dir_idx)

    def test_body_only_edit_still_reports_its_file(self, project: Path):
        # Regression: a body-only edit (typical bugfix commit) changes only a
        # symbol's content fingerprint — the file node's list-fp (symbol roster)
        # is unchanged, so the file node is REUSED while the symbol is re-
        # summarised. Progress must still surface that file (else the WebUI shows
        # nothing for the whole rebuild). Report unit is the file, via its
        # symbol wave.
        build_index(project, summarizer=_DeterministicSummarizer(), force=True,
                    cfg=CodeIndexConfig(max_concurrency=3))

        # Rewrite ONLY fn0's body; keep every symbol NAME + KIND identical so the
        # list-fp gate reuses the file node and only the symbol goes stale.
        (project / "pkg" / "m0.py").write_text(
            "def fn0():\n    return 999\n\n\n"
            "class C0:\n    def meth(self):\n        return 0\n",
            encoding="utf-8",
        )

        calls: list[tuple] = []
        build_index(
            project, summarizer=_DeterministicSummarizer(),
            cfg=CodeIndexConfig(max_concurrency=3),
            progress=lambda *a: calls.append(a),
        )
        # Exactly one report, for the edited file, at file granularity — not zero.
        assert calls, "body-only edit must still report progress for the file"
        paths = [c[0] for c in calls]
        assert "pkg/m0.py" in paths
        # total == number of reports, done climbs 1..total, no symbol-level noise.
        totals = {c[3] for c in calls}
        assert len(totals) == 1 and totals.pop() == len(calls)
        assert [c[2] for c in calls] == list(range(1, len(calls) + 1))
        assert all(c[4] in ("file", "dir") for c in calls)
        # The reused-but-stale-symbol file is reported as a FILE, and only once.
        assert paths.count("pkg/m0.py") == 1
        assert next(c[4] for c in calls if c[0] == "pkg/m0.py") == "file"

    def test_progress_none_is_noop(self, project: Path):
        # Omitting progress leaves the build path byte-for-byte as before.
        build_index(
            project, summarizer=_DeterministicSummarizer(), force=True,
            cfg=CodeIndexConfig(max_concurrency=3), progress=None,
        )
        assert code_index.md_path(project).exists()

    def test_no_callback_for_fully_reused_build(self, project: Path):
        # First full build, then an incremental no-change build: nothing is
        # stale → progress fires zero times (total work is 0).
        build_index(project, summarizer=_DeterministicSummarizer(), force=True,
                    cfg=CodeIndexConfig(max_concurrency=3))
        calls: list[tuple] = []
        build_index(
            project, summarizer=_DeterministicSummarizer(),
            cfg=CodeIndexConfig(max_concurrency=3),
            progress=lambda *a: calls.append(a),
        )
        assert calls == []


# ---------------------------------------------------------------------------
# Group G2, Task 1 — record_index_progress NDJSON + history/retry skip
# ---------------------------------------------------------------------------

class TestRecordIndexProgress:
    def test_write_and_history_retry_skip(self, tmp_path: Path):
        import json

        from tianluo.engine.chat_history import (
            format_history_for_retry,
            get_step_history,
            record_index_progress,
            record_prompt,
        )

        record_prompt(tmp_path, "f1", "s1", "commit", "hello", attempt=0)
        record_index_progress(
            tmp_path, "f1", "s1", "commit",
            path="pkg/m0.py", kind="python", done=1, total=3, phase="file",
        )
        record_index_progress(
            tmp_path, "f1", "s1", "commit",
            path="pkg/", kind="directory", done=3, total=3, phase="dir",
        )

        # get_step_history skips the index_progress lines → only the user turn.
        session = get_step_history(tmp_path, "f1", "s1")
        assert session is not None
        assert [m.role for m in session.messages] == ["user"]
        assert session.messages[0].content == "hello"

        # Retry context (reads through get_step_history) also drops them.
        retry = format_history_for_retry(tmp_path, "f1", "s1")
        assert retry is None or "pkg/m0.py" not in retry

        # The raw jsonl carries well-formed index_progress records.
        jf = tmp_path / "tianluo" / "history" / "f1" / "s1.jsonl"
        recs = [json.loads(ln) for ln in jf.read_text().splitlines() if ln.strip()]
        ip = [r for r in recs if r.get("type") == "index_progress"]
        assert len(ip) == 2
        assert ip[0]["path"] == "pkg/m0.py" and ip[0]["kind"] == "python"
        assert ip[0]["done"] == 1 and ip[0]["total"] == 3 and ip[0]["phase"] == "file"
        assert ip[0]["role"] == "system" and ip[0]["step_type"] == "commit"
        assert ip[1]["phase"] == "dir"

    def test_missing_ids_soft_noop(self, tmp_path: Path):
        from tianluo.engine.chat_history import record_index_progress

        record_index_progress(
            tmp_path, "", "", "commit",
            path="a.py", kind="python", done=1, total=1, phase="file",
        )
        assert not (tmp_path / "tianluo" / "history").exists()

    def test_oserror_is_swallowed(self, tmp_path: Path):
        from tianluo.engine.chat_history import record_index_progress

        # Make the flow "dir" a plain file so mkdir raises OSError; the write
        # must warn-and-swallow, never propagate.
        hist = tmp_path / "tianluo" / "history"
        hist.mkdir(parents=True)
        (hist / "f1").write_text("x", encoding="utf-8")
        record_index_progress(
            tmp_path, "f1", "s1", "commit",
            path="a.py", kind="python", done=1, total=1, phase="file",
        )


# ---------------------------------------------------------------------------
# Group G2, Task 3 — ensure_code_index_fresh flow-context progress emitter
# ---------------------------------------------------------------------------

class TestEnsureCodeIndexFreshProgress:
    def _build_with_echo(self, project: Path) -> None:
        import tianluo.engine.llm_caller as llm_mod

        orig = llm_mod.LLMCaller
        llm_mod.LLMCaller = _EchoCaller
        try:
            build_index(project, force=True,
                        cfg=CodeIndexConfig(max_concurrency=3))
        finally:
            llm_mod.LLMCaller = orig

    def test_no_flow_context_is_noop(self, project: Path):
        self._build_with_echo(project)
        # No flow/step context → silent refresh, writes no history, never raises.
        _REAL_ENSURE_CODE_INDEX_FRESH(project)
        assert not (project / "tianluo" / "history").exists()

    def test_commit_context_writes_progress(self, project: Path):
        import json

        import tianluo.engine.llm_caller as llm_mod
        from tianluo.engine.chat_history import get_step_history

        self._build_with_echo(project)
        # Add a new file: its file node + parent dir become stale and are
        # (re)summarised, firing progress on the commit path.
        (project / "pkg" / "newmod.py").write_text(
            "def z():\n    return 0\n", encoding="utf-8"
        )
        orig = llm_mod.LLMCaller
        llm_mod.LLMCaller = _EchoCaller
        try:
            _REAL_ENSURE_CODE_INDEX_FRESH(
                project, flow_id="f1", step_id="s1", step_type="commit"
            )
        finally:
            llm_mod.LLMCaller = orig

        jf = project / "tianluo" / "history" / "f1" / "s1.jsonl"
        assert jf.exists()
        recs = [json.loads(ln) for ln in jf.read_text().splitlines() if ln.strip()]
        ip = [r for r in recs if r.get("type") == "index_progress"]
        assert ip, "expected index_progress records written on the commit path"
        assert all(r["total"] == ip[0]["total"] for r in ip)
        assert [r["done"] for r in ip] == list(range(1, len(ip) + 1))
        assert {r["phase"] for r in ip} <= {"file", "dir"}

        # get_step_history skips them → no system-role bleed into the session.
        session = get_step_history(project, "f1", "s1")
        assert session is None or all(m.role != "system" for m in session.messages)
