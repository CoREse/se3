---
id: hc-example-001
type: decision
priority: medium
status: responded
created: 2026-02-14
---

# 示例：选择数据库方案

## Context
项目需要持久化用户数据。当前项目规模较小（预计<1000用户），但需要考虑未来扩展性。这是一个示例文件，展示human-call的标准格式。

## Request
请在以下数据库方案中做出选择。

## Options
- **A**: SQLite - 轻量级，无需额外部署，适合小规模项目。但并发写入性能有限。
- **B**: PostgreSQL - 功能强大，扩展性好。但需要额外部署和维护。
- **C**: 文件系统 (JSON/YAML) - 最简单，无依赖。但不适合复杂查询和大数据量。

---

## Response
选择A (SQLite)。当前阶段简单优先，后续如果需要可以迁移到PostgreSQL。
