# SE3 Framework Version History

## Current Version

**2.22.7** — Fix: Version substring matching bug in documentation consistency check.

## Version History

| Version | Date | Changes |
|---------|------|---------|
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
