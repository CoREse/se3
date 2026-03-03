# SE3 Run 模式测试 Prompts

本文档包含用于测试 SE3 `se3 run` 命令各种模式的测试 prompts。

## 测试模式列表

| 模式 | 文件 | 描述 | 步骤数 |
|------|------|------|--------|
| feature | [feature.md](feature.md) | 完整功能开发流程 | 11 |
| bugfix | [bugfix.md](bugfix.md) | Bug 修复流程 | 10 |
| review | [review.md](review.md) | 代码审查流程 | 4 |
| small | [small.md](small.md) | 小型变更流程 | 5 |
| directive | [directive.md](directive.md) | 指令执行流程 | 8 |
| discovery | [discovery.md](discovery.md) | 需求探索流程 | 动态 |

## 快速测试命令

```bash
# Feature 模式测试
cd tests/e2e/test-project
se3 run "实现一个任务搜索功能" --type=feature

# Bugfix 模式测试（需先注入 bug）
se3 run "修复删除任务后 ID 不连续的 bug" --type=bugfix

# Review 模式测试
se3 run "审查当前代码实现" --type=review

# Small 模式测试
se3 run "在 README 中添加使用示例" --type=small

# Directive 模式测试
se3 run "给 list 命令添加 --status 过滤选项" --type=directive

# Discovery 模式测试
se3 run --discover "我想给 task-cli 添加数据导出功能"
```

## 测试流程

1. **准备阶段**: 确保测试项目处于干净状态
2. **执行测试**: 使用对应模式的 prompt 运行 `se3 run`
3. **验证结果**: 检查文件变更、版本更新、progress.md 记录
4. **恢复状态**: 使用 `git reset` 恢复测试前状态

## 状态恢复

测试完成后，使用以下命令恢复项目到测试前状态：

```bash
cd tests/e2e/test-project
./tests/reset.sh
```
