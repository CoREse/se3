sync_analyzer.py resolved. The remaining conflicted files are `sync_engine.py` and `sync_loop.py`.All three conflicts resolved and compile-clean:

- **`sync_analyzer.py`** — kept G2's `stripped` variable (HEAD's version assigned to `response` but later code used `stripped`, which would have been undefined) plus G2's fuller comment. The HEAD-side fence-stripping intent is preserved.
- **`sync_engine.py`** — `LoopResult` now keeps both G1's `format_error_specs` and G2's `obsolete_specs*` / `level_1/2/3` telemetry fields.
- **`sync_loop.py`** — kept both G1's `format_error_specs` set and G2's discovery convergence tracking (`discovery_converged`, `discovery_stable_count`).

No conflict markers remain anywhere in `src/se3/` or `tests/`.