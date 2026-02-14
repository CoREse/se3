# SE 3.0 最佳实践指南

## 1. 编写好的 intentions.md

- **简洁明确**：用1-3段话描述项目的核心意图
- **聚焦"为什么"**：描述要解决的问题，而非具体实现方案
- **由人类维护**：这是人类掌控项目方向的核心文件
- **避免过于详细**：具体需求属于demands.md

### 好的示例
```markdown
# 意图
构建一个本地优先的笔记应用，支持Markdown编辑和全文搜索。
目标用户是开发者，需要快速记录和检索技术笔记。
```

### 避免的示例
```markdown
# 意图
用React + SQLite构建一个笔记应用，需要有标签功能、
导出功能、同步功能、主题切换...（过于具体）
```

## 2. 管理 demands.md

- **只增不减**：除非与intentions.md冲突，否则需求只增加不删除
- **层次清晰**：用编号标识需求（D1、D1.1等），便于追踪
- **可验证**：每个需求应该能判断"是否已实现"
- **AI参与维护**：AI可以基于intentions推导出需求并补充

## 3. Session管理

### 控制Session范围
- 每个session聚焦1-2个openspec change
- 不要在一个session中尝试完成整个项目
- 如果发现scope太大，主动拆分为多个change

### 有效的Progress记录
- 记录**结果**而非过程（"实现了X功能"而非"修改了a.js文件"）
- 记录**遗留问题**比记录完成的工作更重要
- **下一步建议**要具体可执行

### Commit Message规范
```
[Change名] 完成了XYZ功能

状态：change中3/5个任务已完成
注意：Y模块的Z功能还需要处理边界情况
下一步：继续完成剩余2个任务，特别关注错误处理
```

## 4. Human-as-MCP最佳实践

### 何时发起Human Call
- 涉及不可逆操作（部署、数据迁移等）
- 需要业务领域知识
- 技术方案有多个等价选项需要人类偏好
- 需要外部系统的访问凭证

### 何时不应发起Human Call
- 纯技术实现细节（agent可自行决定）
- 已在demands.md中明确的需求
- 可以通过搜索或文档解决的问题

### 编写好的Human Call请求
- 提供充分的上下文（为什么需要决策）
- 如果是decision类型，列出所有选项并分析优劣
- 标注正确的优先级
- 说明此决策对哪些任务有影响

## 5. Agent Team协作

### Change隔离策略
- 每个change应修改尽量独立的文件集
- 如果两个change必须修改同一文件，考虑合并或排序执行
- 使用git分支进一步隔离并行工作

### 角色使用建议
- 小型项目：单agent承担所有角色
- 中型项目：architect+implementer分工
- 大型项目：完整的三角色分工

## 6. 常见问题与解决方案

### Context Window耗尽
- **预防**：控制每个session的工作范围
- **应对**：在context接近极限时优先执行shutdown协议
- **恢复**：下一个session通过progress.md快速恢复上下文

### Change实现偏离Spec
- 完成change后使用openspec verify检查
- 发现偏离时创建新的change进行修正
- 不要在当前change中修补，保持change的职责单一

### Progress文件过大
- 定期将旧的session记录归档到 `docs/progress-archive/`
- 只在progress.md中保留最近10个session的记录

### 多Agent冲突
- 如果发生git冲突，优先保留更新的实现
- 通过agent-comms通知对方冲突情况
- 重新协调任务分配
