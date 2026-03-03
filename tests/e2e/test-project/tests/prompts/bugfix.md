# Bugfix Mode Test Prompt

## Mode
`se3 run --type=bugfix`

## Setup (Inject a bug first)
在测试前，需要先在代码中注入一个 bug：

```bash
# 在 src/task_cli/cli.py 的 delete 函数中，删除任务后没有重新排序 ID
# 修改代码，注释掉重新排序的部分
cd tests/e2e/test-project
sed -i 's/# Reassign IDs/# Reassign IDs (BUG: disabled)/' src/task_cli/cli.py
sed -i 's/for j, t in enumerate(tasks):/# for j, t in enumerate(tasks):/' src/task_cli/cli.py
sed -i 's/t\["id"\] = j + 1/# t["id"] = j + 1/' src/task_cli/cli.py
```

## Test Prompt
```
修复删除任务后 ID 不连续的 bug。当删除一个任务后，剩余任务的 ID 应该
重新排序，保持从 1 开始的连续序列。现在删除任务后 ID 会出现空洞。
```

## Expected Workflow Steps
1. **analyze** - 识别为 bugfix
2. **read_spec** - 读取相关 spec
3. **propose** - 生成修复提案
4. **plan_tasks** - 分解修复任务（可能跳过）
5. **implement** - 修复 bug
6. **test** - 运行测试
7. **verify_spec** - 验证修复
8. **update_spec** - 更新 spec（如需要）
9. **commit** - 提交变更，版本 bump 到 0.1.1
10. **summarize** - 生成总结

## Expected Changes
- `src/task_cli/cli.py`: 修复 delete 命令的 ID 重排序逻辑

## Verification
- [ ] 删除任务后 ID 重新排序
- [ ] 测试通过
- [ ] 版本更新为 0.1.1
- [ ] progress.md 有记录
