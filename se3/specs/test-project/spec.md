<!-- spec-format: v1 -->
# SE3 E2E Test Project Specification

## Purpose

Defines the end-to-end test project for the SE3 framework. This project is a real software project developed with SE3, used to test the various workflow modes of the `se3 run` command.

**Test project location**: `/data/cre/workspace/test-project/` (a sibling directory of se3.0, an independent git repository)

## Requirements

### Requirement: Test Project Overview

The test project SHALL be a complete, runnable software project with the following characteristics:

- **Project type**: Python CLI tool
- **Project name**: Task CLI
- **Functionality**: A simple command-line task manager
- **Version**: 0.1.7 (the current `pyproject.toml` version)
- **Test coverage**: A complete pytest test suite

#### Scenario: Test project structure
- **GIVEN** a developer needs to test SE3 workflows
- **WHEN** entering `/data/cre/workspace/test-project/`
- **THEN** a complete Python project is present
- **AND** it contains source code, tests, specs, and SE3 configuration

### Requirement: Supported Test Modes

The test project SHALL support testing the following SE3 workflow modes:

| Mode | Test file | Description |
|------|----------|------|
| `feature` | `tests/prompts/feature.md` | Full 10-step feature development flow |
| `bugfix` | `tests/prompts/bugfix.md` | Bug fix flow (skips update_spec) |
| `review` | `tests/prompts/review.md` | Code review flow (4 steps) |
| `small` | `tests/prompts/small.md` | Small change flow (5 steps) |
| `directive` | `tests/prompts/directive.md` | Directive execution flow |
| `discovery` | `tests/prompts/discovery.md` | Requirement discovery flow |

#### Scenario: Run feature mode test
- **GIVEN** the test project is initialized
- **WHEN** running `se3 run "implement search feature" --type=feature`
- **THEN** the full 10-step flow runs
- **AND** the version is bumped to 0.2.0

#### Scenario: Run bugfix mode test
- **GIVEN** a known bug exists in the code
- **WHEN** running `se3 run "fix bug" --type=bugfix`
- **THEN** the 10-step flow runs (without update_spec)
- **AND** the version is bumped to 0.1.1

#### Scenario: Run review mode test
- **GIVEN** a code implementation needs review
- **WHEN** running `se3 run "review code" --type=review`
- **THEN** the 4-step review flow runs
- **AND** no code is modified, only a report is generated

### Requirement: Test Project Structure

The test project SHALL have the following directory structure:

```
/data/cre/workspace/test-project/
├── pyproject.toml          # Python project config
├── README.md               # Project documentation
├── se3.yaml                # SE3 configuration
├── .gitignore              # Git ignore rules
├── LLM_TEST_GUIDE.md       # LLM automated testing guide
├── scripts/                # Automation scripts
│   ├── auto_test.py        # Main test script
│   ├── test_discover_auto.py # Discovery mode automation test
│   └── test_fix_loop.py    # Fix-loop verification script
├── src/
│   └── task_cli/
│       ├── __init__.py     # Package init
│       ├── cli.py          # CLI main module
│       ├── calculator.py   # Arithmetic reference implementation (correct version)
│       └── calc_test.py    # Fix-loop injection target (correct implementation when clean)
├── tests/
│   ├── conftest.py         # Pytest config (supports fix-loop test bug injection)
│   ├── test_cli.py         # Test file
│   ├── reset.sh            # Test reset script
│   └── prompts/            # Test prompts
│       ├── README.md
│       ├── feature.md
│       ├── bugfix.md
│       ├── review.md
│       ├── small.md
│       ├── directive.md
│       └── discovery.md
└── se3/
    └── specs/              # SE3 specs
        ├── base/spec.md
        └── task-cli/spec.md
```

#### Scenario: Verify project structure
- **GIVEN** the test project is initialized
- **WHEN** checking the directory structure
- **THEN** all required files and directories exist
- **AND** the project can be imported and run

### Requirement: Test Reset Capability

The test project SHALL support restoring to the pre-test state via git.

#### Scenario: Reset after testing
- **GIVEN** a round of testing has completed
- **WHEN** running `./tests/reset.sh`
- **THEN** the project is restored to a clean initial state
- **AND** all test-generated files are deleted
- **AND** the version follows the git reset back to the `pyproject.toml` version corresponding to the `v1.0-stable` tag (currently 0.1.7); the reset script itself does not explicitly rewrite the version number
- **AND** the SE3 runtime state is cleaned up

**Reset script functions:**
1. Reset git to the `v1.0-stable` tag (defined in the script as `STABLE_TAG="v1.0-stable"`, executed via `git reset --hard $STABLE_TAG`)
2. Clean up SE3 runtime files (state, tmp, logs, cache, history)
3. Delete generated runtime files (`progress.md`, `.se3_test_run_tracker`)
4. Verify the project state

**Command-line arguments:**

- In default (interactive) mode, `reset.sh` uses `read -p` to prompt the user for confirmation "Are you sure you want to reset to the stable version? This will discard all uncommitted changes. [y/N]", continuing only when the user enters `y` or `Y`, otherwise printing "Cancelled" and exiting with exit code `0`
- The script accepts a `-f` or `--force` flag: by iterating over the positional arguments (`for arg in "$@"`) it matches `-f` or `--force`, and on a match sets `FORCE=true`, skipping the interactive confirmation and proceeding directly into the reset flow
- This flag is designed for automation scenarios (such as the unattended scripts `scripts/auto_test.py` and `scripts/test_fix_loop.py`), preventing the script from hanging when stdin is not interactive

#### Scenario: Interactive reset requires confirmation
- **GIVEN** a tester runs `./tests/reset.sh` directly without passing `-f`/`--force`
- **WHEN** the script reaches the confirmation prompt
- **THEN** the script asks `[y/N]` via `read -p`
- **AND** when the input is not `y/Y`, it prints "Cancelled" and exits with exit code `0`, performing no reset operation

#### Scenario: Force flag skips confirmation prompt
- **GIVEN** an automation script needs to reset the test project unattended
- **WHEN** running `./tests/reset.sh -f` or `./tests/reset.sh --force`
- **THEN** the script skips the `[y/N]` interactive confirmation
- **AND** directly performs the git reset, runtime cleanup, and verification steps

### Requirement: Fix-Loop Test Support

The test project SHALL provide fix-loop (defect-repair loop) test support via `tests/conftest.py`, used to verify the SE3 framework's ability to automatically repair code after a test failure.

**Implementation mechanism:**

- `tests/conftest.py` checks the `SE3_FIX_LOOP_TEST` environment variable during pytest startup (the `pytest_configure` hook)
- When `SE3_FIX_LOOP_TEST=1`, it writes predefined "defective" content into `src/task_cli/calc_test.py`:
  - `add(a, b)` is implemented as `a - b` (incorrect subtraction)
  - `multiply(a, b)` is implemented as `a + b` (incorrect addition)
- After injection it immediately pops the variable from the environment (`os.environ.pop`), ensuring subsequent runs in the same process tree are unaffected
- Injection happens only during the "first round" of the fix-loop test, forcing the first round of tests to fail and thereby triggering the SE3 repair flow

#### Scenario: Trigger fix-loop test bug injection
- **GIVEN** a tester wants to verify the SE3 repair loop
- **WHEN** setting `SE3_FIX_LOOP_TEST=1` and running pytest
- **THEN** `src/task_cli/calc_test.py` is rewritten by conftest into a buggy version
- **AND** the tests then fail
- **AND** the environment variable is cleared, so subsequent tests are not re-injected

#### Scenario: Normal pytest run unaffected
- **GIVEN** the `SE3_FIX_LOOP_TEST` environment variable is not set
- **WHEN** running pytest
- **THEN** conftest does not modify any source file
- **AND** the tests run against the normal code

### Requirement: Calculator Module

The test project SHALL provide a `calculator.py` module under `src/task_cli/` as the "correct implementation" baseline that passes tests, contrasting with the "defective" implementation injected by the fix-loop.

**Module interface:**

- `add(a, b)` — returns `a + b`
- `subtract(a, b)` — returns `a - b`
- `multiply(a, b)` — returns `a * b`

**Purpose:**

- Serves as the reference implementation for arithmetic operations, independent of the CLI entry module
- Provides the correct behavior for the fix-loop test to compare against the injected version in `calc_test.py`
- Can be imported directly by the test suite for verification

#### Scenario: Calculator module exposes correct arithmetic
- **GIVEN** the test project is in a clean state
- **WHEN** importing `task_cli.calculator`
- **THEN** `add(2, 3)` returns `5`
- **AND** `subtract(5, 3)` returns `2`
- **AND** `multiply(2, 3)` returns `6`

### Requirement: Fix-Loop Target Module

The test project SHALL provide a `calc_test.py` module under `src/task_cli/` as the target file rewritten by `conftest.py` during the fix-loop test.

**Module interface (correct implementation in the clean state):**

- `add(a, b)` — returns `a + b`
- `multiply(a, b)` — returns `a * b`

**Injection behavior:**

- When `SE3_FIX_LOOP_TEST=1`, `conftest.py` rewrites this file into a buggy implementation (see the Fix-Loop Test Support requirement)
- The file lives under `src/` rather than `tests/`, making it easy to restore to a clean state via git reset
- The file's leading docstring explicitly declares its role as the fix-loop test target

#### Scenario: Fix-loop target module exposes correct arithmetic in clean state
- **GIVEN** the `SE3_FIX_LOOP_TEST` environment variable is not set
- **WHEN** importing `task_cli.calc_test`
- **THEN** `add(2, 3)` returns `5`
- **AND** `multiply(2, 3)` returns `6`

#### Scenario: Fix-loop target module is reset by git
- **GIVEN** a round of fix-loop testing has injected the bug
- **WHEN** running `./tests/reset.sh`
- **THEN** `src/task_cli/calc_test.py` is restored to the correct implementation

### Requirement: Calculator Module Test Suite

The test project SHALL provide a pytest test suite for the `calculator` module under `tests/test_calculator.py`, used to verify the correctness of the reference implementation.

**Test organization:**

- Tests are organized as a `TestCalculator` class, making it easy to run them grouped by class
- Imports `add`, `subtract`, `multiply` from `task_cli.calculator`

**Test coverage:**

- `test_add` — verifies `add(2, 3) == 5` and `add(-1, 1) == 0` (covering positive-number and zero-sum cases)
- `test_subtract` — verifies `subtract(5, 3) == 2` and `subtract(0, 5) == -5` (covering the negative-result case)
- `test_multiply` — verifies `multiply(2, 3) == 6` and `multiply(-2, 3) == -6` (covering the negative-multiplication case)

#### Scenario: Calculator tests pass on clean state
- **GIVEN** the test project is in a clean state
- **WHEN** running `pytest tests/test_calculator.py`
- **THEN** all `TestCalculator` tests pass
- **AND** the `add`, `subtract`, `multiply` behavior of the `calculator` module is verified

### Requirement: Fix-Loop Target Module Test Suite

The test project SHALL provide a pytest test suite for the `calc_test` module under `tests/test_calc_test.py`, serving as the criterion by which the fix-loop test fails in the first round and passes after repair.

**Test organization:**

- Top-level function-style (non-class) pytest tests
- Imports `add`, `multiply` from `task_cli.calc_test`

**Test coverage:**

- `test_add` — verifies `add(2, 3) == 5` and `add(-1, -2) == -3`
- `test_multiply` — verifies `multiply(2, 3) == 6` and `multiply(4, 5) == 20`

**Relationship with the Fix-Loop:**

- In the clean state (without `SE3_FIX_LOOP_TEST`), all tests pass
- After `SE3_FIX_LOOP_TEST=1` triggers conftest to inject the wrong implementation, these tests fail (`add` changed to subtraction, `multiply` changed to addition), thereby triggering the SE3 repair flow

#### Scenario: Fix-loop target tests pass on clean state
- **GIVEN** the `SE3_FIX_LOOP_TEST` environment variable is not set
- **WHEN** running `pytest tests/test_calc_test.py`
- **THEN** `test_add` and `test_multiply` all pass

#### Scenario: Fix-loop target tests fail after bug injection
- **GIVEN** `SE3_FIX_LOOP_TEST=1` is set
- **WHEN** running `pytest tests/test_calc_test.py`
- **THEN** `test_add` and `test_multiply` fail
- **AND** the failure becomes the trigger signal for the SE3 repair loop

### Requirement: Test Verification

Each test mode SHALL have an explicit verification checklist.

#### Scenario: Verify feature test results
- **GIVEN** the feature mode test has completed
- **WHEN** running the verification checks
- **THEN** confirm:
  - [ ] code files were modified
  - [ ] test files were updated
  - [ ] the spec was updated
  - [ ] the version was correctly bumped
  - [ ] progress.md has a record
  - [ ] a git commit exists

### Requirement: Documentation

The test project SHALL contain complete testing documentation.

**Documentation list:**
- `LLM_TEST_GUIDE.md` - LLM automated testing guide
- `tests/prompts/README.md` - index of test prompts
- `tests/prompts/*.md` - per-mode test prompts

**Automated testing infrastructure:**
- `scripts/auto_test.py` - main automation test script
- `scripts/test_discover_auto.py` - Discovery mode automation test script
- `scripts/test_fix_loop.py` - Fix-loop functionality verification script

#### Scenario: Follow test documentation
- **GIVEN** a developer needs to run tests
- **WHEN** reading `LLM_TEST_GUIDE.md`
- **THEN** they obtain complete test step instructions
- **AND** they can complete the testing independently

### Requirement: Top-Level Project Documents

The test project root SHALL contain several top-level project documents that record test execution results, known defects, and version evolution history, as part of the test project's own observable artifacts.

**Documentation list:**

- `README.md` — project description (already listed in the main structure)
- `LLM_TEST_GUIDE.md` — LLM automated testing guide (already listed in the main structure)
- `VERSIONS.md` — the test project's version history, recording each version's change summary in descending version order (e.g., `0.1.0` the initial version, `0.1.1` adding the stats/export commands and test infrastructure, etc.)
- `TEST_RESULTS.md` — a result report for a single round of SE3 end-to-end testing, containing the test date, SE3 version, a summary table of per-mode test results, per-mode execution details, discovered issues, and improvement suggestions
- `CRITICAL_BUG_REPORT.md` — descriptions of and recommendations for blocking SE3 framework defects found during testing, such as the incident where the `implement` step mistakenly wrote descriptive text into a code file

**Purpose:**

- Serve as "post-mortem report" traces of the test project's past runs, complementing runtime artifacts such as `se3/history/` and `progress.md`
- Beyond the clean test baseline, provide a bug feedback channel targeting the SE3 framework itself
- `VERSIONS.md` is independent of the in-spec Version field that SE3 maintains automatically, recording the version evolution of the test project itself (such as task_cli)

#### Scenario: Top-level documents exist alongside structured content
- **GIVEN** the test project is in a state after having run several rounds of SE3 flows
- **WHEN** listing the project root
- **THEN** `README.md`, `LLM_TEST_GUIDE.md`, `VERSIONS.md`, `TEST_RESULTS.md`, and `CRITICAL_BUG_REPORT.md` are all present
- **AND** they can be read independently, allowing one to understand the test project's recent status without entering subdirectories

#### Scenario: Critical bug reports capture SE3 framework defects
- **GIVEN** an SE3 flow exposed a framework-level defect (such as the implement step producing non-code content)
- **WHEN** the tester writes `CRITICAL_BUG_REPORT.md`
- **THEN** the document records the problem description, affected files, error examples, root cause, temporary workaround, and recommended improvements
- **AND** the report is independent of `TEST_RESULTS.md`, dedicated to quick identification of blocking defects

#### Scenario: Reset behavior for top-level documents
- **GIVEN** the cleanup scope of `tests/reset.sh` is declared as resetting git to the `v1.0-stable` tag and deleting the generated `progress.md` and `.se3_test_run_tracker`
- **WHEN** inspecting the project again after running the reset
- **THEN** the git-tracked versioned documents (such as `README.md`, `LLM_TEST_GUIDE.md`, `VERSIONS.md`, `TEST_RESULTS.md`, `CRITICAL_BUG_REPORT.md`) return to their corresponding baseline along with the git state
- **AND** the runtime-generated files (`progress.md`, `.se3_test_run_tracker`) are explicitly deleted per the cleanup rules in the reset script

### Requirement: Discovery Mode Automation Script

The test project SHALL provide an automated test script for Discovery mode under `scripts/test_discover_auto.py` that simulates user responses to discovery questions, thereby completing the entire discover workflow unattended.

**Script behavior:**

- Launches `se3 run --discover <prompt>` via `subprocess.Popen` and reads standard output in line-buffered mode
- Maintains a set of predefined responses (multi-round answers for the task search feature + a final `yes` confirmation)
- When the output shows a `Your response:` or `Discovery Pause` prompt, it writes the next predefined response in order to the subprocess stdin
- If the responses are exhausted while input is still awaited, it terminates the subprocess and reports failure
- After the subprocess exits, it reads `se3/state/engine.json` and verifies `status == "completed"`
- On success it prints the `refined_description` output produced by the discovery step (the first 200 characters)

**Purpose:**

- Verifies the end-to-end usability of the discover workflow without requiring human input each round
- Serves as the executable test fixture for discovery mode in the LLM automation test suite

#### Scenario: Run discover mode automation
- **GIVEN** the test project is in a clean state
- **WHEN** running `python scripts/test_discover_auto.py`
- **THEN** the script launches `se3 run --discover` and answers each round of discovery questions with the predefined responses
- **AND** the `status` in `se3/state/engine.json` becomes `completed`
- **AND** the script exits with exit code `0` and prints the refined description summary

#### Scenario: Discover automation runs out of responses
- **GIVEN** discovery asks for more rounds than the number of predefined responses
- **WHEN** the script detects the next input prompt but the responses are exhausted
- **THEN** the subprocess is terminated and failure is returned

### Requirement: Fix-Loop Verification Script

The test project SHALL provide a fix-loop (test-verify-fix) end-to-end verification script under `scripts/test_fix_loop.py` that proves SE3 triggers the repair loop on test failure and ultimately passes the tests.

**Script flow:**

1. **Reset the project**: `pkill` any leftover `se3 run` processes; `git reset --hard v1.0-stable` + `git clean -fd` to return to the stable tag; recursively clean the `se3/state`, `se3/history`, `se3/tmp`, `se3/calls`, `se3/logs`, `se3/cache` directories; delete the `progress.md`, `.se3_test_run_tracker`, `.test_run_count` tracker files
2. **Verify the initial state**: check that `src/task_cli/calc_test.py` is currently the correct implementation (containing `return a + b` and `return a * b`); the bug will be injected at runtime by conftest
3. **Launch the workflow**: run `SE3_FIX_LOOP_TEST=1 se3 run "Fix calc_test module - fix add and multiply functions" --type=bugfix` in the background with `nohup`, redirecting the log to `/tmp/fix_loop_test.log`
4. **Poll and monitor**: periodically read `se3/state/engine.json`, tracking the current step type/status, the `implement` step count, and the `fix_iterations` field; when `implement_count > 1`, the fix loop is considered triggered; waits up to 600 seconds
5. **Analyze the results**: after the flow ends, output the fix iterations, the number of implement steps, the step_history sequence, and the final flow status, and verify whether `calc_test.py` was repaired to the correct implementation
6. **Pass criterion**: only when there is more than one `implement` step and the final flow status is `COMPLETED` is it considered passing

**Dependencies:**

- the fix-loop injection logic in `tests/conftest.py`
- the `tests/test_calc_test.py` tests for the `calc_test` module functions
- the git tag `v1.0-stable` as the clean baseline

#### Scenario: Fix-loop verification succeeds
- **GIVEN** the v1.0-stable tag exists and the conftest fix-loop injection logic is correct
- **WHEN** running `python scripts/test_fix_loop.py`
- **THEN** the project is reset to v1.0-stable
- **AND** `se3 run` starts with `SE3_FIX_LOOP_TEST=1` and triggers a first-round test failure
- **AND** more than one `implement` step appears in the flow
- **AND** the final `engine.json` status is `COMPLETED` and `calc_test.py` is restored to the correct implementation
- **AND** the script exits with exit code `0`

#### Scenario: Fix-loop verification times out
- **GIVEN** the se3 flow cannot complete within 600 seconds for some reason
- **WHEN** the polling times out
- **THEN** the script prints `TIMEOUT` and exits with a failure status

#### Scenario: Fix-loop not triggered
- **GIVEN** the flow ends after only one `implement` step
- **WHEN** the script runs `analyze_results`
- **THEN** it reports "Fix loop was NOT triggered" and exits with a failure status

## Architecture

### Test Project Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    E2E Test Project                      │
├─────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  Source Code │  │    Tests     │  │    Specs     │  │
│  │  (task_cli)  │  │  (pytest)    │  │  (SE3 specs) │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
├─────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐                    │
│  │   Prompts    │  │ Reset Script │                    │
│  │  (7 modes)   │  │ (reset.sh)   │                    │
│  └──────────────┘  └──────────────┘                    │
└─────────────────────────────────────────────────────────┘
```

### Test Flow

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   Prepare   │───→│  se3 run    │───→│   Verify    │───→│   Reset     │
│  (clean)    │    │  (test)     │    │  (results)  │    │  (restore)  │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
```

## Usage

### Initialize Testing

```bash
# 1. Enter the test project
cd /data/cre/workspace/test-project

# 2. Confirm a clean state
git status

# 3. Run the test
se3 run "implement search feature" --type=feature
```

### Run a Specific Mode Test

```bash
# Feature mode
cd /data/cre/workspace/test-project
se3 run "implement task search feature" --type=feature

# Bugfix mode (inject a bug first)
se3 run "fix the bug where deleted task IDs are non-contiguous" --type=bugfix

# Review mode
se3 run "review the code implementation" --type=review

# Small mode
se3 run "add an example to the README" --type=small

# Directive mode
se3 run "add a --status filter option" --type=directive

# Discovery mode
se3 run --discover "I want to add an export feature"
```

### Reset the Test Project

```bash
cd /data/cre/workspace/test-project
./tests/reset.sh
```

### LLM Automated Testing

The test project supports a fully LLM-automated testing flow without human intervention.

**Using the automation test scripts:**

```bash
# Run all mode tests
cd /data/cre/workspace/test-project
python scripts/auto_test.py --all

# Run a specific mode test
python scripts/auto_test.py --mode=feature
python scripts/auto_test.py --mode=bugfix
python scripts/auto_test.py --mode=review
python scripts/auto_test.py --mode=small
```

**Automation test features:**

1. **Automatic reset**: automatically resets the project state after each test completes
   - Uses `git reset --hard` to roll back code changes
   - Cleans up SE3 runtime files
   - Ensures tests are mutually independent and repeatable

2. **Status monitoring**: monitors `se3/state/engine.json` to track execution progress

3. **Result verification**: automatically verifies:
   - whether file changes match expectations
   - whether tests pass
   - whether the version is correctly updated

**LLM testing guide:**

See `LLM_TEST_GUIDE.md` for details, including:
- detailed instructions for each test mode
- verification checklist templates
- error handling strategies
- output report format

## Maintenance

### Update the Test Project

When the SE3 framework is updated, you need to:

1. Update the specs in `se3/specs/`
2. Update the `se3.yaml` configuration
3. Update the test prompts (if needed)
4. Update the test flow documentation

### Add a New Test Mode

When SE3 adds a new workflow mode:

1. Add a new test prompt under `tests/prompts/`
2. Update `tests/prompts/README.md`
3. Update this spec

## References

- [SE3 Workflows Spec](../se3-workflows/spec.md)
- [SE3 Commands Spec](../se3-commands/spec.md)
- [Flow Engine Spec](../flow-engine/spec.md)
- [Session Protocol Spec](../session-protocol/spec.md)
