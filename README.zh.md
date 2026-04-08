# SE3 — Software Engineering 3.0 框架

![Version](https://img.shields.io/badge/version-3.38.1-blue)
![Python](https://img.shields.io/badge/python-3.8+-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

**一个规范驱动的流程引擎，将 AI 编程助手变成有纪律的软件工程师。**

[English README](README.md)

---

## 动机

AI 编程助手很强大，但缺乏纪律。给它一个复杂任务，它会：

- **跨会话丢失上下文** — 不记得做了什么、还剩什么、为什么做出某些决策
- **跳过工程规范** — 直接写代码，不做分析、设计或规范审查
- **偏离规范** — 悄悄弱化或忽略现有需求
- **产出未经验证的工作** — 不跑测试就提交，或者未检查就标记"完成"
- **缺乏流程结构** — 没有 feature/bugfix/review 的概念，不会根据范围调整流程

SE3 通过将 AI 代理包装在一个**程序驱动的状态机**中来解决这些问题：一个 11 步流程引擎，强制执行 分析 → 设计 → 实现 → 测试 → 验证 → 提交 的顺序，并根据任务类型和范围自动调整流程。AI 仍然负责思考；SE3 确保它按正确的顺序思考。

## 亮点

- **统一的 `se3 run` 入口** — 一个命令处理所有工作流：feature、bugfix、review、small change、directive
- **11 步流程引擎** — 状态机编排 analyze → propose → design → plan → implement → test → verify → commit，按工作流类型自动选择步骤
- **5 种工作流类型** — feature、bugfix、review、small、directive — 各有定制的步骤序列（bugfix 跳过设计，review 跳过实现）
- **Discovery 模式** — 当想法模糊需要先澄清需求时，进行多轮需求探索
- **Loop 模式 + git worktree 隔离** — 在隔离分支上连续自主执行，用 `--merge` 安全地合并回来
- **规范驱动的护栏** — 现有需求不能被悄悄弱化或删除；引擎强制保护规范完整性
- **智能版本管理** — LLM 分析实际代码变更，自动确定 patch/minor/major 的语义版本号
- **确认/审阅步骤** — 在 propose/design 步骤之后插入人工或 LLM 审阅关卡
- **多语言支持** — 输出语言（如 zh-CN）和规范书写语言（如 en-US）可独立配置

## 快速开始

### 1. 安装

```bash
# 在 SE3 仓库根目录
pip install -e .
```

### 2. 初始化项目

```bash
cd your-project
se3 init
```

这会创建：
```
se3/
├── specs/
│   └── base/
│       └── spec.md    # 基础项目规范（编辑此文件）
se3.yaml               # 框架配置
```

### 3. 运行任务

```bash
se3 run "添加基于 JWT 的用户认证"
```

引擎将会：分析任务 → 读取相关规范 → 提出方案 → 设计架构 → 规划实现任务 → 编写代码 → 运行测试 → 验证规范符合性 → 更新规范 → 版本号递增 → 提交 → 生成摘要。

## 使用方式

### 新任务

```bash
# 自动检测工作流类型
se3 run "修复登录超时 bug"

# 显式指定工作流类型
se3 run --type bugfix "修复登录超时 bug"
se3 run --type feature "添加 OAuth2 支持"
se3 run --type small "修复错误消息中的拼写错误"
```

### 恢复中断的流程

```bash
# 恢复最近中断的流程
se3 run --resume

# 恢复指定的流程
se3 run --resume --flow-id <flow-id>
```

### Discovery 模式

当你有一个模糊的想法，需要先探索需求时：

```bash
se3 run --discover "我需要一套用户角色管理的功能"
```

Discovery 模式会进行多轮对话来澄清需求，然后使用精化后的描述继续完整的工作流。

### Loop 模式（连续自主执行）

Loop 模式连续运行多个任务，每个任务在隔离的 git worktree 分支上执行：

```bash
# 启动 loop 模式（默认最多 10 次迭代）
se3 run --loop

# 限制迭代次数
se3 run --loop --max-iterations 5

# 禁用分支隔离（直接在当前分支上工作）
se3 run --loop --no-worktree
```

管理 loop 分支：

```bash
# 列出所有未合并的 loop 分支
se3 run --list-loops

# 合并 loop 分支（先显示 diff 摘要）
se3 run --merge se3-loop/20260324-120000
```

### 其他命令

```bash
# 检查规范文件是否符合护栏规则
se3 guardrails se3/specs/auth/spec.md

# 查看会话历史
se3 history
```

## 工作流类型

SE3 根据工作类型调整流程。每种工作流类型从 11 步引擎中选择不同的步骤子集：

| 类型 | 步骤序列 | 适用场景 |
|------|---------|---------|
| **feature** | analyze → read_spec → propose → design → plan_tasks → implement → test → verify_spec → update_spec → version_analyze → commit → summarize | 新功能或重大增强 |
| **bugfix** | analyze → read_spec → propose → plan_tasks → implement → test → verify_spec → update_spec → version_analyze → commit → summarize | Bug 修复（跳过设计，加快迭代） |
| **directive** | analyze → read_spec → plan_tasks → implement → test → verify_spec → version_analyze → commit → summarize | 按明确指令执行（跳过 propose + design） |
| **small** | analyze → implement → test → version_analyze → commit → summarize | 拼写错误、小修复、简单变更 |
| **review** | analyze → read_spec → verify_spec → summarize | 代码审查、审计或分析（不含实现） |

Discovery 模式（`--discover`）在任何工作流类型前添加 **discovery** 步骤，用于多轮需求探索。

## 架构概览

### 11 步流程引擎

SE3 的核心是一个程序驱动的状态机。每个步骤有明确的职责：

| # | 步骤 | 职责 | 自动化？ |
|---|------|------|---------|
| 1 | **analyze** | 分类任务类型和范围 | LLM |
| 2 | **read_spec** | 加载相关规范文件 | 自动 |
| 3 | **propose** | 生成变更方案，识别受影响文件 | LLM |
| 4 | **design** | 设计架构方案，生成设计文档 | LLM |
| 5 | **plan_tasks** | 拆分为具体任务 | LLM |
| 6 | **implement** | 编写代码，声明新增测试 | LLM |
| 7 | **test** | 运行测试套件，失败时触发修复循环 | 自动 |
| 8 | **verify_spec** | 检查实现是否符合规范要求 | LLM |
| 9 | **update_spec** | 在护栏约束下更新规范 | LLM |
| 10 | **version_analyze** | 根据实际变更确定语义版本号递增类型 | LLM |
| 11 | **commit** | 暂存、版本递增、提交 | 自动 |

最后还有一个 **summarize** 步骤，生成交接摘要供下次会话使用。

### 状态持久化

所有流程状态持久化到 `se3/state/engine.json`，支持：
- **恢复** — 中断后从断点精确恢复
- **历史** — 每个步骤的输入和输出的完整审计记录
- **修复循环** — 测试失败时，引擎自动返回 implement 步骤

### LLM 子进程模式

需要"思考"的步骤（analyze、propose、design 等）会启动一个 LLM 子进程，并精心构建上下文。子进程只接收与当前步骤相关的信息——而非整个对话历史。这保证了每个步骤的专注性，防止上下文污染。

## 项目结构

```
project/
├── se3.yaml                    # 框架配置
├── pyproject.toml              # Python 项目配置
├── se3/                        # SE3 运行时目录（除 specs/ 外 gitignored）
│   ├── specs/                  # 需求的权威来源（已提交到 git）
│   │   ├── base/
│   │   │   └── spec.md         # 基础项目约定（必需）
│   │   └── [capability]/
│   │       └── spec.md         # 功能/领域规范
│   ├── state/                  # 流程引擎状态持久化
│   ├── history/                # LLM 对话历史（NDJSON）
│   ├── cache/                  # 缓存文件
│   ├── logs/                   # 执行日志
│   ├── calls/                  # 人工审批调用队列
│   └── collab/                 # 多智能体协作状态
├── src/                        # 源代码
├── tests/                      # 测试文件
└── scripts/                    # 辅助脚本
```

## 配置

SE3 通过项目根目录的 `se3.yaml` 进行配置。主要配置项：

```yaml
# 确认/审阅关卡
confirmation:
  enabled: true
  steps: ["propose", "design"]    # 在这些步骤后插入审阅
  reviewer: "human"               # "human" 或 "llm"
  llm_reviewer:
    model: null
    max_iterations: 3

# 自动版本管理
version:
  enabled: true
  smart_version_analysis: true    # LLM 分析变更确定语义版本号递增
  auto_bump: true
  bump_rules:                     # 智能分析关闭时的兜底规则
    feature: minor
    bugfix: patch
    small: patch

# 语言设置
language:
  language: zh-CN                 # 面向人的输出语言
  spec_language: en-US            # 规范书写语言

# Claude 命令解析（基于优先级的回退机制）
claude_commands:
  - cmd: "claude"
    priority: 10
```

## 解决的问题

| 问题 | SE3 的解决方案 |
|------|---------------|
| **跨会话上下文丢失** | 状态持久化到 `se3/state/` + summarize 步骤生成交接摘要；`--resume` 从断点精确恢复 |
| **缺乏工程纪律** | 流程引擎强制按顺序执行 分析 → 设计 → 实现 → 测试；feature 任务不能跳过到编码 |
| **规范偏离** | 护栏机制防止悄悄弱化或删除现有需求；`update_spec` 步骤显式管理规范变更 |
| **未经验证的工作** | `test` 步骤自动运行；`verify_spec` 检查实现是否符合需求；失败触发修复循环 |
| **一刀切的流程** | 5 种工作流类型自适应调整：feature 完整流程、bugfix 快速路径、review 只读模式 |
| **手动流程编排** | `se3 run` 处理一切：无需手动跟踪步骤，无需记住"下一步做什么" |
| **不安全的自主执行** | Loop 模式使用 git worktree 隔离；变更留在独立分支上，直到显式合并 |
| **版本管理开销** | 智能版本分析根据实际代码变更自动确定正确的语义版本号递增 |
| **模糊的需求** | Discovery 模式在开始实现之前进行多轮需求探索 |

## 版本历史

完整版本历史请查看 [VERSIONS.md](VERSIONS.md)。

**当前版本：3.38.1**

## 许可证

MIT
