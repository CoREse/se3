# SE 3.0 最佳实践指南

## 1. Human Call 最佳实践

### 何时使用同步模式
- 人类在场且问题可以即时回答
- 项目意图获取（首次启动）
- 快速决策（A还是B？）
- 需求确认（"你的意思是...？"）

### 何时使用异步模式
- 需要人类离线执行操作（部署、申请账号）
- 人类明确表示不在了
- 问题需要人类花时间调研后回答
- 跨session的未决请求

### 编写好的 Human Call
- 提供充分上下文（为什么需要人类介入）
- decision类型要列出选项和优劣分析
- 标注正确的优先级
- 说明对哪些任务有影响

### 不应发起 Human Call 的场景
- 纯技术实现细节（agent自行决定）
- 已在 demands.md 中明确的需求
- 可通过搜索或文档解决的问题

## 2. 管理 demands.md

- demands.md 是项目需求的唯一来源
- 初始内容通过首次 human call 获取，后续通过更多 human call 迭代
- 只增不减（除非需求被明确废弃）
- 层次清晰：用编号标识（D1、D1.1等）
- 每个需求应可验证

## 3. Session 管理

### 渐进式启动
- 只读 progress.md 最近记录 + git log 就开始工作
- 不要预读所有文件浪费 context
- 需要某个 spec 的细节时再去读

### 控制 Session 范围
- 每个 session 聚焦1-2个 openspec change
- 发现 scope 太大时主动拆分
- 不要在一个 session 中尝试完成整个项目

### 有效的 Progress 记录
- 记录**结果**而非过程
- 记录**遗留问题**比记录完成的工作更重要
- **下一步建议**要具体可执行

### Commit Message 规范
```
[Change名] 完成了XYZ功能

状态：change中3/5个任务已完成
注意：Y模块的Z功能还需要处理边界情况
下一步：继续完成剩余2个任务，特别关注错误处理
```

## 4. Agent Team 协作

### Change 隔离策略
- 每个 change 应修改尽量独立的文件集
- 两个 change 必须修改同一文件时，考虑合并或排序执行

### 角色使用建议
- 小型项目：单 agent 承担所有角色
- 中型项目：architect + implementer 分工
- 大型项目：完整三角色分工

## 5. 常见问题

### Context Window 耗尽
- **预防**：控制工作范围 + 渐进式加载
- **应对**：优先执行 shutdown 协议
- **恢复**：下一个 session 通过 progress.md 快速恢复

### Change 实现偏离 Spec
- 完成后用 openspec verify 检查
- 发现偏离时创建新 change 修正
- 不在当前 change 中修补

### Progress 文件过大
- 定期归档旧记录到 docs/progress-archive/
- 只保留最近20个 session 的记录
