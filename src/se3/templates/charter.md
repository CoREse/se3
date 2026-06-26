# {project_name} — Charter

## Purpose
项目宪章（charter）。此文件由 `se3 init` / `se3 migrate` 生成，在每个 `se3 run`
step 中**无条件全量注入**，并兼任沙箱子进程的 conventions 通道（子进程读不到
CLAUDE.md，只能经 charter 获得项目级约定）。

charter 只收录**代码说不出、且全项目每个 step 都需要全量看到**的高层内容：项目
身份、顶层架构、项目级横切强制约定、版本管理。它**不**承载每模块/每符号的定位
信息——那是 code-index（`se3 code-index`）的职责，按需钻取，不进 charter（复制
进来只会得到一份随规模膨胀、又不如代码准的镜像）。

## Requirements

### Requirement: Project Identity
- 项目名称: {project_name}
- 简述: {project_description}
- 主要语言/框架: {languages_and_frameworks}

### Requirement: Top-Level Architecture
顶层架构的全局图景——主要子系统是什么、它们如何拼合、跨子系统的边界在哪里。
只写**需主观判断、无单一代码归属**的架构决策（『为何这些模块归为一个子系统』
这类语义分层）。

{top_level_architecture}

**注意:** 每个目录/模块/符号『在哪、干嘛、有哪些关键符号』这类机械定位信息
**不写在这里**，由 code-index 自动维护、按需查阅（`se3 code-index` 显示顶层
地图，`se3 code-index show <path>` 钻取到函数级）。charter 只承载机械结构
层级表达不了的语义/架构分层。

### Requirement: Coding Conventions
项目级、横切全项目的编码约定（不随单个模块变化、每个 step 都应遵守的那部分）。
- {coding_conventions}

### Requirement: Key Constraints
项目级强制约束（违反即视为错误的硬约定）。
- {key_constraints}

### Requirement: Workflow Conventions
- {workflow_conventions}

### Requirement: Version Management

项目 SHALL 使用语义化版本控制（Semantic Versioning 2.0.0）作为版本管理标准。

**版本号文件（单一真相源）:**
- Python 项目: `pyproject.toml` 中的 `project.version` 字段
- Node.js 项目: `package.json` 中的 `version` 字段
- 其他项目: 在 `se3.yaml` 中显式指定 `version.file_path`

**版本格式:**
遵循 SemVer 2.0.0: `MAJOR.MINOR.PATCH[-prerelease][+build]`
- MAJOR: 不兼容的 API 修改
- MINOR: 向下兼容的功能添加
- PATCH: 向下兼容的问题修复

**版本决策模型:**
- `version_analyze` 步骤的 `suggested_version` 字段是新版本号的唯一权威来源
  （由 LLM 基于实际变更内容、SemVer 2.0.0 默认规则以及可选的项目级规则文件推导）
- 可选自定义规则: 在 `se3/version-rules.md` 写入自然语言规则，
  `version_analyze` 会将其注入 LLM prompt 作为决策依据；文件不存在时回落到默认 SemVer 2.0.0 规则
- `commit` 步骤直接采用 `suggested_version` 写入版本文件；若该字段缺失或步骤失败，
  流程报错中断并提示人工介入（不再有静默 patch bump 兜底）

**文档更新:**
- README.md: 显示当前版本徽章/头部
- VERSIONS.md: 维护版本历史变更日志
