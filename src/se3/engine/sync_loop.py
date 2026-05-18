Resolved the merge conflict in `src/se3/engine/sync_loop.py` by combining both branches:

- **`__init__` signature** — kept both `force` (G5/level-1/2 cache) and `confirm_cleanup` (G6/obsolete cleanup) parameters.
- **Round loop** — kept HEAD's discovery `do_discovery` logic and `round_skip` (level-2 cache hits ∪ per-spec converged), and G6's `round_spec_deps` construction from accumulated deps.
- **`run_once` call** — passes `do_discovery`, `skip_specs=round_skip`, and `spec_deps=round_spec_deps`.
- **Methods** — kept both HEAD's `_write_sync_state`/`_engine_specs` and G6's `_check_all_deps_missing`/`_update_obsolete_candidates`.
- **Integration fix** — wired `obsolete_candidates` into `_write_sync_state` so `SyncState.obsolete_specs` is populated at convergence (HEAD's original `obsolete_specs=[]` placeholder noted "handled in G8"; G6 supplies the data, so I threaded it through rather than leaving it empty).