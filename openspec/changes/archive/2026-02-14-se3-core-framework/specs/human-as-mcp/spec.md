## ADDED Requirements

### Requirement: Human Call Interface
系统SHALL定义标准的人类调用接口，将人类交互建模为异步调用。

每个人类调用请求MUST包含：
- 唯一标识符（ID）
- 调用类型（decision/action/information）
- 请求描述（自然语言）
- 上下文信息（为什么需要人类介入）
- 优先级（high/medium/low）
- 状态（pending/responded/expired）

#### Scenario: 发起人类调用
- **WHEN** agent遇到需要人类介入的事项
- **THEN** agent在human-calls/目录下创建一个调用请求文件，包含所有必要信息

#### Scenario: 人类响应调用
- **WHEN** 人类查看pending状态的调用请求并提供响应
- **THEN** 调用请求状态更新为responded，响应内容被记录在同一文件中

### Requirement: Non-Blocking Execution
人类调用MUST NOT阻塞其他不相关任务的执行。

#### Scenario: 发起调用后继续工作
- **WHEN** agent发起一个human call且还有其他不依赖此调用结果的任务
- **THEN** agent标记依赖此调用的任务为waiting-human，继续执行其他任务

#### Scenario: 人类响应后恢复任务
- **WHEN** 之前waiting-human的调用收到人类响应
- **THEN** 被暂停的任务标记为可执行，在合适的时机恢复执行

### Requirement: Human Call Persistence
人类调用请求SHALL持久化到文件系统中，不依赖实时通信。

调用文件存储在项目下的`human-calls/`目录中，文件名格式为`{timestamp}-{id}.md`。

#### Scenario: 跨session的人类调用
- **WHEN** session A发起的human call在session A结束时仍未响应
- **THEN** 调用文件保留在文件系统中，session B启动时可以读取并检查响应状态

### Requirement: Human Call Types
系统SHALL支持三种类型的人类调用。

- **decision**: 需要人类做出判断或选择（如架构决策、优先级排序）
- **action**: 需要人类执行的操作（如外部系统配置、账号申请）
- **information**: 需要人类提供的信息（如业务逻辑确认、需求澄清）

#### Scenario: 决策类调用
- **WHEN** agent需要人类在多个方案中做出选择
- **THEN** 调用请求中列出所有选项及其优劣分析，人类选择后记录决策理由
