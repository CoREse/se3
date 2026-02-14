# Demands

## D1: 长程Agent开发框架设计

基于Anthropic文章《Effective Harnesses for Long-Running Agents》中的核心理念，设计一个适用于Claude Code的长程开发框架：

### D1.1: 跨Session知识传递机制
- 设计结构化的知识传递系统，确保每个新session能快速理解项目状态
- 利用git commit messages、progress文件、openspec specs等多维度信息源
- 定义session启动协议（startup protocol），规范agent进入项目时的行为

### D1.2: 增量式开发流程
- 强制每次session聚焦于有限范围的feature/change
- 每次session结束时代码必须处于可合并（mergeable）状态
- 通过openspec change来管理每次增量的边界

### D1.3: 基于Git的Checkpointing
- 每完成一个feature/change后进行有意义的git commit
- commit message中包含对下一个session有价值的上下文信息
- 支持通过git history回滚有问题的实现

## D2: Spec Driven Development (SDD) 与长程Agent的有机结合

### D2.1: openspec作为Feature管理的核心
- specs作为项目功能的权威来源（single source of truth）
- changes作为增量开发单元，每个change对应一组相关任务
- archive机制追踪已完成的变更历史

### D2.2: 任务分解策略
- 每个change最多包含5个有强逻辑关系的任务
- 任务粒度要适合单个context window完成
- 任务间依赖关系要清晰明确

### D2.3: 验证闭环
- 每个change完成后需要验证实现是否符合spec
- 验证结果反馈到spec中（更新状态）
- 未通过验证的change需要记录问题并在后续change中修复

## D3: Agent Team原生支持

### D3.1: 多Agent协作模型
- 设计支持多个Claude Code agent并行工作的机制
- 定义agent间的任务分配和协调规则
- 避免agent间的冲突（如同时修改同一文件）

### D3.2: Agent角色分化
- 支持不同角色的agent（如：架构师agent、实现agent、测试agent）
- 每种角色有对应的行为规范和权限边界
- 角色间的交互通过结构化接口进行

## D4: 人类调用（Human-as-MCP）

### D4.1: 人类调用接口设计
- 将人类交互建模为类似MCP的异步调用
- 定义清晰的调用接口：请求格式、响应格式、超时处理
- 人类调用请求应持久化到文件系统，不依赖实时通信

### D4.2: 非阻塞执行
- 人类调用不应阻塞其他不相关任务的执行
- 需要人类输入的任务应被标记为"等待人类响应"并暂停
- 其他任务应继续正常推进
- 人类响应到达后，被暂停的任务自动恢复

### D4.3: 人类调用场景
- 需要人类判断的决策（如架构选择、优先级排序）
- 需要人类执行的操作（如外部系统配置、账号申请）
- 需要人类提供的信息（如业务逻辑确认、需求澄清）

## D5: 系统实现形态

### D5.1: 实现载体探索
- 探索最佳的实现形态：CLAUDE.md指令、Skills、MCP Server、或其组合
- 评估各种形态的优劣势
- 设计可扩展的架构，支持未来新的实现载体

### D5.2: 配置化与可定制
- 系统行为应可通过配置文件调整
- 支持项目级别和全局级别的配置
- 提供合理的默认值，降低上手门槛

### D5.3: 输出物定义
- 产出一套完整的CLAUDE.md模板（或等价物）
- 产出配套的Skills定义
- 产出使用文档和最佳实践指南
- 产出示例项目作为参考实现
