"""SE3 Init command - Initialize a new SE3 project."""

import fnmatch
import subprocess
from pathlib import Path
from typing import Optional

import typer

# Note: This module exports the init function directly to be registered by cli.py
# Not using app.command() here because cli.py registers it directly

DEFAULT_SE3_YAML = """# SE3 Project Configuration
# https://github.com/Fission-AI/SE3
#
# For local-only overrides, create se3.local.yaml in the project root.
# When present, it fully replaces this file at load time and is
# gitignored by default so personal tweaks never get committed.

project_name: {project_name}

# Version management settings
version:
  enabled: true
  # bump_rules:
  #   feature: minor
  #   bugfix: patch

# Confirmation steps (optional)
# Per-step dict: list a step here to insert a CONFIRM after it.
# Steps NOT listed are not confirmed (there is no global toggle).
# confirmation:
#   steps:
#     plan: {{reviewer: human}}
#     design: {{reviewer: reviewer_bot, max_iterations: 3}}

# Agent registry (optional) — referenced by name from llm_caller / confirmation.
# agents:
#   primary: {{type: claude-code, cmd: claude, priority: 10}}

# LLM caller chain (optional)
# llm_caller:
#   defaults: [primary]
"""

# Default .gitignore template for SE3 projects
DEFAULT_GITIGNORE_TEMPLATE = """# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg
MANIFEST

# Virtual environments
venv/
ENV/
env/
.venv

# IDEs
.vscode/
.idea/
*.swp
*.swo
*~
.DS_Store

# SE3: ignore runtime content, whitelist specs/issues/scripts
/se3/*
!/se3/specs/
!/se3/issues/
!/se3/scripts/

# SE3: local-only config overrides (never committed)
se3.local.yaml
"""


def is_git_repository(path: Path) -> bool:
    """Check if the given path is already inside a git repository.

    Args:
        path: Directory to check

    Returns:
        True if the path is inside a git repository, False otherwise
    """
    # Check for .git directory in the path or any parent directory
    current = path.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return True
        current = current.parent
    return False


def init_repository(path: Path) -> tuple[bool, str]:
    """Initialize a new git repository at the given path.

    Args:
        path: Directory where git repository should be initialized

    Returns:
        Tuple of (success: bool, message: str)
    """
    try:
        result = subprocess.run(
            ["git", "init"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=True,
        )
        return True, result.stdout.strip() if result.stdout else "Git repository initialized"
    except subprocess.CalledProcessError as e:
        return False, f"Failed to initialize git repository: {e.stderr.strip() if e.stderr else str(e)}"
    except FileNotFoundError:
        return False, "Git is not installed or not in PATH"


LOCAL_CONFIG_PATTERN = "se3.local.yaml"
# Sentinel used to distinguish narrow negation patterns (that specifically
# un-ignore ``se3.local.yaml``) from broad ones (like ``!*.yaml``). A
# negation is "narrow" only if it matches LOCAL_CONFIG_PATTERN while NOT
# also matching the committed ``se3.yaml`` — i.e. the user was targeting
# the ``.local.yaml`` name specifically, not ``.yaml`` in general.
_PROJECT_CONFIG_PATTERN = "se3.yaml"
# No leading newline here — callers add exactly one blank-line separator
# before this block, independent of whether the existing file ends with
# a newline. Keeping the separator logic out of the constant avoids the
# asymmetric "zero vs one trailing newline → one vs two blank lines"
# artefact the old layout produced.
LOCAL_CONFIG_APPEND_BLOCK = (
    "# SE3: local-only config overrides (never committed)\n"
    f"{LOCAL_CONFIG_PATTERN}\n"
)


def _normalize_gitignore_pattern(pattern: str) -> str:
    """Strip anchor / recursive-glob / directory markers for fnmatch.

    - Leading ``/``: gitignore root anchor, not part of the filename.
    - Leading ``**/``: git's recursive-glob semantics — matches the file
      at any depth. ``fnmatchcase`` does not model ``**``, so without
      stripping we would miss patterns like ``**/se3.local.yaml`` and
      append a redundant rule.
    - Trailing ``/``: directory-only marker. A directory-only pattern
      does not strictly ignore a regular file, but the user has already
      spelled the name out — treat it as intent to ignore and avoid
      appending a duplicate line.
    """
    if pattern.startswith("/"):
        pattern = pattern[1:]
    if pattern.startswith("**/"):
        pattern = pattern[3:]
    if pattern.endswith("/"):
        pattern = pattern[:-1]
    return pattern


def _gitignore_has_local_pattern(content: str) -> bool:
    """Return True when .gitignore already ignores ``se3.local.yaml``.

    Matches literal lines (``se3.local.yaml`` / ``/se3.local.yaml`` /
    ``**/se3.local.yaml``) as well as glob patterns that already cover
    the filename (e.g. ``*.local.yaml``, ``*.local.*``, ``se3.local.*``).
    Without this the user would get a redundant append block on every
    ``se3 init`` even though the file is already ignored by an existing
    broader pattern. Negation patterns (``!...``) are skipped — they
    weaken ignore rules rather than add them.
    """
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        pattern = _normalize_gitignore_pattern(line)
        if not pattern:
            continue
        if fnmatch.fnmatchcase(LOCAL_CONFIG_PATTERN, pattern):
            return True
    return False


def _gitignore_has_local_negation(content: str) -> bool:
    """Return True when .gitignore *narrowly* un-ignores ``se3.local.yaml``.

    Git's ``!pattern`` syntax re-includes a previously-ignored path. If
    the user has explicitly written ``!se3.local.yaml`` (perhaps because
    a broad pattern like ``*.yaml`` was ignoring it and they wanted the
    file tracked), silently appending ``se3.local.yaml`` afterwards
    creates two conflicting rules that fight by last-line-wins order —
    the user could end up with the file tracked or ignored depending on
    unrelated edits, without any warning.

    "Narrow" here means the negation pattern matches ``se3.local.yaml``
    but does NOT also match ``se3.yaml``. Broad patterns such as
    ``!*.yaml``, ``!se3.*``, or ``!*`` happen to cover our file too, but
    the user was not explicitly un-ignoring it — they just have a general
    rule that tracks all YAML (or all) files. In that case appending our
    ignore rule is the right thing to do, and the warning would mislead.
    """
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line.startswith("!"):
            continue
        body = line[1:].lstrip()
        if not body or body.startswith("#"):
            continue
        pattern = _normalize_gitignore_pattern(body)
        if not pattern:
            continue
        matches_local = fnmatch.fnmatchcase(LOCAL_CONFIG_PATTERN, pattern)
        matches_project = fnmatch.fnmatchcase(_PROJECT_CONFIG_PATTERN, pattern)
        if matches_local and not matches_project:
            return True
    return False


def create_gitignore(path: Path, force: bool = False) -> tuple[str, str]:
    """Ensure ``.gitignore`` ignores ``se3.local.yaml``.

    Five outcomes are returned via ``status``:

    - ``"created"`` — file did not exist (or ``force=True``); template was
      written from scratch.
    - ``"appended"`` — file existed without ``se3.local.yaml`` in it; we
      appended the local-config-ignore block (idempotent: re-running is a
      no-op). Even without ``--force`` this happens, because the task
      explicitly requires the pattern to be present.
    - ``"negated"`` — file existed and contained an explicit negation
      (``!se3.local.yaml``) that would fight a plain ``se3.local.yaml``
      append. We leave the file untouched and surface a warning rather
      than create two conflicting rules that silently resolve by
      last-line-wins.
    - ``"unchanged"`` — file existed and already ignored
      ``se3.local.yaml``.
    - ``"error"`` — an I/O error prevented reading or writing the file.
      Distinct from ``"unchanged"`` so callers can surface the real
      failure instead of showing a misleading "already exists" message.

    Args:
        path: Directory where ``.gitignore`` lives.
        force: When True, overwrite any existing file with the full template.

    Returns:
        Tuple of ``(status, message)``.
    """
    gitignore_path = path / ".gitignore"

    if not gitignore_path.exists() or force:
        try:
            gitignore_path.write_text(DEFAULT_GITIGNORE_TEMPLATE, encoding="utf-8")
            return "created", ".gitignore created"
        except Exception as e:
            return "error", f"Failed to create .gitignore: {str(e)}"

    try:
        existing = gitignore_path.read_text(encoding="utf-8")
    except Exception as e:
        return "error", f"Failed to read existing .gitignore: {str(e)}"

    # Negation check runs BEFORE the ignore-pattern check on purpose: a
    # file can contain both a broad ignore (e.g. ``*.yaml``) AND an
    # explicit ``!se3.local.yaml`` negation. Semantically the negation
    # wins — git keeps the file tracked — so returning ``"unchanged"``
    # because the broad pattern also matches would make us silently
    # accept a state where se3.local.yaml is NOT ignored and the
    # operator never gets warned. Surface the negation warning first.
    if _gitignore_has_local_negation(existing):
        # User explicitly un-ignored se3.local.yaml. Appending a plain
        # ``se3.local.yaml`` line now would create two conflicting rules
        # where later-line-wins determines the outcome — exactly the
        # kind of silent foot-gun we want to avoid. Do not modify the
        # file; the caller will surface the warning to the operator.
        return (
            "negated",
            f".gitignore contains an explicit negation of {LOCAL_CONFIG_PATTERN} "
            f"(``!{LOCAL_CONFIG_PATTERN}``); refusing to append a conflicting rule",
        )

    if _gitignore_has_local_pattern(existing):
        return "unchanged", ".gitignore already exists (use --force to overwrite)"

    # Append the local-config block with exactly one blank line of
    # separation, regardless of whether the existing file ends with a
    # trailing newline:
    #   "xyz\n"  + separator + block → "xyz\n\n# SE3…"
    #   "xyz"    + separator + block → "xyz\n\n# SE3…"
    # Both end up with one blank line between the previous content and
    # the comment header.
    if not existing:
        separator = ""
    elif existing.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    # Single write_text call replaces the read+append pair, so the
    # on-disk transition from "existing" to "existing + block" is one
    # syscall rather than two. A concurrent writer slipping in between
    # the earlier read and an append can no longer produce a duplicated
    # pattern line — the worst case now is a last-writer-wins clobber,
    # which is the normal semantics of any non-locking file writer.
    new_content = existing + separator + LOCAL_CONFIG_APPEND_BLOCK
    try:
        gitignore_path.write_text(new_content, encoding="utf-8")
    except Exception as e:
        return "error", f"Failed to append to .gitignore: {str(e)}"
    return "appended", f"appended {LOCAL_CONFIG_PATTERN} to existing .gitignore"


def _get_base_spec_template(project_name: str) -> str:
    """Generate base spec content."""
    return f"""# {project_name} — Base Specification

## Purpose

项目基础约定。此 spec 由 `se3 init` 生成，在所有 `se3 run` 流程中自动加载。

## Requirements

### Requirement: Project Identity

- **项目名称**: {project_name}
- **简述**: （请填写项目简述）
- **主要语言/框架**: （请填写语言和框架）

### Requirement: Directory Structure

- `src/` — 源码目录
- `tests/` — 测试目录
- `se3/specs/` — SE3 规范目录

### Requirement: Coding Conventions

- （请填写代码规范）

### Requirement: Key Constraints

- （请填写关键约束）

### Requirement: Workflow Conventions

- 使用 `se3 run "task description"` 启动开发流程
- 运行测试后才可标记功能完成
- 主分支保持可运行状态

### Requirement: Version Management

项目 SHALL 使用语义化版本控制（Semantic Versioning 2.0.0）。

**版本格式:** `MAJOR.MINOR.PATCH`
- MAJOR: 不兼容的 API 修改
- MINOR: 向下兼容的功能添加  
- PATCH: 向下兼容的问题修复

**版本更新规则:**
- `feature` 任务 → bump minor 版本
- `bugfix` 任务 → bump patch 版本

#### Scenario: 版本自动更新
- **GIVEN** 当前版本为 1.2.3
- **WHEN** 完成 feature 任务并执行 commit 步骤
- **THEN** 版本自动更新为 1.3.0
"""


def run_init(project_root: Path, project_name: str, force: bool = False) -> dict:
    """Core init logic, separated for testability.

    Args:
        project_root: Root directory of the project
        project_name: Name of the project
        force: Whether to overwrite existing files

    Returns:
        dict with "created" (list of relative paths), "skipped" (list of messages),
        and git/gitignore status flags
    """
    root = Path(project_root).resolve()
    created = []
    skipped = []

    # Create se3 directory structure
    se3_dir = root / "se3"
    specs_dir = se3_dir / "specs"
    base_dir = specs_dir / "base"

    se3_dir.mkdir(exist_ok=True)
    specs_dir.mkdir(exist_ok=True)
    base_dir.mkdir(exist_ok=True)

    # Create se3.yaml (never touch se3.local.yaml — it is user-owned and
    # takes precedence at load time).
    se3_yaml = root / "se3.yaml"
    if not se3_yaml.exists() or force:
        se3_yaml.write_text(
            DEFAULT_SE3_YAML.format(project_name=project_name), encoding="utf-8"
        )
        created.append(str(se3_yaml.relative_to(root)))
    else:
        skipped.append(f"{se3_yaml.relative_to(root)} already exists (use --force to overwrite)")

    # Detect (but do not modify) an existing se3.local.yaml so the operator
    # knows it will shadow the just-generated se3.yaml at load time.
    local_yaml = root / "se3.local.yaml"
    # Use is_file() (not exists()) so the warning fires only for a real
    # file that will actually shadow se3.yaml at load time — matches the
    # check in get_project_config_path(). A directory or dangling symlink
    # at this path would not shadow, so we shouldn't warn about it.
    local_overrides_yaml = local_yaml.is_file()

    # Create base spec
    base_spec = base_dir / "spec.md"
    if not base_spec.exists() or force:
        base_spec.write_text(_get_base_spec_template(project_name), encoding="utf-8")
        created.append(str(base_spec.relative_to(root)))
    else:
        skipped.append(f"{base_spec.relative_to(root)} already exists (use --force to overwrite)")

    # Initialize git repository if not already in one
    git_initialized = False
    git_already_existed = False
    git_message = ""

    if is_git_repository(root):
        git_already_existed = True
        git_message = "Already inside a git repository"
    else:
        success, git_message = init_repository(root)
        if success:
            git_initialized = True

    # Create or update .gitignore. Five outcomes are surfaced: created
    # (brand-new file), appended (existing file gained the se3.local.yaml
    # pattern), negated (file explicitly un-ignores se3.local.yaml so we
    # refused to append a conflicting rule), unchanged (existing file
    # already had the pattern), and error (an I/O error prevented the
    # read or write). The error status is kept distinct from unchanged so
    # the UI does not mislabel a real I/O failure as "already exists".
    gitignore_status, gitignore_message = create_gitignore(root, force=force)
    gitignore_created = gitignore_status == "created"
    gitignore_appended = gitignore_status == "appended"
    gitignore_negated = gitignore_status == "negated"
    gitignore_already_existed = gitignore_status == "unchanged"
    gitignore_error = gitignore_status == "error"

    return {
        "created": created,
        "skipped": skipped,
        "git_initialized": git_initialized,
        "git_already_existed": git_already_existed,
        "git_message": git_message,
        "gitignore_created": gitignore_created,
        "gitignore_appended": gitignore_appended,
        "gitignore_negated": gitignore_negated,
        "gitignore_already_existed": gitignore_already_existed,
        "gitignore_error": gitignore_error,
        "gitignore_message": gitignore_message,
        "local_overrides_yaml": local_overrides_yaml,
    }


def init_cmd(
    project_root: str = typer.Option(".", "--project-root", "-p", help="Project root directory"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Project name"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing files"),
):
    """Initialize a new SE3 project.

    Creates the standard SE3 directory structure:
    - se3.yaml - Project configuration
    - se3/specs/ - Specification directory
    - se3/specs/base/spec.md - Base project specification
    """
    root = Path(project_root).resolve()

    # Detect project name if not provided
    if not name:
        name = root.name or "my-project"

    result = run_init(root, name, force)

    for path in result["created"]:
        typer.echo(f"✓ Created {path}")
    for msg in result["skipped"]:
        typer.echo(f"⚠ {msg}")

    # Display git initialization status
    if result.get("git_initialized"):
        typer.echo(f"✓ Initialized git repository")
    elif result.get("git_already_existed"):
        typer.echo(f"⚠ Git repository already exists")

    # Display .gitignore creation status
    if result.get("gitignore_created"):
        typer.echo(f"✓ Created .gitignore")
    elif result.get("gitignore_appended"):
        typer.echo(f"✓ Appended {LOCAL_CONFIG_PATTERN} to .gitignore")
    elif result.get("gitignore_negated"):
        typer.echo(
            f"⚠ .gitignore contains an explicit negation of {LOCAL_CONFIG_PATTERN} "
            f"(!{LOCAL_CONFIG_PATTERN}); leaving it untouched. "
            f"Remove the negation or delete {LOCAL_CONFIG_PATTERN} from tracking."
        )
    elif result.get("gitignore_error"):
        typer.echo(f"⚠ {result.get('gitignore_message')}")
    elif result.get("gitignore_already_existed"):
        typer.echo(f"⚠ .gitignore already exists (use --force to overwrite)")

    # Warn when an existing se3.local.yaml will shadow the generated se3.yaml
    if result.get("local_overrides_yaml"):
        typer.echo(
            "⚠ se3.local.yaml exists — it will override se3.yaml at load time"
        )

    typer.echo(f"\n🎉 SE3 project initialized: {name}")
    typer.echo(f"\nNext steps:")
    typer.echo(f"  1. Edit se3.yaml to configure your project")
    typer.echo(f"  2. Edit se3/specs/base/spec.md to define project conventions")
    typer.echo(f"  3. Run 'se3 run \"your task\"' to start developing")
