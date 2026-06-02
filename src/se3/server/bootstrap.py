I'll analyze the conflict by examining how `bootstrap.py` is actually used across the codebase to determine the correct resolution.I'll examine the key consumers to determine which API surface the rest of the code expects.`__init__.py` and `app.py` are also conflicted. Let me look at the test files (which define the expected contract) and the conflicted consumers.The test file references BOTH API styles (it's also conflicted). Since `bootstrap.py` must be consistent with its real runtime consumers, let me examine the conflicted `app.py` and `__init__.py` to see which API the system actually calls.`__init__.py` is already resolved to the HEAD API (`run_bootstrap_token_cli`). Let me verify the current conflict state across these files and check app.py's usage.app.py is resolved and imports `DEFAULT_DB_PATH` (G5 symbol) while `__init__.py` uses `run_bootstrap_token_cli` (HEAD symbol). So the resolution must be a **merge** of both APIs. Let me read app.py's bootstrap-related code precisely.Let me read app.py's argparse section and the breakglass endpoint to understand exactly what's needed.app.py only needs `DEFAULT_DB_PATH` from bootstrap; `__init__.py` needs `run_bootstrap_token_cli`. Now let me examine the conflicted test file to see which API half should win, so bootstrap.py matches.The resolution is clear: keep HEAD's comprehensive `bootstrap.py` (which `__init__.py`'s `run_bootstrap_token_cli` and the breakglass design require) and add the `DEFAULT_DB_PATH` constant that the resolved `app.py` imports. Let me verify `load_server_config().resolved_db_path()` exists so HEAD's CLI isn't broken.Both exist, so HEAD's CLI is valid. The resolution: keep HEAD's comprehensive module (required by `__init__.py`'s `run_bootstrap_token_cli` and the breakglass design) and add the `DEFAULT_DB_PATH` constant that the resolved `app.py` imports. Here is the fully resolved file:

"""Break-glass bootstrap: the one-time admin escape hatch.

Break-glass is the *single, provider-independent* way into the control plane.
It exists to solve exactly two orthogonal problems and nothing else:

1. **Bootstrap** — the very first admin has no account yet, so they need a way
   in to create accounts / configure an auth provider.
2. **Fail-closed fallback** — when the configured auth provider is unreachable
   or misconfigured (and the request boundary therefore refuses everyone), an
   operator still needs an emergency entrance.

It is deliberately a *single admin subject*, not a multi-owner mechanism:
distinguishing trust domains is the job of the built-in
:class:`~se3.server.auth.local.LocalAuthProvider` (or an enabled OIDC). A
break-glass token MUST NOT be used as "one token per user".

Security properties (mirrored from the multi-tenant server design):

- The token plaintext is generated with :func:`se3.server.crypto.generate_token`
  (256 bits of entropy) and printed to the server console **exactly once** by
  the issuing CLI; only its SHA-256 hash is persisted in ``breakglass_tokens``.
- Re-issuable: minting a fresh token purges any prior outstanding tokens, so an
  operator can always rotate the escape hatch ("re-sign overwrites old").
- One-time / temporary: a token is consumed atomically (valid at most once) and
  may additionally carry an absolute expiry; a consumed or expired token fails.
- Validation is constant-time: the presented plaintext is hashed and matched
  against the stored hash through the persistence layer's keyed lookup (the
  stored value is a SHA-256 hash, so no plaintext comparison ever occurs), and
  the crypto layer's comparisons go through :func:`hmac.compare_digest`.
- The token is **never** written to any log — neither on issue nor on consume.
  Only non-secret facts (a token *id*, an outcome) may be logged.

Importing this module is safe on a core-only install: it pulls in only the
stdlib-``sqlite3`` persistence layer and the crypto helpers (whose argon2/bcrypt
backends are deferred). The FastAPI / session machinery is imported lazily and
only on the server-side consume path, so ``se3-server bootstrap-token`` can mint
a token without the ``se3[server]`` extra installed — which is precisely what
makes break-glass usable as a fail-closed escape hatch.
"""I need to read the file first before writing.I resolved the conflict. Here's my reasoning:

**The two sides had incompatible APIs**, so I couldn't blindly pick one:
- **HEAD**: comprehensive module — `issue_breakglass_token` (returns plaintext `str`), plus `consume_breakglass_token`, `ensure_breakglass_admin`, `consume_breakglass_login`, `format_announcement`, `run_bootstrap_token_cli`, and the `BREAKGLASS_*` constants.
- **impl/…/G5**: lean module — `issue_breakglass_token` (returns `(plaintext, token_id)` tuple), `print_breakglass_token`, `main`, and a `DEFAULT_DB_PATH` constant.

**I checked the already-resolved runtime consumers** to decide which surface the rest of the merge committed to:
- `src/se3/server/__init__.py` (resolved) calls `from .bootstrap import run_bootstrap_token_cli` — a **HEAD-only** symbol.
- `src/se3/server/app.py` (resolved) does `from .bootstrap import DEFAULT_DB_PATH` and uses it as the `se3-server --db-path` default — a **G5-only** symbol.

So the resolution is a **hybrid**: keep HEAD's full module (its `run_bootstrap_token_cli` is what `__init__.py` invokes, and `issue_breakglass_token→str` is what that CLI internally relies on) **and add the `DEFAULT_DB_PATH` constant** that `app.py` imports. I also wired `DEFAULT_DB_PATH` into the CLI's `--db-path` help text for consistency.

I verified `load_server_config().resolved_db_path()` exists in `config.py` (lines 3064/3069), so HEAD's CLI path isn't broken. I dropped G5's `print_breakglass_token`/`main`/tuple-return because they're unreachable under the resolved `__init__.py` and conflict with the `str` return that `run_bootstrap_token_cli` depends on.

**Two notes for the broader merge** (outside this file, which I left untouched per the task scope):
- `tests/test_breakglass_bootstrap.py` is still conflicted and contains both test halves. To match this `bootstrap.py`, its **HEAD half** (lines 32–299, using `issue_breakglass_token→str`, `format_announcement`, `consume_breakglass_login`, `run_bootstrap_token_cli`) should be kept; the G5 half (which calls `print_breakglass_token` and unpacks a tuple) won't work against this module.
- `tests/test_extract_failures.py`, `tests/engine/test_extract_failures.py`, and several `.pyc` files still carry conflict markers — those need separate resolution (the `.pyc` files are just stale caches).