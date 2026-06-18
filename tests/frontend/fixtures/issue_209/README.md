# issue #209 — real-frame fixture (WebUI console freeze on discovery→analyze / retry)

These are **real** artifacts captured from a real `se3 run` (fake deterministic
agent) that ran `discovery → analyze → plan`, with `plan` failing into a
`retry_decision` pause — i.e. it exercises **both** issue-#209 trigger scenarios
in one flow:

* the **discovery→analyze transition** (discovery pauses for plan confirmation,
  then resumes into analyze — `01_discovery` gets a second `step_started`
  running anchor + `step_completed`, and `02_analyze` is a brand-new jsonl), and
* a **step error + manual retry** boundary (`03_plan` ends in `step_failed`).

## Files

* `01_discovery_*.jsonl`, `02_analyze_*.jsonl`, `03_plan_*.jsonl` — the
  **authoritative on-disk records** exactly as `engine/chat_history.py` wrote
  them (append-only; envelopes carry only `{role,content,timestamp}` and the
  lifecycle anchors are flat `type`-tagged dicts — **no** `step_type` field; the
  daemon injects the authoritative `step_type` from the file-name). Use these to
  replay through the **real** `DaemonHistoryReader.read_flow` /
  `read_active_flows` (the G3 daemon-layer regression).
* `daemon_frames.json` — the **real daemon frame sequence** these records
  produce when read incrementally by `read_active_flows` (tick-by-tick, with the
  pause→resume burst as the daemon actually delivers it):
  * frame 0 `disc-stream-1` — `mode:full`, 3 records (discovery)
  * frame 1 `disc-paused` — `mode:append`, 3 records (discovery stream + paused)
  * frame 2 `resume-burst` — `mode:append`, **14 records** carrying the
    discovery resume `step_started`/`step_completed` **and all of analyze and
    plan**.

  Use this to replay through the production `app.js` `applyHistoryData` /
  `dedupeAppendRecords` (the G3 frontend-layer guard).

## What the fixture proves (G1 diagnosis)

Replaying these **real** frames shows the daemon read path, the frontend
`dedupeAppendRecords`, and `renderConversation` (incremental vs full) all handle
the transition/retry batch **correctly** — analyze+plan appear, incremental ==
full. The freeze is therefore **not** in frame content handling. See
`tests/ISSUE_209_FREEZE_DIAGNOSIS.md` for the坐实ed root cause (daemon
push-loop starvation under load) and `tests/repro_issue_209_freeze.py` for the
end-to-end reproduction that does fail.
