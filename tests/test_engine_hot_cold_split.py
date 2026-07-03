This HEAD side is not real code — it's corrupted agent narrative (the known merge-narrative-corruption failure mode). I need to reconstruct the actual G4/G6 test content from git rather than trust the conflict markers. Let me investigate.Confirmed: stage 2 (HEAD) is corrupted narrative — the real G4+G6 test code was destroyed by the known merge-narrative-corruption bug. I must reconstruct the genuine G4 and G6 test content from the merge parents.Both genuine versions recovered (G4: 350 lines, G6: 324 lines). Now let me trace where the corruption entered and what the correct HEAD content should be, and check whether G7 also touched this file.The corruption entered at the G4 merge (0204ef3e). The correct HEAD should be G4+G6 merged. The final resolution needs all three real suites: G4 (models), G6 (read-path), G5 (write-path). Let me pull all three and analyze name collisions.No test-name collisions across the three suites. Collisions are only in helpers: `pm` fixture (G6 & G5) and `_make_flow` (G4 & G6). Let me read all three files in full to merge cleanly.I have all three genuine suites. The corrupted HEAD was meant to be G4+G6 merged (rename `_make_flow`→`_make_models_flow`), and G5 (theirs) adds the write-path suite. No test-name collisions across the three; only helper collisions are the identical `pm` fixture (G6/G5, dedupe once) and `_make_flow` (rename G4's). Let me write the reconstructed, fully-merged file.Now I'll write the reconstructed merged file.The file is written. Let me verify it has no conflict markers and parses as valid Python.No conflict markers, valid Python. Let me confirm there are no duplicate top-level definitions that would silently shadow.The merge conflict in `tests/test_engine_hot_cold_split.py` is resolved.

## What happened

The `HEAD` side of the conflict was **not real code** — it was corrupted agent-narrative prose (a single line of English describing what the merge "should" do). This is the known merge-narrative-corruption failure mode. The actual G4 (models-layer) and G6 (read-path) test suites that HEAD was supposed to contain had been destroyed during the earlier G4 merge (commit `0204ef3e`).

## How I resolved it

Rather than trust the poisoned conflict markers, I reconstructed the genuine content from the merge parents:
- **G4 models tests** (350 lines) from impl commit `8dd6290d`
- **G6 read-path tests** (324 lines) from impl commit `efa05df0`
- **G5 write-path tests** — the `theirs` side, which was intact

The merged file keeps all three suites (they cover different halves of Part B, zero test-name collisions), deduping the only real collisions:
- The identical `pm` fixture (defined in both G6 and G5) → kept once at the top.
- The `_make_flow` helper name collided between G4 and G6 with different signatures → renamed G4's variant to `_make_models_flow` and updated all 12 of its call sites (exactly as the corrupted narrative had intended).
- Consolidated the three import blocks into one.

Verified: no conflict markers remain, the file parses as valid Python (`ast.parse` OK), and there are no duplicate top-level `def`/`class` names.

Note: this only resolves the test file. `src/se3/engine/persistence.py` remains an unmerged conflict (`UU`) and still needs resolution before the merge can be committed — and the combined test file references symbols from several branches' APIs (`_is_hotcold`, `ENGINE_FORMAT_KEY`, `COLD_CONTEXT_FILENAME`, `_split_to_new_format`, `_is_new_format`, `CONTEXT_COLD_FILENAME`), so those will need to be reconciled in the persistence.py resolution for the suite to actually collect and pass.