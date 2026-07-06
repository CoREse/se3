"""Boundary e2e for issue #260 — the discovery→analyze WS-append freeze.

Issue #260 (follow-up to #209/#243/#244): after a running flow crosses the
discovery→analyze boundary, an already-open WebUI chat stops receiving *any*
new content over the live ``/ws/ui`` WebSocket. ~5s later the frontend's
progression grace fallback (commit 8a128eb3) fires a silent rebuild, which only
surfaces the lone ``analyze`` step label — nothing appears for the rest of
analyze until the user exits and re-enters the chat. That the 5s timer elapses
at all proves ``flowConversationAppendSeq`` did not grow, i.e. the WS increment
truly stopped at that boundary.

This module is the G1 **boundary reproduction + five-hop diagnosis harness**. It
drives the REAL daemon ``DaemonHistoryReader`` over a REAL on-disk
``engine.json``/``jsonl`` evolution that mirrors the discovery→analyze timing
(steps list first-write + PAUSED→RUNNING flip + a freshly-created ``02_analyze``
jsonl + continuous analyze appends), feeds every increment through the REAL
server ``_handle_message`` (cache + ``/ws/ui`` broadcast), and asserts on what a
subscribed ``_UiWS`` client receives.

The crucial fidelity difference from the pre-existing ungated harness
(``test_server_history_live_append_broadcast.py::_drive_scenario``, which calls
``read_active_flows`` unconditionally every mutation) is that this harness
reproduces the daemon push loop's **signature gate**: it reads the delta ONLY
when ``active_flow_signature`` (via ``client._history_changed``) reports a
change — the exact debounce the real daemon applies. That gate is where a
boundary-specific freeze could hide.

Findings this harness locks (see ``tests/DISCOVERY_ANALYZE_BOUNDARY_VERIFICATION.md``):

* The signature-gated in-process path is **clean** across the boundary — because
  ``active_flow_signature`` keys the engine.json on a RAW ``_safe_stat`` and each
  per-step jsonl on a RAW ``_safe_stat``, every distinct-stat disk write shifts
  the signature and the delta is read + broadcast. So read_flow / read_active_flows
  / append_history / the /ws/ui fanout are all proven correct in-process (the
  ``test_boundary_*`` and ``test_normal_step_boundary_*`` cases pass).
* The confirmed daemon-side latent hazard is the ``disk_json_cache`` stale parse
  for the LIVE ``engine.json``: a same-``(mtime, size)`` rewrite that changes only
  the true middle of a >128 KiB engine.json returns the just-superseded parse
  (``test_active_engine_json_middle_rewrite_returns_stale_parse`` — ``xfail``
  until G2 hardens it). This is the design's primary fix target.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from se3.daemon import disk_json_cache, protocol
from se3.daemon.history import DaemonHistoryReader
from se3.server.state import ServerState
from se3.server.ws import HistoryRequestRegistry, UiHub, _handle_message


# --------------------------------------------------------------------------
# UI stand-in + on-disk builders (mirror what chat_history / persistence write)
# --------------------------------------------------------------------------


class _UiWS:
    """Minimal UI WebSocket stand-in capturing decoded frames it is sent."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def history_frames(self, flow_id: str | None = None) -> list:
        frames = [m for m in self.sent if m.get("type") == "history_data"]
        if flow_id is not None:
            frames = [m for m in frames if m.get("flow_id") == flow_id]
        return frames


def _write_engine(root: Path, flow_id: str, status: str, steps: dict | None = None,
                  current_step_index: int = 0) -> None:
    """Write a realistic ``engine.json`` with a ``state.steps`` table.

    Discovery runs with ``steps == {}`` (the flow snapshot's unique no-step
    form); the boundary is the ONE place the full steps table is first written
    and ``status`` flips PAUSED→RUNNING while analyze starts.
    """
    state_dir = root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "engine.json").write_text(
        json.dumps(
            {
                "flow_id": flow_id,
                "status": status,
                "task_description": "Add a /health endpoint",
                "state": {"steps": steps or {}, "current_step_index": current_step_index},
                "is_worktree_mode": False,
            }
        ),
        encoding="utf-8",
    )


def _hist(root: Path, flow_id: str) -> Path:
    return root / "se3" / "history" / flow_id


def _write_jsonl(path: Path, lines: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, lines: list) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for x in lines:
            fh.write(json.dumps(x) + "\n")


def _chat(role: str, content: str, ts: int) -> dict:
    return {"role": role, "content": content, "timestamp": ts}


def _started(step_id: str, step_type: str, ts: int) -> dict:
    return {"type": "step_started", "step_id": step_id, "step_type": step_type,
            "status": "running", "timestamp": ts}


def _completed(step_id: str, step_type: str, ts: int) -> dict:
    return {"type": "step_completed", "step_id": step_id, "step_type": step_type,
            "data": {"step": {"step_id": step_id, "step_type": step_type,
                              "status": "completed", "outputs": {}}}, "timestamp": ts}


# --------------------------------------------------------------------------
# Scenario scripts: an ordered list of disk mutations, one push tick per entry.
# --------------------------------------------------------------------------


#: The authoritative steps table first written at the boundary (discovery is the
#: only step that runs with an EMPTY steps table; here the full plan lands).
_STEPS_TABLE = {
    f"{i:02d}_{name}": {"status": "pending", "step_type": name}
    for i, name in enumerate(
        ["analyze", "plan", "implement", "test", "commit"], start=2
    )
}


def _boundary_mutations(root: Path, flow_id: str):
    """discovery (empty steps) → confirm → steps first-write + PAUSED→RUNNING →
    a freshly-created 02_analyze jsonl → analyze keeps appending.

    Each callable returns the human label of the record(s) it appended, so the
    driver can assert that append reached the UI within its push cycle.
    """
    disc = _hist(root, flow_id) / "01_discovery_ab12.jsonl"
    anal = _hist(root, flow_id) / "02_analyze_cd34.jsonl"
    D = "01_discovery_ab12"
    A = "02_analyze_cd34"

    def m0():  # discovery starts — engine has NO steps yet
        _write_engine(root, flow_id, "RUNNING", steps={})
        _write_jsonl(disc, [_started(D, "discovery", 1),
                            _chat("assistant", "Round 1 — which option?", 2)])
        return ["Round 1 — which option?"]

    def m1():  # discovery pauses awaiting the answer
        _append_jsonl(disc, [_chat("assistant", "Awaiting your choice…", 3)])
        _write_engine(root, flow_id, "PAUSED", steps={})
        return ["Awaiting your choice…"]

    def m2():  # operator answers; resume (still no steps table)
        _append_jsonl(disc, [_chat("user", "1", 3), _started(D, "discovery", 3)])
        _write_engine(root, flow_id, "RUNNING", steps={})
        return ["1"]

    def m3():  # discovery confirm round — pause again
        _append_jsonl(disc, [_chat("assistant", "Confirm the plan?", 4)])
        _write_engine(root, flow_id, "PAUSED", steps={})
        return ["Confirm the plan?"]

    def m4():  # *** THE BOUNDARY *** steps table FIRST-WRITE + PAUSED→RUNNING
        _append_jsonl(disc, [_chat("user", "按1确定", 5)])
        _write_engine(root, flow_id, "RUNNING", steps=_STEPS_TABLE,
                      current_step_index=0)
        return ["按1确定"]

    def m5():  # discovery completes + a NEW 02_analyze jsonl is created
        _append_jsonl(disc, [_completed(D, "discovery", 6)])
        _write_engine(root, flow_id, "RUNNING", steps=_STEPS_TABLE,
                      current_step_index=1)
        _write_jsonl(anal, [_started(A, "analyze", 7),
                            _chat("assistant", "Analyzing the spec…", 8)])
        return ["Analyzing the spec…"]

    def m6():  # analyze keeps producing — the mid-step content that "disappears"
        _append_jsonl(anal, [_chat("assistant", "Found 3 relevant modules.", 9)])
        return ["Found 3 relevant modules."]

    def m7():
        _append_jsonl(anal, [_chat("assistant", "Analysis complete.", 10)])
        return ["Analysis complete."]

    return [m0, m1, m2, m3, m4, m5, m6, m7]


def _normal_step_mutations(root: Path, flow_id: str):
    """CONTROL — an ordinary analyze→plan boundary (steps table already present,
    no PAUSED→RUNNING flip, current_step_index just increments). Must NOT
    regress: every append is delivered over the live WS.
    """
    anal = _hist(root, flow_id) / "02_analyze_cd34.jsonl"
    plan = _hist(root, flow_id) / "03_plan_ef56.jsonl"
    A = "02_analyze_cd34"
    P = "03_plan_ef56"

    def m0():
        _write_engine(root, flow_id, "RUNNING", steps=_STEPS_TABLE, current_step_index=1)
        _write_jsonl(anal, [_started(A, "analyze", 1),
                            _chat("assistant", "Analysis running…", 2)])
        return ["Analysis running…"]

    def m1():
        _append_jsonl(anal, [_chat("assistant", "Analysis complete.", 3)])
        return ["Analysis complete."]

    def m2():  # analyze→plan: same shape, index just advances, plan jsonl created
        _append_jsonl(anal, [_completed(A, "analyze", 4)])
        _write_engine(root, flow_id, "RUNNING", steps=_STEPS_TABLE, current_step_index=2)
        _write_jsonl(plan, [_started(P, "plan", 5),
                            _chat("assistant", "Drafting the plan…", 6)])
        return ["Drafting the plan…"]

    def m3():
        _append_jsonl(plan, [_chat("assistant", "Plan ready.", 7)])
        return ["Plan ready."]

    return [m0, m1, m2, m3]


# --------------------------------------------------------------------------
# The signature-GATED driver — mirrors DaemonClient._history_changed +
# _push_history (the fidelity the ungated _drive_scenario lacks).
# --------------------------------------------------------------------------


class _GatedBoundaryDriver:
    """Drive the real reader → real server broadcast through the push-loop gate.

    Reproduces ``DaemonClient``'s per-tick behaviour: compute
    ``active_flow_signature`` (``_history_changed``), and read+push a delta ONLY
    when it changed since the previous tick; on a change, rebuild the cursor map
    exactly like ``_push_history`` (retaining a resumable terminal flow's cursor
    via ``live_flow_ids``). Each pushed increment goes through the real
    ``_handle_message`` so the server cache + ``/ws/ui`` fanout are exercised.
    """

    def __init__(self, root: Path, machine_id: str = "m1", owner: str = "owner-A"):
        self.root = root
        self.machine_id = machine_id
        self.owner = owner
        self.reader = DaemonHistoryReader(project_roots_provider=lambda: [str(root)])
        self.state = ServerState()
        self.hub = UiHub()
        self.ui = _UiWS()
        self.registry = HistoryRequestRegistry()
        self._last_sig: dict = {}
        self._cursors: dict = {}

    async def setup(self) -> None:
        await self.state.register_machine(
            self.machine_id, "host", "1.0", owner_id=self.owner
        )
        await self.hub.register(self.ui, self.owner)

    async def tick(self) -> str | list:
        """One push-loop tick. Returns "DEBOUNCED" when the signature gate
        suppressed the read, else the list of ``(mode, n_records)`` pushed."""
        # --- _history_changed (the debounce gate) ---
        signature = self.reader.active_flow_signature()
        changed = signature != self._last_sig
        self._last_sig = signature
        if not changed:
            return "DEBOUNCED"
        # --- _push_history (read active-flow deltas + rebuild cursors) ---
        self.reader.invalidate_index_cache()
        reads = self.reader.read_active_flows(self._cursors)
        new_cursors = {r.flow_id: r.cursor for r in reads}
        live = set(self.reader.live_flow_ids())
        for flow_id, cursor in self._cursors.items():
            if flow_id not in new_cursors and flow_id in live:
                new_cursors[flow_id] = cursor
        self._cursors = new_cursors
        pushed: list = []
        for r in reads:
            if not r.records:
                continue
            msg = protocol.make_history_data(r.flow_id, r.mode, r.records, cursor=r.cursor)
            await _handle_message(msg, self.state, self.machine_id, self.hub, self.registry)
            pushed.append((r.mode, len(r.records)))
        return pushed or "NO_RECORDS"

    def ui_bodies(self, flow_id: str) -> list:
        """Every conversation content body that reached the live UI, in order."""
        bodies: list = []
        for frame in self.ui.history_frames(flow_id):
            for rec in frame["records"]:
                content = (rec.get("message") or {}).get("content")
                if content is not None:
                    bodies.append(content)
        return bodies


async def _run_gated_scenario(root: Path, flow_id: str, mutations) -> dict:
    """Apply each mutation, tick the gated driver, and record what the UI got.

    Returns ``{"driver", "expected_per_step": [[labels], ...],
    "delivered_after_step": [bodies-seen-so-far, ...]}``.
    """
    disk_json_cache.clear_cache()
    driver = _GatedBoundaryDriver(root)
    await driver.setup()
    expected_per_step: list = []
    delivered_after_step: list = []
    for mutate in mutations:
        labels = mutate()
        # The real push loop polls on a fast tick; a single fresh disk write
        # shifts the raw-stat signature, so one tick delivers it. A second tick
        # confirms the steady state debounces (nothing new to push).
        await driver.tick()
        second = await driver.tick()
        assert second == "DEBOUNCED", (
            f"steady-state after append should debounce, got {second!r}"
        )
        expected_per_step.append(labels)
        delivered_after_step.append(list(driver.ui_bodies(flow_id)))
    return {
        "driver": driver,
        "flow_id": flow_id,
        "expected_per_step": expected_per_step,
        "delivered_after_step": delivered_after_step,
    }


# --------------------------------------------------------------------------
# Boundary delivery — every disk append must reach the live WS within its tick.
# --------------------------------------------------------------------------


def test_boundary_each_disk_append_delivered_via_gated_push(tmp_path):
    """The core #260 assertion: across discovery→analyze, every disk append is
    delivered to a subscribed /ws/ui client within its push cycle — through the
    real signature gate — so the live chat keeps growing with NO full reload.

    This PASSES in-process, which is itself the diagnosis: because
    ``active_flow_signature`` keys on a RAW ``_safe_stat`` of engine.json and of
    each per-step jsonl, the boundary's distinct-stat writes always shift the
    signature and the delta is read + broadcast. The daemon read path, the server
    append, and the /ws/ui fanout are therefore proven correct in-process — the
    remaining #260 freeze needs the real-fs vulnerable condition reproduced by
    ``test_active_engine_json_middle_rewrite_returns_stale_parse``.
    """
    result = asyncio.run(
        _run_gated_scenario(tmp_path, "live", _boundary_mutations(tmp_path, "live"))
    )
    # Each step's appended label must be present once the driver has ticked for it.
    for i, labels in enumerate(result["expected_per_step"]):
        delivered = result["delivered_after_step"][i]
        for label in labels:
            assert label in delivered, (
                f"disk append {label!r} (step {i}) never reached the live WS; "
                f"delivered so far: {delivered}"
            )
    # The analyze mid-step bodies — the content that "disappears" in the bug —
    # arrived over the live stream, not only via a full reload.
    final = result["delivered_after_step"][-1]
    assert "Analyzing the spec…" in final
    assert "Found 3 relevant modules." in final
    assert "Analysis complete." in final

    # No full reload mid-stream: the first frame is the initial full snapshot;
    # every later frame is an append (the live chat grows incrementally).
    frames = result["driver"].ui.history_frames("live")
    assert frames[0]["mode"] == protocol.HISTORY_MODE_FULL
    assert all(f["mode"] == protocol.HISTORY_MODE_APPEND for f in frames[1:]), (
        "a mid-stream mode:full reload appeared — the live view was rebuilt "
        "instead of growing incrementally"
    )


def test_boundary_streamed_records_equal_full_snapshot_no_loss_no_dup(tmp_path):
    """The concatenation of every broadcast frame equals the authoritative full
    snapshot — nothing lost, nothing delivered twice — across the boundary."""
    result = asyncio.run(
        _run_gated_scenario(tmp_path, "live", _boundary_mutations(tmp_path, "live"))
    )
    driver = result["driver"]
    streamed: list = []
    for frame in driver.ui.history_frames("live"):
        streamed.extend(frame["records"])
    snap = asyncio.run(
        driver.state.get_history_snapshot("live", expected_machine_id="m1")
    )
    assert streamed == snap["records"]
    keyed = [
        (r["step_id"], json.dumps(r["message"], sort_keys=True, ensure_ascii=False))
        for r in streamed
    ]
    assert len(keyed) == len(set(keyed)), "a record was broadcast twice"


def test_normal_step_boundary_not_regressed(tmp_path):
    """CONTROL: an ordinary analyze→plan boundary keeps streaming every append.

    The user reported other step boundaries work; this locks that the control
    path is delivered by the SAME gated harness, so a future fix cannot regress
    it.
    """
    result = asyncio.run(
        _run_gated_scenario(tmp_path, "live", _normal_step_mutations(tmp_path, "live"))
    )
    final = result["delivered_after_step"][-1]
    assert "Analysis running…" in final
    assert "Analysis complete." in final
    assert "Drafting the plan…" in final
    assert "Plan ready." in final


# --------------------------------------------------------------------------
# The confirmed daemon-side latent hazard: disk_json_cache stale parse for the
# LIVE engine.json under a same-(mtime,size) MIDDLE rewrite — the design's primary
# #260 fix target. G1 reproduced it as an ``xfail``; G2 hardened the cache to hash
# the WHOLE content, so this now PASSES as a permanent regression guard.
# --------------------------------------------------------------------------


def _build_large_engine(marker: int) -> str:
    """A >128 KiB indent=2 engine.json whose ONLY variable byte-run is a marker
    buried in the TRUE MIDDLE of the steps table — beyond the 64 KiB head window
    AND before the 64 KiB tail window ``disk_json_cache`` hashes.

    Everything the head window sees (``flow_id`` / ``status``) and everything the
    tail window sees (``current_step_index`` and the trailing worktree keys) is
    held byte-for-byte constant, because those are exactly the fields the verify
    window DOES catch. Only a single mid-table step's marker changes, so a
    same-size rewrite is invisible to the head+tail window — the narrow-but-real
    staleness condition (a step-status flip deep in a large legacy engine.json).
    """
    steps: dict = {}
    for i in range(3000):
        blob = "x" * 40
        if i == 1500:  # middle of the file — outside both 64 KiB verify windows
            blob = "MID%04d" % marker + "x" * 33
        steps["%04d_step" % i] = {"status": "pending", "blob": blob}
    obj = {
        "flow_id": "F1",
        "status": "RUNNING",
        "task_description": "td",
        # current_step_index is INTENTIONALLY constant: it lands in the tail
        # window (after the steps table), which the verify hash covers, so
        # varying it would defeat the reproduction. The real vulnerable rewrite
        # is a deep-in-the-table step-status change that leaves head+tail intact.
        "state": {"steps": steps, "current_step_index": 0},
        "is_worktree_mode": False,
    }
    return json.dumps(obj, indent=2)


def test_active_engine_json_middle_rewrite_returns_fresh_parse(tmp_path):
    """The #260 primary fix, locked: a same-``(st_mtime_ns, st_size)`` rewrite that
    differs ONLY in the file's middle must return the FRESH parse.

    Before G2 the head+tail verify window was byte-identical, so
    ``read_engine_header(active=True)`` reused the first parse and served the
    SUPERSEDED middle — a daemon reading the live engine.json in the dense
    discovery→analyze rewrite window acted on stale flow state (the confirmed
    freeze root cause). G2 now hashes the whole content, so the middle change is
    caught and the fresh parse is returned. (G1 shipped this as a strict-``xfail``
    reproduction; it is now a permanent regression guard.)
    """
    ej = tmp_path / "engine.json"
    first = _build_large_engine(0)
    ej.write_text(first, encoding="utf-8")
    assert ej.stat().st_size > 128 * 1024, "engine.json must exceed the verify window"
    disk_json_cache.clear_cache()

    d1 = disk_json_cache.read_engine_header(ej, active=True)
    st = os.stat(ej)

    second = _build_large_engine(1)
    assert len(first) == len(second), "the rewrite must preserve the byte size"
    ej.write_text(second, encoding="utf-8")
    # Force the two writes to share an mtime tick (coarse-mtime filesystems and
    # two fast writes on ext4 do this naturally; here we make it deterministic).
    os.utime(ej, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(ej).st_mtime_ns == st.st_mtime_ns
    assert os.stat(ej).st_size == st.st_size

    d2 = disk_json_cache.read_engine_header(ej, active=True)
    mid1 = d1["state"]["steps"]["1500_step"]["blob"][:7]
    mid2 = d2["state"]["steps"]["1500_step"]["blob"][:7]
    assert mid1 == "MID0000"
    # The daemon observes the fresh middle (MID0001): G2's whole-content freshness
    # hash catches the middle rewrite the old head+tail window masked.
    assert mid2 == "MID0001", (
        f"disk_json_cache served a STALE parse for the live engine.json: read "
        f"{mid2!r}, expected the freshly-written MID0001"
    )


def test_active_flow_signature_masks_engine_middle_rewrite(tmp_path):
    """Documents WHY the staleness is masked in the common case (and where it is
    NOT): ``active_flow_signature`` keys engine.json on a RAW ``_safe_stat``, so a
    same-``(mtime, size)`` middle rewrite leaves the signature UNCHANGED — the
    push loop debounces that tick. When such an engine-only tick is the only
    change (no jsonl append), the delta read is skipped; the confirmed staleness
    then decides what the daemon believes about the flow. This is the concrete
    interaction G2 must close, captured here as a regression anchor.
    """
    root = tmp_path
    flow_id = "F1"
    disk_json_cache.clear_cache()
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(root)])
    # A >128KiB live engine.json + one active jsonl so the flow is in the sig.
    (root / "se3" / "state").mkdir(parents=True, exist_ok=True)
    ej = root / "se3" / "state" / "engine.json"
    ej.write_text(_build_large_engine(0), encoding="utf-8")
    hist = _hist(root, flow_id)
    _write_jsonl(hist / "02_analyze_cd34.jsonl", [_chat("assistant", "hi", 1)])

    sig1 = reader.active_flow_signature()
    st = os.stat(ej)
    ej.write_text(_build_large_engine(1), encoding="utf-8")  # same size, middle-only
    os.utime(ej, ns=(st.st_atime_ns, st.st_mtime_ns))        # same mtime tick
    sig2 = reader.active_flow_signature()

    # The raw-stat engine part is identical, and no jsonl changed → the signature
    # is unchanged, so client._history_changed would DEBOUNCE this tick.
    assert sig1 == sig2, (
        "expected the same-(mtime,size) middle rewrite to leave the raw-stat "
        "signature unchanged (the debounce that masks the staleness)"
    )


def _build_large_worktree_engine(marker: int, branch: str) -> str:
    """Like :func:`_build_large_engine` but a ``--worktree``-mode engine.json: the
    worktree tail keys (``worktree_branch`` / ``worktree_path``) trail the giant
    steps table, exactly where the removed 64 KiB tail window used to sit.
    """
    steps = {}
    for i in range(3000):
        blob = "x" * 40
        if i == 1500:
            blob = "MID%04d" % marker + "x" * 33
        steps["%04d_step" % i] = {"status": "pending", "blob": blob}
    obj = {
        "flow_id": "F1",
        "status": "RUNNING",
        "task_description": "td",
        "state": {"steps": steps, "current_step_index": 0},
        "is_worktree_mode": True,
        "worktree_branch": branch,
        "worktree_path": "/tmp/wt/" + branch,
    }
    return json.dumps(obj, indent=2)


def test_worktree_engine_middle_rewrite_fresh_and_tail_keys_preserved(tmp_path):
    """--worktree non-regression: the whole-content freshness hash (which replaced
    the head+tail window) must both catch a middle rewrite AND still surface the
    worktree tail keys on the live read.

    The removed tail window is exactly where ``worktree_branch`` / ``worktree_path``
    live, so a naive fix could regress worktree visibility. Because the fix
    re-parses the WHOLE file on a real change, every top-level key — worktree tail
    keys included — is returned, and the middle marker is fresh.
    """
    ej = tmp_path / "engine.json"
    first = _build_large_worktree_engine(0, "feat-a")
    ej.write_text(first, encoding="utf-8")
    assert ej.stat().st_size > 128 * 1024
    disk_json_cache.clear_cache()

    d1 = disk_json_cache.read_engine_header(ej, active=True)
    assert d1["is_worktree_mode"] is True
    assert d1["worktree_branch"] == "feat-a"
    st = os.stat(ej)

    second = _build_large_worktree_engine(1, "feat-a")
    assert len(first) == len(second)
    ej.write_text(second, encoding="utf-8")
    os.utime(ej, ns=(st.st_atime_ns, st.st_mtime_ns))  # same (mtime, size)

    d2 = disk_json_cache.read_engine_header(ej, active=True)
    # Fresh middle AND the worktree tail keys still present (no regression).
    assert d2["state"]["steps"]["1500_step"]["blob"][:7] == "MID0001"
    assert d2["is_worktree_mode"] is True
    assert d2["worktree_branch"] == "feat-a"
    assert d2["worktree_path"] == "/tmp/wt/feat-a"
