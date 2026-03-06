"""SE3 Init command - Initialize a new SE3 project."""

from pathlib import Path
from typing import Optional

import typer

app = typer.Typer()

DEFAULT_SE3_YAML = """# SE3 Project Configuration
# https://github.com/Fission-AI/SE3

# Version management settings
version:
  enabled: true
  # bump_rules:
  #   feature: minor
  #   bugfix: patch

# Confirmation steps (optional)
# confirmation:
#   enabled: true
#   steps: [propose, design]

# Claude CLI command resolution
# claude_commands:
#   - cmd: claude
#     priority: 0
"""


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


@app.command(name="init")
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
        se3_yaml.write_text(DEFAULT_SE3_YAML, encoding="utf-8")
        typer.echo(f"✓ Created {se3_yaml.relative_to(root)}")
    else:
        typer.echo(f"⚠ {se3_yaml.relative_to(root)} already exists (use --force to overwrite)")
    
    # Create base spec
    base_spec = base_dir / "spec.md"
    if not base_spec.exists() or force:
        base_spec.write_text(_get_base_spec_template(name), encoding="utf-8")
        typer.echo(f"✓ Created {base_spec.relative_to(root)}")
    else:
        typer.echo(f"⚠ {base_spec.relative_to(root)} already exists (use --force to overwrite)")
    
    typer.echo(f"\n🎉 SE3 project initialized: {name}")
    typer.echo(f"\nNext steps:")
    typer.echo(f"  1. Edit {se3_yaml.relative_to(root)} to configure your project")
    typer.echo(f"  2. Edit {base_spec.relative_to(root)} to define project conventions")
    typer.echo(f"  3. Run 'se3 run \"your task\"' to start developing")
