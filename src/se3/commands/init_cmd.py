"""SE3 Init command - Initialize a new SE3 project."""

import subprocess
from pathlib import Path
from typing import Optional

import typer

# Note: This module exports the init function directly to be registered by cli.py
# Not using app.command() here because cli.py registers it directly

DEFAULT_SE3_YAML = """# SE3 Project Configuration
# https://github.com/Fission-AI/SE3

project_name: {project_name}

# Version management settings
version:
  enabled: true
  # bump_rules:
  #   feature: minor
  #   bugfix: patch

# Confirmation steps (optional)
# confirmation:
#   enabled: true
#   steps: [plan]

# Claude CLI command resolution
# claude_commands:
#   - cmd: claude
#     priority: 0
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


def create_gitignore(path: Path, force: bool = False) -> tuple[bool, str]:
    """Create .gitignore file at the given path.

    Args:
        path: Directory where .gitignore should be created
        force: Whether to overwrite existing .gitignore

    Returns:
        Tuple of (created: bool, message: str)
    """
    gitignore_path = path / ".gitignore"

    if gitignore_path.exists() and not force:
        return False, f".gitignore already exists (use --force to overwrite)"

    try:
        gitignore_path.write_text(DEFAULT_GITIGNORE_TEMPLATE, encoding="utf-8")
        return True, ".gitignore created"
    except Exception as e:
        return False, f"Failed to create .gitignore: {str(e)}"


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

    # Create se3.yaml
    se3_yaml = root / "se3.yaml"
    if not se3_yaml.exists() or force:
        se3_yaml.write_text(
            DEFAULT_SE3_YAML.format(project_name=project_name), encoding="utf-8"
        )
        created.append(str(se3_yaml.relative_to(root)))
    else:
        skipped.append(f"{se3_yaml.relative_to(root)} already exists (use --force to overwrite)")

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

    # Create .gitignore
    gitignore_created = False
    gitignore_already_existed = False
    gitignore_message = ""

    gitignore_created_result, gitignore_message = create_gitignore(root, force=force)
    if gitignore_created_result:
        gitignore_created = True
    elif "already exists" in gitignore_message:
        gitignore_already_existed = True

    return {
        "created": created,
        "skipped": skipped,
        "git_initialized": git_initialized,
        "git_already_existed": git_already_existed,
        "git_message": git_message,
        "gitignore_created": gitignore_created,
        "gitignore_already_existed": gitignore_already_existed,
        "gitignore_message": gitignore_message,
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
    elif result.get("gitignore_already_existed"):
        typer.echo(f"⚠ .gitignore already exists (use --force to overwrite)")

    typer.echo(f"\n🎉 SE3 project initialized: {name}")
    typer.echo(f"\nNext steps:")
    typer.echo(f"  1. Edit se3.yaml to configure your project")
    typer.echo(f"  2. Edit se3/specs/base/spec.md to define project conventions")
    typer.echo(f"  3. Run 'se3 run \"your task\"' to start developing")
