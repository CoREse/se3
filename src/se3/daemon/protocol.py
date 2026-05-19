`src/se3/daemon/protocol.py` is resolved — no conflict markers remain.

Both conflict regions involved adding `MSG_INTERJECT_FLOW`. Resolution:

1. **Message-type constant** — kept G6's version (the constant plus its docstring comment). I dropped HEAD's duplicate `CALL_KIND_*` block because the identical constants already exist further down the file under the clean "interaction-call kinds" section; keeping HEAD's block would have defined `CALL_KIND_CALL`/`CALL_KINDS` twice.

2. **`make_interject_flow` docstring** — merged both: kept HEAD's "same content a local operator would type at the Ctrl-C prompt" phrasing, but used G6's accurate sink description (`interjection`-kind call file under `se3/calls/`, drained at the next step boundary), which matches the clean `CALL_KIND_INTERJECTION` semantics elsewhere in this file.

The other conflicted files (`aggregator.py`, `client.py`, `server/app.py`) still need resolution if you want me to continue.