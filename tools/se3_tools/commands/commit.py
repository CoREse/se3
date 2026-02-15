"""SE3 Commit command — the single entry point for all git commits.

Replaces direct `git commit` usage. Enforces:
- Tests must pass before commit (no override without explicit flag)
- Sensitive files are blocked (.env, credentials, secrets)
- Commit message follows SE3 conventions (context for next session)
- Only tracked/specified files are staged

Message generation:
- Without -m: uses `claude -p` to generate a context-rich message from the diff
- With -m: validates the message meets minimum quality standards
- Both paths ensure the message contains actionable context for the next session
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import typer

app = typer.Typer(invoke_without_command=True)

# Files that should never be committed
SENSITIVE_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "credentials.json",
    "secrets.yaml",
    "secrets.yml",
    ".secret*",
    "*_secret*",
    "*.credential*",
    "token.json",
    "service-account*.json",
]

# Minimum message quality thresholds
MIN_MESSAGE_LENGTH = 20
MESSAGE_QUALITY_WARNINGS = [
    ("Status:", "Consider adding 'Status:' line describing where things stand"),
    ("Next:", "Consider adding 'Next:' line for what the next session should do"),
]

# Prompt for AI-generated commit messages
COMMIT_MESSAGE_PROMPT = """You are generating a git commit message for an SE3 project.

Below is the diff of staged changes. Write a commit message following this exact format:

<first-line>
A concise summary of WHAT changed and WHY (under 72 chars).
Do NOT just list file names. Describe the intent.

<body>
After a blank line, provide:

Status: where the project stands after this commit
Next: what the next development session should do

IMPORTANT:
- The first line should describe the PURPOSE, not just "Update X files"
- Status and Next lines give future developers context to continue efficiently
- Be specific and actionable in the Next line

Example:
Add health monitoring for collab workers via git activity checks

Status: collab orchestrator detects stale workers, all 38 tests pass
Next: integrate with se3 collab CLI and test with real claude -p processes

Now generate the message for this diff:

{diff}

Respond with ONLY the commit message text. No markdown fences, no explanation."""


def find_project_root() -> Path:
    """Find project root by looking for .claude/ or .git/."""
    current = Path.cwd()
    while current != current.parent:
        if (current / ".claude").is_dir() or (current / ".git").is_dir():
            return current
        current = current.parent
    return Path.cwd()


def run_command(cmd: List[str], cwd: Path, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command and return the result."""
    return subprocess.run(
        cmd, cwd=cwd, capture_output=capture, text=True
    )


def detect_test_command(project_root: Path) -> Optional[List[str]]:
    """Auto-detect the project's test command.

    Checks (in order):
    1. se3.config.yaml commit.test_command
    2. pytest (if tests/ exists or pytest.ini/pyproject.toml has pytest config)
    3. npm test (if package.json has test script)
    4. None (no tests detected)
    """
    # Check se3.config.yaml
    config_file = project_root / "se3.config.yaml"
    if config_file.exists():
        try:
            import yaml
            with open(config_file) as f:
                config = yaml.safe_load(f) or {}
            test_cmd = config.get("commit", {}).get("test_command")
            if test_cmd:
                return test_cmd.split() if isinstance(test_cmd, str) else test_cmd
        except Exception:
            pass

    # Check for pytest
    has_tests_dir = (project_root / "tests").is_dir()
    has_pytest_ini = (project_root / "pytest.ini").exists()
    has_pyproject = (project_root / "pyproject.toml").exists()
    if has_tests_dir or has_pytest_ini:
        return ["python", "-m", "pytest", "tests/", "-q"]
    if has_pyproject:
        content = (project_root / "pyproject.toml").read_text()
        if "[tool.pytest" in content or "pytest" in content:
            return ["python", "-m", "pytest", "-q"]

    # Check for npm test
    pkg_json = project_root / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            if pkg.get("scripts", {}).get("test"):
                return ["npm", "test"]
        except Exception:
            pass

    return None


def run_tests(project_root: Path, test_cmd: Optional[List[str]] = None) -> tuple[bool, str]:
    """Run project tests. Returns (passed, output)."""
    if test_cmd is None:
        test_cmd = detect_test_command(project_root)

    if test_cmd is None:
        return True, "(no test command detected — skipping)"

    typer.echo(f"  Running: {' '.join(test_cmd)}")
    result = run_command(test_cmd, cwd=project_root)

    output = result.stdout or ""
    if result.stderr:
        output += "\n" + result.stderr

    return result.returncode == 0, output.strip()


def check_sensitive_files(files: List[str]) -> List[str]:
    """Check if any files match sensitive patterns. Returns list of blocked files."""
    import fnmatch
    blocked = []
    for f in files:
        basename = Path(f).name
        for pattern in SENSITIVE_PATTERNS:
            if fnmatch.fnmatch(basename, pattern):
                blocked.append(f)
                break
    return blocked


def get_changed_files(project_root: Path) -> dict:
    """Get all changed files grouped by status.

    Returns dict with keys: staged, modified, untracked
    """
    result = run_command(["git", "status", "--porcelain"], cwd=project_root)
    if result.returncode != 0:
        return {"staged": [], "modified": [], "untracked": []}

    staged = []
    modified = []
    untracked = []

    for line in result.stdout.rstrip("\n").split("\n"):
        if not line or len(line) < 4:
            continue
        index_status = line[0]
        worktree_status = line[1]
        filepath = line[3:]

        # Handle renames: "R  old -> new"
        if " -> " in filepath:
            filepath = filepath.split(" -> ")[1]

        if index_status in ("A", "M", "D", "R"):
            staged.append(filepath)
        if worktree_status in ("M", "D"):
            modified.append(filepath)
        if index_status == "?" and worktree_status == "?":
            untracked.append(filepath)

    return {"staged": staged, "modified": modified, "untracked": untracked}


def stage_files(project_root: Path, files: Optional[List[str]] = None) -> List[str]:
    """Stage files for commit. Returns list of staged files.

    If files is None, stages all tracked modified files (NOT untracked).
    """
    if files:
        # Stage specified files
        result = run_command(["git", "add"] + files, cwd=project_root)
        if result.returncode != 0:
            typer.echo(f"  Error staging files: {result.stderr}", err=True)
            return []
        return files
    else:
        # Stage all tracked modifications (not untracked files)
        changes = get_changed_files(project_root)
        to_stage = changes["staged"] + changes["modified"]
        if not to_stage:
            return []
        result = run_command(["git", "add"] + to_stage, cwd=project_root)
        if result.returncode != 0:
            typer.echo(f"  Error staging files: {result.stderr}", err=True)
            return []
        return to_stage


def get_staged_diff(project_root: Path, stat_only: bool = False) -> str:
    """Get the diff of staged changes."""
    cmd = ["git", "diff", "--cached"]
    if stat_only:
        cmd.append("--stat")
    result = run_command(cmd, cwd=project_root)
    return result.stdout.strip() if result.returncode == 0 else ""


def generate_message_ai(project_root: Path) -> Optional[str]:
    """Generate a commit message using claude -p from the staged diff.

    Returns the generated message, or None if AI generation fails/unavailable.
    """
    if not shutil.which("claude"):
        return None

    diff = get_staged_diff(project_root)
    if not diff:
        return None

    # Truncate very large diffs to avoid context overflow
    max_diff_chars = 8000
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars] + "\n\n... (diff truncated, showing first 8000 chars)"

    prompt = COMMIT_MESSAGE_PROMPT.format(diff=diff)

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--max-turns", "1"],
            capture_output=True, text=True, timeout=60,
            cwd=project_root,
        )
        if result.returncode == 0 and result.stdout.strip():
            msg = result.stdout.strip()
            # Clean up any markdown fences the AI might add
            msg = re.sub(r'^```\w*\n?', '', msg)
            msg = re.sub(r'\n?```$', '', msg)
            return msg.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


def generate_message_fallback(project_root: Path) -> str:
    """Generate a basic commit message from diff stats.

    Fallback when AI generation is unavailable.
    """
    diff_stat = get_staged_diff(project_root, stat_only=True)
    if not diff_stat:
        return "Update files"

    lines = diff_stat.strip().split("\n")
    changed_files = [l.strip().split("|")[0].strip() for l in lines[:-1] if "|" in l]

    if len(changed_files) == 1:
        return f"Update {changed_files[0]}"
    elif len(changed_files) <= 3:
        return f"Update {', '.join(changed_files)}"
    else:
        summary = lines[-1].strip() if lines else ""
        return f"Update {len(changed_files)} files ({summary})"


def validate_message(message: str) -> List[str]:
    """Validate a commit message meets SE3 quality standards.

    Returns list of warnings (empty = good).
    """
    warnings = []

    if len(message.strip()) < MIN_MESSAGE_LENGTH:
        warnings.append(
            f"Message too short ({len(message.strip())} chars). "
            f"Minimum {MIN_MESSAGE_LENGTH} chars for meaningful context."
        )

    # Check first line length
    first_line = message.strip().split("\n")[0]
    if len(first_line) > 120:
        warnings.append(
            f"First line too long ({len(first_line)} chars). "
            "Keep the summary under 72 chars, use body for details."
        )

    # Check for context markers (only warn, don't block)
    for marker, suggestion in MESSAGE_QUALITY_WARNINGS:
        if marker not in message:
            warnings.append(suggestion)

    return warnings


def do_commit(project_root: Path, message: str) -> tuple[bool, str]:
    """Execute git commit. Returns (success, output)."""
    result = run_command(
        ["git", "commit", "-m", message],
        cwd=project_root,
    )
    output = result.stdout or ""
    if result.stderr:
        output += "\n" + result.stderr
    return result.returncode == 0, output.strip()


@app.callback()
def commit(
    message: Optional[str] = typer.Option(
        None, "--message", "-m", help="Commit message"
    ),
    files: Optional[str] = typer.Option(
        None, "--files", "-f",
        help="Space-separated list of files to stage (default: all tracked changes)"
    ),
    skip_tests: bool = typer.Option(
        False, "--skip-tests",
        help="Skip test verification (use with caution)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Preview what would be committed without doing it"
    ),
    no_ai: bool = typer.Option(
        False, "--no-ai",
        help="Skip AI message generation, use fallback"
    ),
    project_root: Optional[str] = typer.Option(
        None, "--project-root", "-p",
        help="Project root directory"
    ),
):
    """Commit changes with SE3 verification.

    Runs tests, checks for sensitive files, generates/validates commit message.
    This is the ONLY way to commit in SE3 projects.
    """
    root = Path(project_root) if project_root else find_project_root()
    file_list = files.split() if files else None

    typer.echo(f"\nse3 commit — {root}")
    typer.echo("=" * 50)

    # Step 1: Check for changes
    typer.echo("\n[1/5] Checking for changes...")
    changes = get_changed_files(root)
    total_changes = len(changes["staged"]) + len(changes["modified"]) + len(changes["untracked"])

    if total_changes == 0 and not file_list:
        typer.echo("  No changes to commit.")
        raise typer.Exit(0)

    if changes["staged"]:
        typer.echo(f"  Staged:    {len(changes['staged'])} file(s)")
    if changes["modified"]:
        typer.echo(f"  Modified:  {len(changes['modified'])} file(s)")
    if changes["untracked"]:
        typer.echo(f"  Untracked: {len(changes['untracked'])} file(s) (will not be auto-staged)")

    # Step 2: Run tests
    typer.echo("\n[2/5] Running tests...")
    if skip_tests:
        typer.echo("  WARNING: Tests skipped by --skip-tests flag")
        test_passed = True
        test_output = "(skipped)"
    else:
        test_passed, test_output = run_tests(root)
        if test_passed:
            typer.echo(f"  Tests passed.")
        else:
            typer.echo(f"  Tests FAILED. Commit blocked.")
            typer.echo(f"\n  Output:\n{test_output}")
            typer.echo("\n  Fix the tests or use --skip-tests to override.")
            raise typer.Exit(1)

    # Step 3: Stage files and check sensitive
    typer.echo("\n[3/5] Staging files...")
    staged = stage_files(root, file_list)

    if not staged:
        result = run_command(["git", "diff", "--cached", "--name-only"], cwd=root)
        if not result.stdout.strip():
            typer.echo("  No files to commit after staging.")
            raise typer.Exit(0)
        staged = result.stdout.strip().split("\n")

    # Re-read staged files after staging
    result = run_command(["git", "diff", "--cached", "--name-only"], cwd=root)
    actual_staged = [f for f in result.stdout.strip().split("\n") if f]

    # Check for sensitive files
    blocked = check_sensitive_files(actual_staged)
    if blocked:
        typer.echo(f"  BLOCKED: Sensitive files detected:")
        for f in blocked:
            typer.echo(f"    - {f}")
        run_command(["git", "reset", "HEAD"] + blocked, cwd=root)
        typer.echo(f"  Auto-unstaged {len(blocked)} sensitive file(s).")
        result = run_command(["git", "diff", "--cached", "--name-only"], cwd=root)
        if not result.stdout.strip():
            typer.echo("  No files remaining after removing sensitive files.")
            raise typer.Exit(1)
        actual_staged = [f for f in result.stdout.strip().split("\n") if f]

    typer.echo(f"  Staged {len(actual_staged)} file(s):")
    for f in actual_staged[:10]:
        typer.echo(f"    {f}")
    if len(actual_staged) > 10:
        typer.echo(f"    ... and {len(actual_staged) - 10} more")

    # Step 4: Generate/validate commit message
    typer.echo("\n[4/5] Preparing commit message...")
    if message:
        # Validate user-provided message
        warnings = validate_message(message)
        if warnings:
            typer.echo("  Message quality warnings:")
            for w in warnings:
                typer.echo(f"    - {w}")
            typer.echo("  (Proceeding anyway — these are suggestions, not blockers)")
        else:
            typer.echo("  Message validates OK.")
    else:
        # Generate message
        if not no_ai:
            typer.echo("  Generating message with AI...")
            message = generate_message_ai(root)
            if message:
                typer.echo(f"  AI-generated message:")
                for line in message.split("\n"):
                    typer.echo(f"    {line}")
            else:
                typer.echo("  AI generation unavailable, using fallback.")

        if not message:
            message = generate_message_fallback(root)
            typer.echo(f"  Fallback message: {message}")
            typer.echo("  WARNING: Fallback messages lack context. Consider providing -m.")

    # Step 5: Commit
    typer.echo("\n[5/5] Committing...")

    if dry_run:
        typer.echo(f"\n  [DRY RUN] Would commit with message:")
        for line in message.split("\n"):
            typer.echo(f"    {line}")
        typer.echo(f"\n  Files:")
        for f in actual_staged:
            typer.echo(f"    {f}")
        run_command(["git", "reset", "HEAD"] + actual_staged, cwd=root)
        raise typer.Exit(0)

    success, output = do_commit(root, message)
    if success:
        typer.echo(f"  Committed successfully.")
        result = run_command(["git", "log", "--oneline", "-1"], cwd=root)
        if result.returncode == 0:
            typer.echo(f"  {result.stdout.strip()}")
    else:
        typer.echo(f"  Commit failed:")
        typer.echo(f"  {output}")
        raise typer.Exit(1)

    typer.echo(f"\n{'=' * 50}")
    raise typer.Exit(0)
