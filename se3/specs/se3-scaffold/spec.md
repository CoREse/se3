<!-- spec-format: v1 -->
# se3-scaffold Specification

## Purpose

Define the SE3 project scaffold system, including the standard project structure, configuration system, and project initialization via `se3 init`.

## Requirements

### Requirement: SE3 Project Structure

The system SHALL define the standard SE3 project file structure.

**Standard structure:**
```
project/
├── se3.yaml               # Framework configuration (optional)
├── se3.local.yaml         # Optional developer-local override (gitignored)
├── README.md              # Project documentation
├── se3/                   # SE3 runtime directory
│   ├── specs/             # Source of truth for requirements
│   │   ├── base/          # Base project specification
│   │   │   └── spec.md    # Required: project conventions
│   │   └── <capability>/  # Capability specs
│   │       └── spec.md
│   ├── version-rules.md   # Optional project-level natural-language
│   │                      # version rules consumed by version_analyze
│   ├── state/             # Flow engine state persistence
│   └── cache/             # Cache files
├── src/                   # Source code (conventional)
└── tests/                 # Test files (conventional)
```

**Optional file — `se3/version-rules.md`:** When this file exists, its
plain-Markdown contents are injected into the `version_analyze` LLM
prompt as the authoritative version policy (see the `se3-versioning`
*Custom Version Rules File* requirement). When absent, the default
SemVer 2.0.0 rules apply. The file is included alongside `se3/specs/`,
`se3/issues/`, and `se3/scripts/` in the default `.gitignore` whitelist
so it is committed with the project.

**Required Files:**
- `se3/specs/base/spec.md` — Base project specification (auto-loaded in all flows)
- `se3.yaml` — Project configuration (optional but recommended)

**Key Directories:**
- `se3/specs/` — Spec files (the source of truth for requirements)
- `se3/state/` — Flow engine state persistence
- `se3/cache/` — Cache files

#### Scenario: Project initialization
- **WHEN** SE3 is initialized in a directory via `se3 init`
- **THEN** the standard structure is created with `se3/specs/base/spec.md`

### Requirement: Base Specification

The system SHALL require a base specification at `se3/specs/base/spec.md` in every SE3 project.

**Base spec purpose:**
- Define project identity (name, description, languages)
- Define directory structure conventions
- Define coding conventions
- Define key constraints
- Define workflow conventions
- Define version management rules

**Base spec auto-loading:**
- The base spec SHALL be automatically loaded in all `se3 run` flows
- It provides context for the discovery and analyze steps
- It helps the AI understand project conventions without manual prompting

#### Scenario: Base spec discovered
- **GIVEN** a project with `se3/specs/base/spec.md`
- **WHEN** `se3 run` executes discovery or analyze steps
- **THEN** the base spec content is automatically loaded into context

#### Scenario: Base spec missing
- **GIVEN** a project without `se3/specs/base/spec.md`
- **WHEN** `se3 init` is run
- **THEN** a base spec template is created automatically

### Requirement: Configuration System

The system SHALL support configuring framework behavior via `se3.yaml`.

**Configuration file location:** Project root (`se3.yaml`)

**Optional local override:** Developers MAY place a `se3.local.yaml`
at the project root. When present, it fully replaces `se3.yaml` as
the project-level config source (no deep merge). `se3.local.yaml` is
gitignored by default so machine-specific overrides stay local. See
the `se3-config` *Configuration File Format* requirement for the
authoritative semantics.

**Configuration options:**
- `version.enabled`: Enable automatic version bumping (default: true)
- `version.auto_bump`: Auto-apply the LLM-suggested version without confirmation (default: true)
- `agents`: Top-level dict registry `{name: {type, cmd, priority?}}` — see the `se3-config` Agent Registry requirement
- `llm_caller.defaults`: Default caller chain as a list of agent names referencing `agents` (optional)
- `llm_caller.steps.<step_name>`: Per-step hard override as a list of agent names (optional)
- `confirmation.steps`: Per-step confirmation dict `{<step_name>: {reviewer?, max_iterations?}}` — steps not listed are NOT confirmed

#### Scenario: Using default configuration
- **WHEN** no se3.yaml file exists in the project
- **THEN** the framework runs with built-in default values

#### Scenario: Custom configuration
- **WHEN** se3.yaml exists and specifies custom settings
- **THEN** the framework uses those settings to customize behavior

### Requirement: Project Initialization via se3 init

The system SHALL initialize a new SE3 project via the `se3 init` command.

**Interface:**
```bash
se3 init [--project-root PATH] [--name PROJECT_NAME] [--force]
```

**Created Files:**
1. **se3.yaml** — Project configuration
2. **se3/specs/base/spec.md** — Base specification template

#### Scenario: Initialize new project
- **GIVEN** a clean project directory without SE3 configuration
- **WHEN** a user runs `se3 init` in the project directory
- **THEN** the system creates:
  - `se3.yaml` with default configuration
  - `se3/specs/` directory structure
  - `se3/specs/base/spec.md` with base specification template

#### Scenario: Initialize with custom name
- **GIVEN** a directory at /path/to/my-project
- **WHEN** user runs `se3 init --name "My Project"`
- **THEN** the base spec contains "My Project" as project name

#### Scenario: Force re-initialization
- **GIVEN** a project with existing se3.yaml
- **WHEN** user runs `se3 init --force`
- **THEN** existing files are overwritten with fresh templates

### Requirement: Spec Directory Structure

The system SHALL define the specs directory structure.

**Specs Location:**
- Primary: `se3/specs/` (SE3 3.0+)

**Spec Organization:**
```
se3/specs/
├── base/                   # Base project specification (REQUIRED)
│   └── spec.md
├── _changelog/             # Spec change log (optional)
│   └── YYYY-MM-DD-change.md
├── _backlog/               # Backlog specs (optional)
├── flow-engine/            # Flow engine spec (if customizing)
│   └── spec.md
├── se3-commands/           # Commands spec (if customizing)
│   └── spec.md
└── <project-specific>/     # Project capability specs
    └── spec.md
```

**Spec Format:**
- Markdown format
- Required sections: Purpose, Requirements
- Scenario format: WHEN/THEN

#### Scenario: Spec discovery
- **WHEN** flow engine reads specs
- **THEN** it discovers all `*/spec.md` files under `se3/specs/`
- **AND** it always includes `se3/specs/base/spec.md` first

## Base Spec Template

The base specification template SHALL include the following sections:

```markdown
# {project_name} — Base Specification

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
```
