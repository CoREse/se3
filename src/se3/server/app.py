Resolved the conflict in `src/se3/server/app.py`. Both sides agreed on the endpoint logic; the conflict was only in the return shape:

- **Kept** the G6 docstring explaining how `/interject` differs from `/respond`.
- **Kept** HEAD's `JSONResponse` return type with `status_code=202` — consistent with the sibling `publish_flow` endpoint, since interjection is a fire-and-forget dispatch (the daemon turns it into a call file drained later).

Both conflict regions resolved; no markers remain.