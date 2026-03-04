# SE3 E2E Test Project Specification

## Purpose

定义 SE3 框架的端到端测试项目。该项目是一个使用 SE3 开发的实际软件项目，用于测试 `se3 run` 命令的各种工作流模式。

**测试项目位置**: `/data/cre/workspace/test-project/` (与 se3.0 同级目录，独立 git 仓库)

## Requirements

### Requirement: Test Project Overview

测试项目 SHALL 是一个完整的、可运行的软件项目，具备以下特征：

- **项目类型**: Python CLI 工具
- **项目名称**: Task CLI
- **功能**: 简单的命令行任务管理器
- **版本**: 0.1.0（初始）
- **测试覆盖**: 完整的 pytest 测试套件

#### Scenario: Test project structure
- **GIVEN** 开发者需要测试 SE3 工作流
- **WHEN** 进入 `/data/cre/workspace/test-project/`
- **THEN** 看到一个完整的 Python 项目
- **AND** 包含源码、测试、specs、SE3 配置

### Requirement: Supported Test Modes

测试项目 SHALL 支持测试以下 SE3 工作流模式：

| 模式 | 测试文件 | 描述 |
|------|----------|------|
| `feature` | `tests/prompts/feature.md` | 完整 11 步功能开发流程 |
| `bugfix` | `tests/prompts/bugfix.md` | Bug 修复流程（跳过 design） |
| `review` | `tests/prompts/review.md` | 代码审查流程（4 步） |
| `small` | `tests/prompts/small.md` | 小型变更流程（5 步） |
| `directive` | `tests/prompts/directive.md` | 指令执行流程 |
| `discovery` | `tests/prompts/discovery.md` | 需求探索流程 |

#### Scenario: Run feature mode test
- **GIVEN** 测试项目已初始化
- **WHEN** 执行 `se3 run "实现搜索功能" --type=feature`
- **THEN** 执行完整的 11 步流程
- **AND** 版本 bump 到 0.2.0

#### Scenario: Run bugfix mode test
- **GIVEN** 代码中存在已知 bug
- **WHEN** 执行 `se3 run "修复 bug" --type=bugfix`
- **THEN** 执行 10 步流程（无 design）
- **AND** 版本 bump 到 0.1.1

#### Scenario: Run review mode test
- **GIVEN** 需要审查代码实现
- **WHEN** 执行 `se3 run "审查代码" --type=review`
- **THEN** 执行 4 步审查流程
- **AND** 不修改代码，只生成报告

### Requirement: Test Project Structure

测试项目 SHALL 具有以下目录结构：

```
/data/cre/workspace/test-project/
├── pyproject.toml          # Python 项目配置
├── README.md               # 项目文档
├── se3.yaml                # SE3 配置
├── .gitignore              # Git 忽略规则
├── src/
│   └── task_cli/
│       ├── __init__.py     # 包初始化
│       └── cli.py          # CLI 主模块
├── tests/
│   ├── __init__.py
│   ├── test_cli.py         # 测试文件
│   ├── TEST_WORKFLOW.md    # 测试流程文档
│   ├── reset.sh            # 测试重置脚本
│   └── prompts/            # 测试 prompts
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

### Requirement: Test Reset Capability

测试项目 SHALL 支持通过 git 恢复到测试前状态。

#### Scenario: Reset after testing
- **GIVEN** 已完成一轮测试
- **WHEN** 执行 `./tests/reset.sh`
- **THEN** 项目恢复到干净的初始状态
- **AND** 删除所有测试生成的文件
- **AND** 重置版本到 0.1.0
- **AND** 清理 SE3 运行时状态

**重置脚本功能：**
1. 重置 git 到初始提交
2. 清理 SE3 运行时文件（state, tmp, logs, cache, history）
3. 删除生成的文档（progress.md, VERSIONS.md）
4. 验证项目状态

### Requirement: Test Verification

每个测试模式 SHALL 有明确的验证清单。

#### Scenario: Verify feature test results
- **GIVEN** 完成了 feature 模式测试
- **WHEN** 执行验证检查
- **THEN** 确认：
  - [ ] 代码文件已修改
  - [ ] 测试文件已更新
  - [ ] spec 已更新
  - [ ] 版本正确 bump
  - [ ] progress.md 有记录
  - [ ] git 提交存在

### Requirement: Documentation

测试项目 SHALL 包含完整的测试文档。

**文档清单：**
- `tests/TEST_WORKFLOW.md` - 详细测试流程
- `tests/prompts/README.md` - 测试 prompts 索引
- `tests/prompts/*.md` - 各模式测试 prompt

#### Scenario: Follow test documentation
- **GIVEN** 开发者需要运行测试
- **WHEN** 阅读 `tests/TEST_WORKFLOW.md`
- **THEN** 获得完整的测试步骤说明
- **AND** 能够独立完成测试

## Architecture

### 测试项目架构

```
┌─────────────────────────────────────────────────────────┐
│                    E2E Test Project                      │
├─────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  Source Code │  │    Tests     │  │    Specs     │  │
│  │  (task_cli)  │  │  (pytest)    │  │  (SE3 specs) │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
├─────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │   Prompts    │  │   Workflow   │  │ Reset Script │  │
│  │  (7 modes)   │  │  (TEST_*.md) │  │ (reset.sh)   │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### 测试流程

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   Prepare   │───→│  se3 run    │───→│   Verify    │───→│   Reset     │
│  (clean)    │    │  (test)     │    │  (results)  │    │  (restore)  │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
```

## Usage

### 初始化测试

```bash
# 1. 进入测试项目
cd /data/cre/workspace/test-project

# 2. 确认干净状态
git status

# 3. 运行测试
se3 run "实现搜索功能" --type=feature
```

### 运行特定模式测试

```bash
# Feature 模式
cd /data/cre/workspace/test-project
se3 run "实现任务搜索功能" --type=feature

# Bugfix 模式（需先注入 bug）
se3 run "修复删除任务 ID 不连续的 bug" --type=bugfix

# Review 模式
se3 run "审查代码实现" --type=review

# Small 模式
se3 run "在 README 添加示例" --type=small

# Directive 模式
se3 run "添加 --status 过滤选项" --type=directive

# Discovery 模式
se3 run --discover "我想添加导出功能"
```

### 重置测试项目

```bash
cd /data/cre/workspace/test-project
./tests/reset.sh
```

## Maintenance

### 更新测试项目

当 SE3 框架更新时，需要：

1. 更新 `se3/specs/` 中的 specs
2. 更新 `se3.yaml` 配置
3. 更新测试 prompts（如需要）
4. 更新测试流程文档

### 添加新测试模式

当 SE3 添加新工作流模式时：

1. 在 `tests/prompts/` 添加新的测试 prompt
2. 更新 `tests/prompts/README.md`
3. 在 `TEST_WORKFLOW.md` 添加测试步骤
4. 更新本 spec

## References

- [SE3 Workflows Spec](../se3-workflows/spec.md)
- [SE3 Commands Spec](../se3-commands/spec.md)
- [Flow Engine Spec](../flow-engine/spec.md)
- [Session Protocol Spec](../session-protocol/spec.md)
