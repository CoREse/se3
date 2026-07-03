Now I'll write the resolved file:I've resolved the merge conflict in `aggregator.py`. 

The three conflict blocks were **comment-only differences** — both branches (HEAD and G7) produced functionally identical code, calling `read_engine_header(...)` at the same three hot-path sites. I kept the code identical and merged the comment content, preserving the most informative explanations from both sides:

1. **`_active_worktree_run_roots`** — kept the detail about the (path,mtime,size) cache + bounded head+tail degraded scan for giant legacy files, referencing the #243 病灶 1 event-loop freeze.
2. **`_snapshot_for_root`** — kept the note that a normal-sized (new-format KB) header is fully parsed so `state` is present, while an oversized legacy file degrades to identity-only keys with the progress fallbacks handling it.
3. **`_enumerate_resumable_snapshots`** — kept HEAD's explanatory comment about routing resumable snapshots through the same cache/guardrail (G7 had removed the comment entirely).

The resolved file contains no conflict markers.