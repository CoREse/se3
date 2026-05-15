<!-- spec-format: v1 -->
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
- **版本**: 0.1.7（当前 `pyproject.toml` 版本）
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
| `feature` | `tests/prompts/feature.md` | 完整 10 步功能开发流程 |
| `bugfix` | `tests/prompts/bugfix.md` | Bug 修复流程（跳过 update_spec） |
| `review` | `tests/prompts/review.md` | 代码审查流程（4 步） |
| `small` | `tests/prompts/small.md` | 小型变更流程（5 步） |
| `directive` | `tests/prompts/directive.md` | 指令执行流程 |
| `discovery` | `tests/prompts/discovery.md` | 需求探索流程 |

#### Scenario: Run feature mode test
- **GIVEN** 测试项目已初始化
- **WHEN** 执行 `se3 run "实现搜索功能" --type=feature`
- **THEN** 执行完整的 10 步流程
- **AND** 版本 bump 到 0.2.0

#### Scenario: Run bugfix mode test
- **GIVEN** 代码中存在已知 bug
- **WHEN** 执行 `se3 run "修复 bug" --type=bugfix`
- **THEN** 执行 10 步流程（无 update_spec）
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
├── LLM_TEST_GUIDE.md       # LLM 自动化测试指南
├── scripts/                # 自动化脚本
│   ├── auto_test.py        # 主测试脚本
│   ├── test_discover_auto.py # Discovery 模式自动化测试
│   └── test_fix_loop.py    # Fix-loop 验证脚本
├── src/
│   └── task_cli/
│       ├── __init__.py     # 包初始化
│       ├── cli.py          # CLI 主模块
│       ├── calculator.py   # 算术参考实现（正确版本）
│       └── calc_test.py    # Fix-loop 注入目标（干净时为正确实现）
├── tests/
│   ├── conftest.py         # Pytest 配置（支持 fix-loop 测试 bug 注入）
│   ├── test_cli.py         # 测试文件
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

#### Scenario: Verify project structure
- **GIVEN** the test project is initialized
- **WHEN** checking the directory structure
- **THEN** all required files and directories exist
- **AND** the project can be imported and run

### Requirement: Test Reset Capability

测试项目 SHALL 支持通过 git 恢复到测试前状态。

#### Scenario: Reset after testing
- **GIVEN** 已完成一轮测试
- **WHEN** 执行 `./tests/reset.sh`
- **THEN** 项目恢复到干净的初始状态
- **AND** 删除所有测试生成的文件
- **AND** 版本随 git reset 回到 `v1.0-stable` 标签对应的 `pyproject.toml` 版本（当前为 0.1.7）；reset 脚本本身不显式重写版本号
- **AND** 清理 SE3 运行时状态

**重置脚本功能：**
1. 重置 git 到 `v1.0-stable` 标签（脚本中以 `STABLE_TAG="v1.0-stable"` 定义，通过 `git reset --hard $STABLE_TAG` 执行）
2. 清理 SE3 运行时文件（state, tmp, logs, cache, history）
3. 删除生成的运行时文件（`progress.md`、`.se3_test_run_tracker`）
4. 验证项目状态

**命令行参数：**

- `reset.sh` 在默认（交互）模式下通过 `read -p` 提示用户确认 "确定要重置到稳定版本吗？这将丢失所有未提交的变更。[y/N]"，仅当用户输入 `y` 或 `Y` 时才继续，否则打印 "已取消" 并以退出码 `0` 退出
- 脚本接受 `-f` 或 `--force` 标志：通过遍历位置参数（`for arg in "$@"`）匹配 `-f` 或 `--force`，命中则将 `FORCE=true`，跳过交互确认并直接进入重置流程
- 该标志专为自动化场景设计（如 `scripts/auto_test.py`、`scripts/test_fix_loop.py` 等无人值守脚本），避免脚本在 stdin 不可交互时挂起

#### Scenario: Interactive reset requires confirmation
- **GIVEN** 测试者直接执行 `./tests/reset.sh` 且未传入 `-f`/`--force`
- **WHEN** 脚本到达确认提示
- **THEN** 脚本通过 `read -p` 询问 `[y/N]`
- **AND** 输入非 `y/Y` 时打印 "已取消" 并以退出码 `0` 结束，不执行任何重置操作

#### Scenario: Force flag skips confirmation prompt
- **GIVEN** 自动化脚本需要无人值守地重置测试项目
- **WHEN** 执行 `./tests/reset.sh -f` 或 `./tests/reset.sh --force`
- **THEN** 脚本跳过 `[y/N]` 交互确认
- **AND** 直接执行 git reset、运行时清理与验证步骤

### Requirement: Fix-Loop Test Support

测试项目 SHALL 通过 `tests/conftest.py` 提供 fix-loop（缺陷修复循环）测试支持，用于验证 SE3 框架在测试失败后自动修复代码的能力。

**实现机制：**

- `tests/conftest.py` 在 pytest 启动期间（`pytest_configure` 钩子）检查 `SE3_FIX_LOOP_TEST` 环境变量
- 当 `SE3_FIX_LOOP_TEST=1` 时，向 `src/task_cli/calc_test.py` 写入预定义的"有缺陷"内容：
  - `add(a, b)` 被实现为 `a - b`（错误的减法）
  - `multiply(a, b)` 被实现为 `a + b`（错误的加法）
- 注入完成后立即从环境变量中弹出（`os.environ.pop`），确保同一进程树中后续运行不受影响
- 注入只在 fix-loop 测试的"首轮"执行时发生，迫使第一轮测试失败，从而触发 SE3 修复流程

#### Scenario: Trigger fix-loop test bug injection
- **GIVEN** 测试者希望验证 SE3 修复循环
- **WHEN** 设置 `SE3_FIX_LOOP_TEST=1` 并运行 pytest
- **THEN** `src/task_cli/calc_test.py` 被 conftest 重写为含 bug 的版本
- **AND** 测试随即失败
- **AND** 环境变量被清除，后续测试不再重复注入

#### Scenario: Normal pytest run unaffected
- **GIVEN** 没有设置 `SE3_FIX_LOOP_TEST` 环境变量
- **WHEN** 运行 pytest
- **THEN** conftest 不修改任何源文件
- **AND** 测试按正常代码运行

### Requirement: Calculator Module

测试项目 SHALL 在 `src/task_cli/` 下提供 `calculator.py` 模块，作为通过测试的"正确实现"基线，与 fix-loop 注入的"有缺陷"实现形成对照。

**模块接口：**

- `add(a, b)` — 返回 `a + b`
- `subtract(a, b)` — 返回 `a - b`
- `multiply(a, b)` — 返回 `a * b`

**用途：**

- 作为算术运算的参考实现，独立于 CLI 入口模块
- 为 fix-loop 测试提供与 `calc_test.py` 注入版本对比的正确行为
- 可被测试套件直接 import 用于验证

#### Scenario: Calculator module exposes correct arithmetic
- **GIVEN** 测试项目处于干净状态
- **WHEN** import `task_cli.calculator`
- **THEN** `add(2, 3)` 返回 `5`
- **AND** `subtract(5, 3)` 返回 `2`
- **AND** `multiply(2, 3)` 返回 `6`

### Requirement: Fix-Loop Target Module

测试项目 SHALL 在 `src/task_cli/` 下提供 `calc_test.py` 模块，作为 fix-loop 测试中被 `conftest.py` 重写的目标文件。

**模块接口（干净状态下的正确实现）：**

- `add(a, b)` — 返回 `a + b`
- `multiply(a, b)` — 返回 `a * b`

**注入行为：**

- 当 `SE3_FIX_LOOP_TEST=1` 时，`conftest.py` 将该文件重写为含 bug 的实现（参见 Fix-Loop Test Support 需求）
- 文件位于 `src/` 下而非 `tests/` 下，便于通过 git reset 恢复到干净状态
- 文件首部 docstring 明确声明其作为 fix-loop 测试目标的角色

#### Scenario: Fix-loop target module exposes correct arithmetic in clean state
- **GIVEN** 没有设置 `SE3_FIX_LOOP_TEST` 环境变量
- **WHEN** import `task_cli.calc_test`
- **THEN** `add(2, 3)` 返回 `5`
- **AND** `multiply(2, 3)` 返回 `6`

#### Scenario: Fix-loop target module is reset by git
- **GIVEN** 一轮 fix-loop 测试已注入 bug
- **WHEN** 执行 `./tests/reset.sh`
- **THEN** `src/task_cli/calc_test.py` 恢复到正确实现

### Requirement: Calculator Module Test Suite

测试项目 SHALL 在 `tests/test_calculator.py` 下提供 `calculator` 模块的 pytest 测试套件，用于验证参考实现的正确性。

**测试组织：**

- 测试以 `TestCalculator` 类形式组织，便于按类分组运行
- 从 `task_cli.calculator` import `add`、`subtract`、`multiply`

**测试覆盖：**

- `test_add` — 验证 `add(2, 3) == 5` 与 `add(-1, 1) == 0`（含正数与零和场景）
- `test_subtract` — 验证 `subtract(5, 3) == 2` 与 `subtract(0, 5) == -5`（含负结果场景）
- `test_multiply` — 验证 `multiply(2, 3) == 6` 与 `multiply(-2, 3) == -6`（含负数乘法场景）

#### Scenario: Calculator tests pass on clean state
- **GIVEN** 测试项目处于干净状态
- **WHEN** 运行 `pytest tests/test_calculator.py`
- **THEN** 所有 `TestCalculator` 测试通过
- **AND** `calculator` 模块的 `add`、`subtract`、`multiply` 行为得到验证

### Requirement: Fix-Loop Target Module Test Suite

测试项目 SHALL 在 `tests/test_calc_test.py` 下提供 `calc_test` 模块的 pytest 测试套件，作为 fix-loop 测试在第一轮中失败、修复后通过的判定依据。

**测试组织：**

- 顶层函数形式（非类）的 pytest 测试
- 从 `task_cli.calc_test` import `add`、`multiply`

**测试覆盖：**

- `test_add` — 验证 `add(2, 3) == 5` 与 `add(-1, -2) == -3`
- `test_multiply` — 验证 `multiply(2, 3) == 6` 与 `multiply(4, 5) == 20`

**与 Fix-Loop 的关系：**

- 干净状态下（无 `SE3_FIX_LOOP_TEST`），所有测试通过
- 当 `SE3_FIX_LOOP_TEST=1` 触发 conftest 注入错误实现后，这些测试将失败（`add` 被改为减法、`multiply` 被改为加法），从而触发 SE3 修复流程

#### Scenario: Fix-loop target tests pass on clean state
- **GIVEN** 没有设置 `SE3_FIX_LOOP_TEST` 环境变量
- **WHEN** 运行 `pytest tests/test_calc_test.py`
- **THEN** `test_add` 与 `test_multiply` 全部通过

#### Scenario: Fix-loop target tests fail after bug injection
- **GIVEN** 设置 `SE3_FIX_LOOP_TEST=1`
- **WHEN** 运行 `pytest tests/test_calc_test.py`
- **THEN** `test_add` 与 `test_multiply` 失败
- **AND** 失败成为 SE3 修复循环的触发信号

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
- `LLM_TEST_GUIDE.md` - LLM 自动化测试指南
- `tests/prompts/README.md` - 测试 prompts 索引
- `tests/prompts/*.md` - 各模式测试 prompt

**自动化测试设施：**
- `scripts/auto_test.py` - 主自动化测试脚本
- `scripts/test_discover_auto.py` - Discovery 模式自动化测试脚本
- `scripts/test_fix_loop.py` - Fix-loop 功能验证脚本

#### Scenario: Follow test documentation
- **GIVEN** 开发者需要运行测试
- **WHEN** 阅读 `LLM_TEST_GUIDE.md`
- **THEN** 获得完整的测试步骤说明
- **AND** 能够独立完成测试

### Requirement: Top-Level Project Documents

测试项目根目录 SHALL 包含若干顶层项目文档，用于记录测试执行结果、已知缺陷与版本演进历史，作为测试项目自身可观测产物的一部分。

**文档清单：**

- `README.md` — 项目说明（已在主结构中列出）
- `LLM_TEST_GUIDE.md` — LLM 自动化测试指南（已在主结构中列出）
- `VERSIONS.md` — 测试项目版本历史，按版本号倒序记录每个版本的变更摘要（如 `0.1.0` 初始版本、`0.1.1` 新增 stats/export 命令及测试基础设施等）
- `TEST_RESULTS.md` — 单轮 SE3 端到端测试的结果报告，包含测试日期、SE3 版本、各模式测试结果摘要表、各模式执行细节、已发现问题与改进建议
- `CRITICAL_BUG_REPORT.md` — 在测试过程中发现的、阻塞性 SE3 框架缺陷的描述与建议，例如 `implement` 步骤将描述性文字误写入代码文件的事故

**作用：**

- 作为测试项目历次运行的"事后报告"留痕，与 `se3/history/`、`progress.md` 等运行时产物互为补充
- 在干净测试基线之外，提供针对 SE3 框架本身的 bug 反馈渠道
- `VERSIONS.md` 与 SE3 自动维护的 spec 内 Version 字段独立，记录测试项目自身（如 task_cli）的版本演进

#### Scenario: Top-level documents exist alongside structured content
- **GIVEN** 测试项目处于已运行过若干轮 SE3 流程后的状态
- **WHEN** 列出项目根目录
- **THEN** 同时看到 `README.md`、`LLM_TEST_GUIDE.md`、`VERSIONS.md`、`TEST_RESULTS.md` 与 `CRITICAL_BUG_REPORT.md`
- **AND** 它们可被独立阅读，无需进入子目录即可了解测试项目近况

#### Scenario: Critical bug reports capture SE3 framework defects
- **GIVEN** 一次 SE3 流程暴露了框架级缺陷（如 implement 步骤产生非代码内容）
- **WHEN** 测试者撰写 `CRITICAL_BUG_REPORT.md`
- **THEN** 文档记录问题描述、受影响文件、错误示例、根本原因、临时修复手段与建议改进
- **AND** 该报告独立于 `TEST_RESULTS.md`，专门用于阻塞性缺陷的快速识别

#### Scenario: Reset behavior for top-level documents
- **GIVEN** `tests/reset.sh` 的清理范围声明为重置 git 到 `v1.0-stable` 标签并删除生成的 `progress.md` 与 `.se3_test_run_tracker`
- **WHEN** 执行 reset 后再次查看项目
- **THEN** 在 git 中被跟踪的版本化文档（如 `README.md`、`LLM_TEST_GUIDE.md`、`VERSIONS.md`、`TEST_RESULTS.md`、`CRITICAL_BUG_REPORT.md`）随 git 状态回到对应基线
- **AND** 运行时生成的文件（`progress.md`、`.se3_test_run_tracker`）按 reset 脚本中的清理规则被显式删除

### Requirement: Discovery Mode Automation Script

测试项目 SHALL 在 `scripts/test_discover_auto.py` 下提供 Discovery 模式的自动化测试脚本，模拟用户对 discovery 提问的响应，从而无人值守地完成整套 discover 工作流。

**脚本行为：**

- 通过 `subprocess.Popen` 启动 `se3 run --discover <prompt>`，以行缓冲方式读取标准输出
- 维护一组预定义响应（针对任务搜索功能的多轮回答 + 最终 `yes` 确认）
- 当输出中出现 `Your response:` 或 `Discovery Pause` 提示时，按顺序写入下一条预定义响应到子进程 stdin
- 若响应耗尽仍在等待输入，则终止子进程并报告失败
- 子进程退出后读取 `se3/state/engine.json`，校验 `status == "completed"`
- 流程成功时打印 discovery 步骤产生的 `refined_description` 输出（前 200 字符）

**用途：**

- 验证 discover 工作流的端到端可用性，且无需人类逐轮输入
- 作为 LLM 自动化测试套件中针对 discovery 模式的可执行测试夹具

#### Scenario: Run discover mode automation
- **GIVEN** 测试项目处于干净状态
- **WHEN** 执行 `python scripts/test_discover_auto.py`
- **THEN** 脚本启动 `se3 run --discover` 并按预定义响应回答每一轮 discovery 提问
- **AND** `se3/state/engine.json` 中 `status` 变为 `completed`
- **AND** 脚本以退出码 `0` 结束，并打印 refined description 摘要

#### Scenario: Discover automation runs out of responses
- **GIVEN** discovery 提出的轮次超过预定义响应数量
- **WHEN** 脚本检测到下一次输入提示但响应已耗尽
- **THEN** 子进程被终止并返回失败

### Requirement: Fix-Loop Verification Script

测试项目 SHALL 在 `scripts/test_fix_loop.py` 下提供 fix-loop（test-verify-fix）端到端验证脚本，证明 SE3 在测试失败时会触发修复循环并最终通过测试。

**脚本流程：**

1. **重置项目**: `pkill` 残留的 `se3 run` 进程；`git reset --hard v1.0-stable` + `git clean -fd` 回到稳定标签；递归清理 `se3/state`、`se3/history`、`se3/tmp`、`se3/calls`、`se3/logs`、`se3/cache` 目录；删除 `progress.md`、`.se3_test_run_tracker`、`.test_run_count` 跟踪文件
2. **校验初始状态**: 检查 `src/task_cli/calc_test.py` 当前为正确实现（含 `return a + b` 与 `return a * b`），bug 将在运行时由 conftest 注入
3. **启动工作流**: 以 `nohup` 后台运行 `SE3_FIX_LOOP_TEST=1 se3 run "Fix calc_test module - fix add and multiply functions" --type=bugfix`，将日志重定向到 `/tmp/fix_loop_test.log`
4. **轮询监控**: 周期性读取 `se3/state/engine.json`，跟踪当前 step 类型/状态、`implement` 步骤计数、`fix_iterations` 字段；当 `implement_count > 1` 时认为 fix loop 已被触发；最长等待 600 秒
5. **结果分析**: 流程结束后输出 fix iterations、implement 步骤数、step_history 序列、最终 flow 状态，并校验 `calc_test.py` 是否被修复到正确实现
6. **判定标准**: 仅当 `implement` 步骤多于 1 次且最终 flow 状态为 `COMPLETED` 时视为通过

**依赖：**

- `tests/conftest.py` 的 fix-loop 注入逻辑
- `tests/test_calc_test.py` 对 `calc_test` 模块函数的测试
- git 标签 `v1.0-stable` 作为干净基线

#### Scenario: Fix-loop verification succeeds
- **GIVEN** v1.0-stable 标签存在，且 conftest fix-loop 注入逻辑正确
- **WHEN** 执行 `python scripts/test_fix_loop.py`
- **THEN** 项目被重置到 v1.0-stable
- **AND** `se3 run` 以 `SE3_FIX_LOOP_TEST=1` 启动并触发首轮测试失败
- **AND** flow 中出现多于一次的 `implement` 步骤
- **AND** 最终 `engine.json` 状态为 `COMPLETED` 且 `calc_test.py` 恢复为正确实现
- **AND** 脚本以退出码 `0` 结束

#### Scenario: Fix-loop verification times out
- **GIVEN** se3 流程因故无法在 600 秒内完成
- **WHEN** 轮询超时
- **THEN** 脚本打印 `TIMEOUT` 并以失败状态退出

#### Scenario: Fix-loop not triggered
- **GIVEN** flow 仅执行了一次 `implement` 步骤即结束
- **WHEN** 脚本执行 `analyze_results`
- **THEN** 报告 "Fix loop was NOT triggered" 并以失败状态退出

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
│  ┌──────────────┐  ┌──────────────┐                    │
│  │   Prompts    │  │ Reset Script │                    │
│  │  (7 modes)   │  │ (reset.sh)   │                    │
│  └──────────────┘  └──────────────┘                    │
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

### LLM 自动化测试

测试项目支持 LLM 完全自动化的测试流程，无需人类干预。

**使用自动化测试脚本：**

```bash
# 运行所有模式测试
cd /data/cre/workspace/test-project
python scripts/auto_test.py --all

# 运行特定模式测试
python scripts/auto_test.py --mode=feature
python scripts/auto_test.py --mode=bugfix
python scripts/auto_test.py --mode=review
python scripts/auto_test.py --mode=small
```

**自动化测试特性：**

1. **自动重置**: 每个测试完成后自动重置项目状态
   - 使用 `git reset --hard` 回滚代码变更
   - 清理 SE3 运行时文件
   - 确保测试之间相互独立，可重复执行

2. **状态监控**: 监控 `se3/state/engine.json` 跟踪执行进度

3. **结果验证**: 自动验证：
   - 文件变更是否符合预期
   - 测试是否通过
   - 版本是否正确更新

**LLM 测试指南：**

详见 `LLM_TEST_GUIDE.md`，包含：
- 每个测试模式的详细说明
- 验证清单模板
- 错误处理策略
- 输出报告格式

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
3. 更新本 spec

## References

- [SE3 Workflows Spec](../se3-workflows/spec.md)
- [SE3 Commands Spec](../se3-commands/spec.md)
- [Flow Engine Spec](../flow-engine/spec.md)
- [Session Protocol Spec](../session-protocol/spec.md)
