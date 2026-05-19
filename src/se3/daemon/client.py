Resolved the merge conflict in `src/se3/daemon/client.py`:

- **`_handle_interject` conflict** — kept the G6 side, which routes through `self._interject_handler`. This is consistent with the constructor (`self._interject_handler = _default_interject_handler`) and the unified `engine/interaction_calls.py` backend that `run.py` drains from. The HEAD side bypassed `_interject_handler` and wrote to `se3/state/interjections/`, a path the unified drain logic doesn't read.
- **Orphaned `_resolve_interject_root` method** — dropped (it was only used by the discarded HEAD `_handle_interject`).
- **Orphaned `_write_interjection_request` free function** — removed; it was dead code once HEAD's handler was discarded.
- **Duplicate `MSG_INTERJECT_FLOW` dispatch branch** — a bad auto-merge artifact in `_dispatch` had the branch twice; removed the second copy after `MSG_HISTORY_REQUEST`.

Verified: no conflict markers remain and the file parses cleanly.

Note: `aggregator.py`, `protocol.py`, and `server/app.py` are also marked `UU` in the merge — they still need resolving, but only `client.py` was provided here.