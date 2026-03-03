# SE3 Framework Version History

## Current Version

**3.17.5** — feat: Stream full text and thinking content in real-time during LLM calls. Text content streams directly, thinking content streams in gray italic style.

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 3.17.5 | 2026-03-03 | feat: Stream full text and thinking content in real-time during LLM calls. Text content streams directly, thinking content streams in gray italic style. |
| 3.17.4 | 2026-03-03 | fix: Fix JSON parsing bug in EXTRACT and TWO_PHASE modes. Return plain JSON string instead of stream-json wrapper. Remove wall time timeout from all LLM calls, use only inactivity timeout (30 minutes). |
| 3.17.0 | 2026-03-03 | feat: Three JSON extraction modes (STRICT/EXTRACT/TWO_PHASE) for LLM output handling. STRICT mode forces JSON with retry. EXTRACT mode uses LLM extraction on failure without retry. TWO_PHASE mode uses natural generation + LLM extraction for large outputs. Summarize and project_summary steps now use plain text output instead of JSON. Updated flow-engine spec with JSON mode documentation. |
| 3.16.0 | 2026-03-03 | feat: Implement full-content display system for LLM outputs. New display.py module with render_full(), render_proposal(), render_design(), render_spec_content(). New output.py module with truncation-free formatting. Updated run.py and cli.py to use full-content display. |
| 3.15.0 | 2026-03-03 | feat: Remove wall timeout limit and extend inactivity timeout to 30 minutes for se3 run. Fix version display and commit step output propagation. |
| 3.14.0 | 2026-03-02 | feat: Fix retry context inheritance in implement step. Add complete version management spec and base_spec template. Add --bump flag to se3 commit command. |
| 3.13.0 | 2026-03-02 | feat: Add DocumentationUpdater class for README.md and VERSIONS.md updates. Add version bump integration in commit step. Add load_session_config, load_confirmation_config, load_claude_commands, get_language_labels, is_chinese_language functions to config.py. |
| 3.12.0 | 2026-03-02 | fix: Chat history retry context now preserves full conversation structure. Extracts tool calls and results from raw NDJSON instead of using simplified summaries. Each user message has independent tool_results. Supports both snake_case and camelCase field names. Proper field access with defaults to prevent KeyError. |
| 3.11.0 | 2026-02-28 | feat: Add version bumper framework with auto-bump on se3 run commit step. Supports pyproject.toml, package.json, version.py, setup.py. Task type -> bump level mapping (feature->minor, bugfix->patch, breaking->major). Fix JSON parser truncated response handling. Fix Version dataclass order conflict. |
| 3.10.1 | 2026-02-27 | fix: Commit step now handles None values in `changes_made` and `proposal` inputs using `or {}` fallback. Prevents AttributeError when previous steps don't produce these outputs. |
| 3.10.0 | 2026-02-27 | feat: `se3 run` without arguments enters interactive multiline input mode. Supports paste detection (>3 lines abbreviates display). Fix task group handling for directive tasks: create default task group when PLAN_TASKS is skipped, create minimal design doc when DESIGN is skipped. Fix undefined variable bug in plan_tasks.py. |
| 3.9.0 | 2026-02-27 | feat: Task group architecture for plan_tasks and implement steps. plan_tasks now generates `task_groups` (logical task groups with group_id, name, description, group_order, depends_on). implement step executes each group in separate LLM call with isolated context - same base context (design_doc, proposal, project_context) but only group's tasks. Each group has independent retry mechanism with max_retries. Sequential execution with immediate file application per group. Backward compatible with legacy task_list format. Updated STEP_POOL definitions for plan_tasks and implement. |
| 3.8.0 | 2026-02-27 | feat: Confirmation steps with configurable human or LLM review. New `CONFIRM` step type inserted after `propose`, `design`, and optionally `plan_tasks`. Human reviewer mode creates MCP call files and listens for file edits or CLI input (y=approve, n=abort, anything else=revision feedback). LLM reviewer mode uses separate LLM calls to evaluate completeness, spec compliance, and maintainability. Supports review loop: if changes requested, flow returns to original step with feedback for revision. New `confirmation` config in se3.yaml with `enabled`, `steps`, `reviewer`, `llm_reviewer` options. Updated propose/design/plan_tasks handlers to support revision mode. 3 new step handlers, state machine supports review iteration tracking. |
| 3.7.0 | 2026-02-27 | feat: Chat history system. New `ChatMessage`/`ChatSession` data model records every LLM prompt and response to `se3/history/{flow_id}/{step_id}.jsonl`. LLMCaller now accepts `flow_id`/`step_id`/`step_type` and automatically records all calls. On retry, previous conversation context is injected into the prompt. New `se3 history` CLI command for browsing chat history (list flows, view step conversations, JSON/text output). All 10 LLM-using step handlers updated. 24 new tests. Updated flow-engine and se3-commands specs. |
| 3.6.0 | 2026-02-27 | feat: Base spec mechanism. New `se3 init` command generates project structure and `se3/specs/base/spec.md` from template. `read_spec` step auto-loads base spec before LLM selection, ensuring all flows have access to project-level conventions. New `src/se3/templates/` package with base spec skeleton. 9 new tests. |
| 3.5.0 | 2026-02-27 | feat: LLM-driven read_spec replaces keyword matching. New PROJECT_SUMMARY step generates project context summary for downstream LLM steps. New `se3 summary` CLI command. ProjectContextCollector collects git, flow engine, backlog, specs. Migrated roadmap.md to se3/specs/_backlog/ (9 backlog items). Updated propose/design prompts with project context. |
| 3.4.11 | 2026-02-26 | feat: improved spec matching with expanded keywords. Add comprehensive keyword-to-spec mapping covering all 17 specs (agent-team, se3-commands, se3-config, etc.). Add word boundary matching to prevent partial matches (e.g., "about" matching "agent"). Add fallback content-based matching when keyword matching finds no results. |
| 3.4.10 | 2026-02-26 | fix: se3 run hangs due to stdin inheritance. Add `stdin=subprocess.DEVNULL` to prevent subprocess from waiting for input. Add `require_json` parameter to LLMCaller.call() with automatic JSON format retry logic. When LLM doesn't return valid JSON, automatically prompt it to retry with JSON format. |
| 3.4.9 | 2026-02-26 | feat: real-time stream-json progress output in se3 run. Add StreamJSONTracker class to process each NDJSON line immediately, printing live progress including text chunks, tool calls, and tool results. Users can now see Claude Code's progress in real-time instead of waiting for all output. |
| 3.4.8 | 2026-02-26 | fix: simplify stream-json parsing logic. Parse NDJSON line by line, collect text from assistant messages only. Remove redundant extraction functions. All steps now work correctly with stream-json format. |
| 3.4.7 | 2026-02-26 | fix: add `--verbose` flag required for `--output-format stream-json`. Claude CLI requires verbose mode when using stream-json format. Fix exit code 1 error. |
| 3.4.6 | 2026-02-26 | fix: extract only assistant message text from stream-json. Remove `result` type extraction to avoid appending non-JSON text to valid JSON responses. Fix JSON parsing when result summary was being appended to assistant's JSON output. |
| 3.4.5 | 2026-02-26 | fix: handle single-line stream-json format. Remove `len(lines) < 2` check in `_extract_from_stream_json()` to properly parse single-line stream-json events. All JSON parser test cases now pass. |
| 3.4.4 | 2026-02-26 | feat: properly implement stream-json parsing. Add `_extract_from_stream_json()` to extract text content from stream-json format. Rewrite `parse_json_response()` to handle stream-json, NDJSON, and single JSON formats correctly. |
| 3.4.3 | 2026-02-26 | fix: remove `--output-format stream-json` due to parsing compatibility issues. Revert to standard text format which works correctly with existing JSON parsing. Add debug logging for response diagnosis. |
| 3.4.2 | 2026-02-26 | fix: handle NDJSON (newline-delimited JSON) format from `--output-format stream-json`. Add `_parse_ndjson()` function to extract valid JSON from stream output. Fix JSON parsing error that caused "Failed to parse LLM response". |
| 3.4.1 | 2026-02-26 | feat: add `--output-format stream-json` and `--verbose` to Claude CLI calls for streaming JSON output. Change retry behavior: exit immediately on max retries reached instead of prompting user. |
| 3.4.0 | 2026-02-26 | feat: simplify version management — single source of truth from pyproject.toml, dynamic version loading in `__init__.py`, remove duplicate SE3_FRAMEWORK_VERSION definition. feat: add step execution timing display in `se3 run` (shows duration in seconds after each step completes). Update version checks in commit, version, and utils modules to use pyproject.toml. |
| 3.3.6 | 2026-02-26 | feat: centralized robust JSON parsing utility in `src/se3/engine/utils/json_parser.py`. Refactor all 8 step handlers (analyze, propose, design, plan_tasks, implement, verify_spec, summarize, update_spec) to use the centralized JSON parser, improving resilience against malformed LLM outputs. |
| 3.3.5 | 2026-02-26 | fix: add missing `from pathlib import Path` in propose.py and plan_tasks.py, fixing NameError on `Path.cwd()` during step execution. |
| 3.3.4 | 2026-02-26 | fix: enforce step dependency constraints — auto-insert PROPOSE before DESIGN when LLM's analyze step omits it, preventing "No proposal available" error. Fix missing Path import in design.py. |
| 3.3.3 | 2026-02-26 | feat: LLM response summary display (JSON keys, size, duration) after every LLM call in llm_caller. Two-layer Ctrl+C: first interrupts step and prompts for extra instruction to inject into retry, second saves state and exits. |
| 3.3.2 | 2026-02-26 | feat: summarize step now prints formatted summary to terminal including work summary, key changes, files modified, testing status, remaining work, and suggested next steps. Summary is still saved to se3/state/summary-{flow_id}.json for persistence. |
| 3.3.1 | 2026-02-26 | fix: get_project_root() now also checks for se3.yaml/se3.config.yaml to find project root when .git is not available (fixes SSH execution). Improve JSON parsing in all step handlers (analyze, design, implement, plan_tasks, propose, summarize, update_spec, verify_spec) to extract JSON from LLM responses that include extra text before or after the JSON object. |
| 3.3.0 | 2026-02-26 | refactor: SE3 3.3 pure CLI architecture. Delete output/ directory (no more template distribution). Remove init/update/sync commands (no more .claude/ writes). Consolidate .se3/ + specs/ + se3/ into unified se3/ directory. Rename se3.config.yaml to se3.yaml with legacy fallback. |
| 3.2.0 | 2026-02-26 | refactor: Migrate SE3 spec system from openspec/ to native specs/ directory. Engine reads specs/ first with openspec/specs/ fallback. Remove openspec CLI dependency from start/init. Fix pre-existing test_claude_runner failures. |
| 3.1.1 | 2026-02-25 | fix: 4 se3 run bugs. (1) Convert run from add_typer to @app.command for correct --type after positional arg. (2) Add max_retries check + EOFError defaults to Abort. (3) dashboard/status read engine.json instead of globbing flow_*.json. (4) LLMCaller strips CLAUDECODE env var. Doc: fix state file path in run.md. |
| 3.1.0 | 2026-02-25 | feat: Mark all 2.x commands as deprecated with migration guidance to `se3 run`. Add `se3 dashboard` and `se3 status --log` commands. Flow engine status detection in diagnostics. New output specs (run, sync, verify, guardrails, handoff). Clean up stale openspec changes. |
| 3.0.0 | 2026-02-24 | feat: SE3 3.0 flow engine. State machine driven workflow replaces prompt-driven agent. 11 step handlers (analyze→summarize), LLMCaller with retry, JSON state persistence, structured logging, spec index, ContextBuilder. Unified `se3 run` entry point (new/resume/loop). 41 tests. Architecture: .claude/ SE3 spec installation no longer needed. |
| 2.23.1 | 2026-02-20 | fix: Cherry-pick improvements from loop branches. Stash uncommitted changes during branch creation, fix typer.Exit handling, proper worktree validation via git worktree list, auto mode in loop collab. |
| 2.23.0 | 2026-02-20 | feat: Auto-merge loop branch with Claude after all iterations complete. Spawns Claude process to handle merge and conflict resolution automatically. Falls back to manual instructions on failure. |
| 2.22.7 | 2026-02-20 | Fix: Version substring matching bug in documentation consistency check. Uses regex with negative lookbehind/ahead to ensure distinct version matching. |
| 2.22.6 | 2026-02-20 | Fix: Non-blocking documentation checks now catch missing VERSIONS.md. |
| 2.22.5 | 2026-02-20 | Fix: Unify documentation check logic across all commands and prevent --skip-commit bypass of documentation requirements. |
| 2.22.4 | 2026-02-20 | Fix: Correct version.py get_readme_versions function and add documentation consistency check to se3 done command. |
| 2.22.3 | 2026-02-20 | Fix: Strengthen README version consistency checks. VERSIONS.md reference now required when VERSIONS.md exists; README version check is now unconditional. |
| 2.22.2 | 2026-02-20 | Fix: Correct version_updated detection in se3 commit. Now checks for +/- prefix in diff lines to distinguish actual version changes from context lines. |
| 2.22.1 | 2026-02-20 | Fix: Handle None current_version in commit check to prevent TypeError when version extraction fails. Adds explicit check for missing version extraction. |
| 2.22.0 | 2026-02-20 | Feature: README.md update check is now mandatory (blocking) when framework files change. Checks for inline version history and version reference. |
| 2.21.0 | 2026-02-20 | Feature: Move version history to VERSIONS.md, enforce VERSIONS.md update in `se3 commit` (blocking check). Simplified README.md. |
| 2.18.6 | 2026-02-20 | Fix: Collab base_branch propagation in all modes (foreground, manual, daemon). Ensures task branches correctly inherit base branch from loop branch. |
| 2.18.5 | 2026-02-20 | Fix: Collab base_branch correctly set when creating task branches from loop branch. |
| 2.18.4 | 2026-02-20 | Fix: `merge_loop_branch` now handles dirty working tree (warns user to commit/stash), restores original branch after merge, and improved `infer_loop_branch_base` with precise ancestor detection. |
| 2.18.3 | 2026-02-20 | Fix: Ensure collab task branches correctly merge to loop branch by checking out base branch before merge. Simplified base branch inference logic. |
| 2.18.0 | 2026-02-20 | Feature: SE3 Loop branch isolation and merge support. Each loop session creates a dedicated branch (se3-loop/{timestamp}) for isolation. Collab tasks branch from the loop branch. Added `se3 loop --merge <branch>` command to merge loop branch back. |
| 2.17.1 | 2026-02-20 | Fix: `se3 handoff` now auto-commits `progress.md` changes. Previously progress.md was updated but left uncommitted. |
| 2.17.0 | 2026-02-20 | **Version correction**: `.se3/` → `se3/` was incorrectly marked as breaking; this was a temp fix, not API change. Added: `se3 health` command, `--strict`/`--archive` flags, auto health checks in `se3:done`/`se3:work`. Fix: loop summary timeout 60s→300s. |
| 2.16.4 | 2026-02-20 | Fix: Increase loop inter-iteration summary timeout from 60s to 300s. |
| 2.16.3 | 2026-02-20 | Feature: Auto-run OpenSpec health checks in `se3:done` and `se3:work` commands. |
| 2.16.2 | 2026-02-20 | Feature: Add `--strict` and `--fail-on-warning` flags to `se3 health` command for stricter integrity checks. |
| 2.16.1 | 2026-02-20 | Feature: OpenSpec integrity improvements - name validation, format checks, stale change detection. |
| 2.16.0 | 2026-02-20 | Feature: Add `se3 health` command for OpenSpec integrity monitoring with directory structure, zombie changes, and naming convention checks. |
| 2.15.0 | 2026-02-20 | Change: SE3 runtime directory moved from hidden `.se3/` to visible `se3/` for human-as-MCP discoverability. Add `se3 migrate` command for migration. |
| 2.14.1 | 2026-02-19 | Test: add comprehensive tests for `se3 loop` stdin prompt delivery and Ctrl-C supplemental mode. |
| 2.14.0 | 2026-02-19 | Feat: `se3 loop` Ctrl-C supplemental mode now interrupts Claude and restarts with updated prompt. Press Ctrl-C once to enter supplemental mode, type your additional prompt, and Claude restarts immediately with the new context. Empty input continues without changes. |
| 2.13.2 | 2026-02-19 | Fix: `se3 loop` uses `start_new_session` to isolate Claude from Ctrl-C signals. |
| 2.12.19 | 2026-02-19 | Fix: Add missing `meta` and `off-topic` intent classification to `se3 start`. These intent types were defined in openspec but not implemented in the classifier. |
| 2.12.18 | 2026-02-19 | Fix test expectations in `test_fullcycle.py`. `sanitize_change_name` correctly handles empty strings (fallback to `loop-{timestamp}`) and slashes (converted to hyphens for filesystem safety). |
| 2.12.17 | 2026-02-19 | Fix: Use `max_tasks_per_change` from config instead of hardcoded value. |
| 2.12.16 | 2026-02-19 | Iteration 31: Comprehensive project review. Fixed test expectation mismatches in fullcycle tests. All 207 tests pass. |
| 2.11.0 | 2026-02-19 | Add `se3 loop --no-summary` flag. Iteration summary is now enabled by default — Claude Code summarizes each iteration and passes it to the next. Use `--no-summary` to disable. |
| 2.10.8 | 2026-02-18 | Fix `se3 loop`: add `--verbose` flag required for `--output-format stream-json` mode. |
| 2.10.7 | 2026-02-18 | Refactor `se3 loop`: eliminate bash script generation, run claude directly in Python with real-time stream-json rendering. Simpler and more reliable. |
| 2.10.5 | 2026-02-18 | Fix `se3 loop`: use subshell with `set +m` to properly disable job control for wrapper scripts (kclaude) that spawn claude. |
| 2.10.4 | 2026-02-18 | Fix `se3 loop`: add `set +m` to disable job control, preventing claude processes from being stopped (T state) in pipeline. |
| 2.10.3 | 2026-02-18 | Fix `se3 loop`: add `--print` flag for proper `--output-format stream-json` output, matching collab launcher configuration. |
| 2.10.2 | 2026-02-18 | Fix `se3 loop`: use correct `--output-format stream-json` instead of invalid `--stream-json` flag. |
| 2.10.0 | 2026-02-18 | Add stream-json renderer to `se3 loop`. Replaces `--print` with `--stream-json | python3 renderer` for real-time visibility of Claude's thinking, tool calls, and progress. Zero external dependencies. |
| 2.9.0 | 2026-02-18 | Add SE3 1.x features: Input Classification & Stage Routing (`se3 start -i`), Spec Guardrails (`se3 guardrails` command), full shutdown protocol with spec scenario verification and archive support. |
| 2.8.1 | 2026-02-18 | Refactor `se3 loop` - exclusive execution is now the default. Removed manual mode. Generates bash while-loop script, takes over terminal, auto-executes all iterations. Removed `--exec` flag (no longer needed). |
| 2.8.0 | 2026-02-18 | Add `se3 loop --exec` exclusive execution mode. Auto-generates bash while-loop script, takes over terminal, runs Claude Code for each iteration automatically. Supports all loop flags including `--iterations`, `--quick`. |
| 2.7.0 | 2026-02-18 | Add `se3 loop` command for running SE3 workflow repeatedly. Creates a new change for each iteration, tracks progress via `.se3-loop-state.json`, continues from interruption. Default 10 iterations, supports `--iterations`, `--quick`, and `--reset` flags. |
| 2.6.1 | 2026-02-18 | Fix collab mode detection in `se3 done` and `se3 handoff`. Interactive sessions no longer incorrectly detect as collab agents when `.collab/config.json` exists. Only `SE3_AGENT_ROLE` env var indicates collab mode. |
| 2.6.0 | 2026-02-18 | Add "Interpretation & Recommendations" section to human calls. Explains what the call is about, how to handle it, and how to respond. Supports Chinese and English. |
| 2.5.0 | 2026-02-18 | Add `check-human-responses.py` script for optimized human call detection in collab mode. Fixes issue where orchestrator couldn't detect human replies. |
| 2.4.0 | 2026-02-18 | Add `se3 full-cycle` command — runs complete start-work-done workflow in one command. Supports `--quick` flag for small tasks using 'small' workflow. Streamlines simple/quick tasks by combining session initialization, change creation, implementation, and shutdown. |
| 2.3.0 | 2026-02-17 | Merge CLAUDE.md into SE3.md, remove CLAUDE.md.template. Session Guard and simplified templates for 2.x manual trigger mode. |
| 2.2.0 | 2026-02-17 | Add Session Guard mechanism. `se3 work` and `se3 done` now check if session was started via `se3 start`. If not, return error with prompt to start session first. Simplified SE3.md.template and CLAUDE.md.template to reflect "manual trigger" philosophy (no longer expecting agent自觉性). |
| 2.1.0 | 2026-02-17 | Add Chinese language guidance to human calls context content. When language is set to Chinese (zh*), agent calls now include a note prompting humans to respond in Chinese, ensuring the entire interaction is localized. |
| 2.0.0 | 2026-02-17 | **BREAKING**: Programmatic workflow driver architecture. SE3.md reduced to ~80 lines (principles + entry points). All workflows encoded in CLI (`se3 start`, `se3 work`, `se3 done`) returning JSON actions arrays. Skills (`/se3:start`, `/se3:work`, `/se3:done`) are thin wrappers. Eliminates agent-interpreted prose in favor of CLI-driven execution. |
| 1.10.0 | 2026-02-17 | Tool-enforced progress tracking: `se3 commit` auto-appends to progress.md, `se3 handoff` generates session summaries, `se3 status` computes live state (removes status.md dependency), collab do_complete generates reports, worker FINDINGS.md convention |
| 1.9.0 | 2026-02-17 | Add `se3 handoff` command — enforces commit-before-handoff rule, supports both direct usage (auto-commits) and collab mode (creates human-call) |
| 1.8.8 | 2026-02-17 | Fix collab: archive old human-calls on startup, fix JSON extraction from manager, fix escalation output to stdout, limit manager turns to prevent verbose analysis loops |
| 1.8.7 | 2026-02-17 | Fix collab: claude_runner temp .prompt files leaked due to unreachable cleanup code |
| 1.8.6 | 2026-02-17 | Fix collab: orchestrator no longer exits when escalation happens before tasks exist |
| 1.8.5 | 2026-02-17 | Fix: se3 update no longer writes back to output/SE3.md.template, preserving one-way data flow |
| 1.8.4 | 2026-02-16 | Fix collab: detect_usage_limit() only checks last 3000 chars / 20 lines to avoid false positives from source code |
| 1.8.3 | 2026-02-16 | Fix collab: detect_usage_limit() returns False when returncode=0 to avoid false positives from source code content |
| 1.8.2 | 2026-02-16 | Fix collab: Resolve git merge conflict in claude_runner.py docstring |
| 1.8.1 | 2026-02-16 | Fix framework version management: update SE3_FRAMEWORK_VERSION to 1.8.1, add version history entry |
| 1.7.8 | 2026-02-16 | 优化 human calls 检测和处理机制：使用文件系统事件和缓存提升变更检测效率，增强响应完整性检查（重复内容检测、结构验证、长度限制），改进处理流程（批量处理、状态管理优化） |
| 1.7.7 | 2026-02-16 | Fix collab: set max-turns to 0 (unlimited) instead of arbitrary limits; rely on timeout for control |
| 1.7.6 | 2026-02-16 | Fix collab: increase manager max-turns from 3 to 10 (was hitting limit when reading files for planning) |
| 1.7.5 | 2026-02-16 | Fix collab: use `@file` syntax for prompt passing to avoid CLI parsing issues (manager/worker launchers) |
| 1.7.4 | 2026-02-16 | Fix collab: use `--output-format stream-json --verbose` for real-time output (enables activity-based timeout) |
| 1.7.3 | 2026-02-16 | Fix collab: `-p` is a flag not an option; pass prompt as se3 handoff needs auto-commit progress.md changes |
| 1.7.2 | 2026-02-16 | Fix collab: remove bash alias support (too complex), remove error_max_turns from usage limit detection |
| 1.7.1 | 2026-02-16 | Fix collab: support bash aliases/functions for claude commands (use bash -i) |
| 1.7.0 | 2026-02-16 | Fix collab: skip missing commands (exit 127), improve JSON extraction from Claude envelope |
| 1.6.9 | 2026-02-16 | Fix collab: add `error_max_turns` to usage limit detection (was preventing command switch) |
| 1.6.8 | 2026-02-16 | Fix collab: launcher crash handling, select() EINTR handling, worker exit code capture |
| 1.6.7 | 2026-02-16 | Fix collab: add YAML parse error warning; fix se3.config.yaml comment format |
| 1.6.6 | 2026-02-16 | Fix collab: add "hit your limit" to usage limit detection keywords (was causing command not to switch) |
| 1.6.5 | 2026-02-16 | Fix collab: manager launcher outputs valid JSON on failure, signal handling moved to launcher, import error handling, escalation JSON |
| 1.6.4 | 2026-02-16 | Fix collab launchers: JSON extraction, race conditions, signal handling, task dir creation, stderr handling |
| 1.6.3 | 2026-02-16 | Fix collab orchestrator: manager now uses activity-based monitoring via launcher, proper limit detection |
| 1.6.2 | 2026-02-16 | Fix collab orchestrator: treat `error_max_turns` as usage limit and switch command |
| 1.6.1 | 2026-02-16 | Fix collab orchestrator: clear CLAUDECODE on daemon start, command switch on JSON parse error |
| 1.6.0 | 2026-02-16 | Activity-based timeout for collab workers: real-time I/O monitoring, automatic command fallback on inactivity, `run_with_monitor()` API |
| 1.5.5 | 2026-02-16 | Fix collab orchestrator: extract JSON from Claude CLI envelope via Python, add --dangerously-skip-permissions for daemon mode, reduce manager max-turns |
| 1.5.4 | 2026-02-16 | Fix collab orchestrator: do_plan supports full tasks array |
| 1.5.3 | 2026-02-16 | Fix collab orchestrator: clean stale tasks on new session, handle empty task list |
| 1.5.2 | 2026-02-16 | Fix collab manager: use text output format, strip markdown fences from Claude response before JSON parsing |
| 1.5.1 | 2026-02-16 | Fix collab daemon: pass objective to orchestrator script, clear CLAUDECODE env to prevent nested session error |
| 1.5.0 | 2026-02-16 | Simplified collab: removed External Controller v2 (daemon, API, README.md changes). Bash orchestrator only. File-based async communication. |
| 1.4.0 | 2026-02-16 | Claude Command Resolver: priority-based multi-command fallback on usage limit/timeout, unified config module, `se3 claude-cmd` CLI |
| 1.3.0 | 2026-02-16 | Complete External Controller: MCP server, HTTP API, session persistence, auto-commit, collab v2 integration |
| 1.2.0 | 2026-02-16 | External Controller: session daemon with auto-commit, collaboration outside Claude (fixes nested process limitation) |
| 1.1.2 | 2026-02-16 | Fix collab orchestrator: jq-complete pipe assignments (`|`) and `+=` operator, MOCK_MODE unbound variable |
| 1.1.1 | 2026-02-16 | Version management module with strict enforcement rules |
| 1.1.0 | 2026-02-16 | Git Worktree Collaboration: independent entry mode (`--daemon`, `--manual`), multi-language human calls, version consistency check |
| 1.0.0 | 2026-02-16 | Initial stable release: `se3 init`, `se3 update`, `se3 commit`, `se3 collab`, Semantic Versioning 2.0.0 |

## Pre-1.0 Development History

- v7.0 — 2026-02-14 — SE3 Module System: Separate framework (SE3.md) from project config (CLAUDE.md), `se3 init` command
- v6.1 — 2026-02-14 — Requirement intake: three-source taxonomy for structured requirement capture
- v6.0 — 2026-02-14 — CLI tools: se3 lint, sync, verify, status for enforceable framework
- v5.1 — 2026-02-14 — Diagnostic dashboard: status.md for single-source-of-truth session state
- v5.0 — 2026-02-14 — Verification protocol, spec guardrails, init.sh environment automation
- v4.1 — 2026-02-14 — Adaptive formality: match SDD ceremony to scope
- v4.0 — 2026-02-14 — Remove demands.md, specs as truth, adaptive commit/context rules
- v3.0 — 2026-02-14 — English rewrite, native agent team, global CLAUDE.md
- v2.0 — 2026-02-14 — Remove intentions.md, unified Human-as-MCP, progressive startup
- v1.0 — 2026-02-14 — Initial concept
