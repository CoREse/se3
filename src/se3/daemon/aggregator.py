Resolved the merge conflict in `src/se3/daemon/aggregator.py`. Three conflict regions, all resolved toward the HEAD design which is the more complete one:

1. **`PendingCall` docstring** — kept HEAD's fuller docstring (documents `CALL_KINDS`, the four interaction kinds, and the `step_id` display field).

2. **`PendingCall` fields** — kept `kind: str = protocol.CALL_KIND_CALL`. The G6 side duplicated `prompt`/`context`/`options` (the post-conflict common block already declares them). Also removed the duplicate `prompt`/`context`/`options` keys in `to_dict()` that the merge left behind.

3. **`_enumerate_calls`** — kept HEAD's `self._parse_call_file(entry, root)`, which also recovers `step_id`. Consequently removed the now-dead G6 `_call_metadata` module function and its unused `from .protocol import CALL_KINDS` import (HEAD references `protocol.CALL_KINDS` via the `from . import protocol` import).

One thing to verify before committing: `src/se3/daemon/protocol.py` is also conflicted (`UU`) — `aggregator.py` depends on `protocol.CALL_KIND_CALL` and `protocol.CALL_KINDS` being exported there, so resolve that file consistently too.