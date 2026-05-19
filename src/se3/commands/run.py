Resolved the merge conflict in `src/se3/commands/run.py`.

The conflict was in the FAILED-step recovery block of `_run_flow_impl`, where two branches independently implemented "no-TTY → externalize the retry/skip/abort decision":

- **HEAD** used `_resolve_step_failure_action()` — write a `retry_decision` call file, pause the flow, and exit (resume picks up the answer).
- **G2** used `_handle_step_failure_noninteractive()` — write the call file and block-poll in-process until answered.

I kept HEAD's `_resolve_step_failure_action` approach because:
1. HEAD is accumulated `master` (G3/G6/G1 already merged); G2 re-implements the same feature.
2. Its pause-and-exit pattern matches the established `_handle_discovery_pause_noninteractive` flow in the same file (write call file → `FlowStatus.PAUSED` → return).

Two adjustments were needed for a working result:
- The common code after the conflict checks `decision` (a `"retry"/"skip"/"abort"` string), but HEAD's block produced `choice` (an int). I bridged this: `decision = info` on the `"decision"` branch, and `decision = ("retry","skip","abort")[choice]` on the interactive branch.
- Added an `emitter.emit(FLOW_PAUSED)` on the pause path, consistent with every other PAUSED-return path in `_run_flow_impl` (so `JsonSink`/daemon learn the flow paused).

`_handle_step_failure_noninteractive` (G2's helper) is now unreachable but left in place — it sits in a non-conflicted region; removing it is a separate cleanup.

Note: the other two conflicted files in this merge — `src/se3/engine/interaction_calls.py` and `tests/test_interaction_calls.py` (both `AA`) — still need resolution; this task only covered `run.py`.