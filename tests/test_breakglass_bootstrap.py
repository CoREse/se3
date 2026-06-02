I need to inspect the actual implementation to know which API the tests should target. Let me examine the real source files.The implementation matches the HEAD side. Let me confirm the endpoint response shape and `create_app` signature.Let me verify the `SessionStore` constructor and check git history of the HEAD test to understand which is current.I now have full clarity. The real `bootstrap.py` matches the HEAD side exactly (`issue_breakglass_token` returns a plaintext `str`, prefix `se3bg`, `consume_breakglass_token`, `format_announcement`, `ensure_breakglass_admin`, `BREAKGLASS_ADMIN_OWNER_ID`, `consume_breakglass_login`, `run_bootstrap_token_cli`). The G5 side uses APIs that don't exist (`print_breakglass_token`, tuple return, `bg_` prefix).

The only drift: HEAD's endpoint tests predate the final `app.py`. The real `create_app` takes `session_store=` (not `sessions=`), and the breakglass endpoint returns `{"owner_id", "is_admin", "provider"}` (no `"status"`, and `is_admin` not `admin`). I'll resolve to the HEAD structure with those three corrections so the file is consistent with the merged implementation.Now let me verify the resolution passes against the actual implementation:Resolved and verified — all 17 tests pass against the actual implementation.

**Resolution summary:** The two sides were divergent test suites for the same break-glass feature. I checked the real `bootstrap.py`, `crypto.py`, and `app.py`:

- The **HEAD (G4)** side matches the actual implementation API (`issue_breakglass_token` → plaintext `str`, prefix `se3bg`, `consume_breakglass_token`, `format_announcement`, `ensure_breakglass_admin`, `consume_breakglass_login`, `run_bootstrap_token_cli`).
- The **G5/G10** side used APIs that don't exist in the code (`print_breakglass_token`, `(plaintext, token_id)` tuple return, `bg_` prefix) — it would fail to run.

So I took the HEAD structure, and corrected its three points of drift against the *final merged* `app.py`/endpoint contract:
1. `create_app(... sessions=...)` → `session_store=` (the real kwarg).
2. Dropped the nonexistent `body["status"] == "ok"` assertion.
3. `body["admin"]` → `body["is_admin"]` (the real response key).

The empty-token (`422`) and bad-token (`401`) expectations already matched the live endpoint.