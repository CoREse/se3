---
name: "SE3: Start Session"
description: Initialize an SE3 work session — environment, context, baseline
---

**Usage**: `/se3:start`

Start a new SE3 work session. This skill runs the full startup protocol and guides you through any required setup.

**Steps**

1. **Run `se3 start --json`** to compute session state
   ```bash
   se3 start --json
   ```
   Parse the JSON output to get:
   - `first_time`: Whether this is a new project
   - `env_setup`: Whether init.sh needs to run
   - `openspec`: Whether openspec is available/initialized
   - `git`: Branch, uncommitted changes, recent commits
   - `active_changes`: List of active openspec changes
   - `pending_human_calls`: Human responses waiting to be processed
   - `actions`: Array of actions to execute

2. **Execute each action in the `actions` array** in order:

   - `ask_user`: Use AskUserQuestion tool with the provided question
   - `run_script`: Execute the command (e.g., `bash init.sh`)
   - `init_openspec`: Run `openspec init`
   - `run_tests`: Run tests to establish baseline
   - `process_human_call`: Read the specified file in `human-calls/` and act on the response
   - `create_progress`: Create `progress.md` file
   - `create_human_calls_dir`: Create `human-calls/` directory

3. **Report session summary** to the user:
   - Current branch and git status
   - Active changes (if any)
   - Pending human calls (if any)
   - What was set up or checked

4. **Transition to work mode**:
   - If there are active changes: Ask if they want to continue one
   - If no active changes: Ask what they want to work on

**Guardrails**

- If tests fail during `run_tests`, report the failure and pause — do not proceed until fixed
- If `init.sh` fails, diagnose and report before proceeding
- Always complete all actions before starting work
- Do NOT skip the startup protocol — it ensures the environment is ready
