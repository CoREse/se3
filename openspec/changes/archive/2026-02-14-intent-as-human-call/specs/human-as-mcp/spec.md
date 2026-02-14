## MODIFIED Requirements

### Requirement: Human Call Interface
系统SHALL定义标准的人类调用接口，支持同步和异步两种模式。

**同步模式**（人类在场）：
- 直接通过 Claude Code 的交互界面（AskUserQuestion）向人类提问
- 适用于当前 session 中人类在场且需要即时响应的场景
- 响应直接用于当前任务，无需持久化调用文件

**异步模式**（人类不在场或需要时间处理）：
- 在 `human-calls/` 目录下创建请求文件
- 适用于长时间运行、人类暂时不可用、或需要人类离线执行操作的场景
- 每个请求文件MUST包含：唯一标识符、调用类型、请求描述、上下文信息、优先级、状态

**模式选择规则**：
- 默认使用同步模式（假设人在场）
- 当任务需要人类离线执行操作（如部署、申请账号）时，使用异步模式
- 当人类明确表示不再在场时（如"我先走了"），切换到异步模式
- 跨session的未决请求必须使用异步模式

#### Scenario: 同步调用 - 获取项目意图
- **WHEN** agent首次进入一个空项目
- **THEN** agent通过同步模式直接询问人类"这个项目要做什么？"，将响应转化为 demands.md

#### Scenario: 同步调用 - 即时决策
- **WHEN** agent在工作中遇到需要人类判断的选择且人类在场
- **THEN** agent直接通过对话向人类提问，获取即时响应

#### Scenario: 异步调用 - 离线操作
- **WHEN** agent需要人类执行一个无法在当前session完成的操作
- **THEN** agent在 human-calls/ 下创建请求文件，标记相关任务为 waiting-human

#### Scenario: 人类响应异步调用
- **WHEN** 人类查看pending状态的调用请求并提供响应
- **THEN** 调用请求状态更新为responded，下一个session处理响应

### Requirement: Non-Blocking Execution
人类调用MUST NOT阻塞其他不相关任务的执行。

#### Scenario: 发起调用后继续工作
- **WHEN** agent发起一个human call且还有其他不依赖此调用结果的任务
- **THEN** agent标记依赖此调用的任务为waiting-human，继续执行其他任务

#### Scenario: 人类响应后恢复任务
- **WHEN** 之前waiting-human的调用收到人类响应
- **THEN** 被暂停的任务标记为可执行，在合适的时机恢复执行

### Requirement: Human Call Persistence
异步模式的人类调用请求SHALL持久化到文件系统中。

调用文件存储在项目下的`human-calls/`目录中，文件名格式为`{YYYYMMDD}-{HHmmss}-{简短描述}.md`。

#### Scenario: 跨session的人类调用
- **WHEN** session A发起的human call在session A结束时仍未响应
- **THEN** 调用文件保留在文件系统中，session B启动时可以读取并检查响应状态

### Requirement: Human Call Types
系统SHALL支持三种类型的人类调用（同步和异步模式均适用）。

- **decision**: 需要人类做出判断或选择（如架构决策、优先级排序）
- **action**: 需要人类执行的操作（如外部系统配置、账号申请）
- **information**: 需要人类提供的信息（如业务逻辑确认、需求澄清）

#### Scenario: 决策类调用
- **WHEN** agent需要人类在多个方案中做出选择
- **THEN** 调用请求中列出所有选项及其优劣分析，人类选择后记录决策理由
