# Feature Mode Test Prompt

## Mode
`se3 run --type=feature` (or auto-detected)

## Test Prompt
```
实现一个任务搜索功能，让用户可以通过关键词搜索任务标题。需要添加一个新的 
`task search <keyword>` 命令，它会遍历所有任务并显示标题包含关键词的任务。
搜索结果应该高亮显示匹配的关键词。
```

## Expected Workflow Steps
1. **analyze** - 识别为 feature 请求
2. **read_spec** - 读取 task-cli spec
3. **propose** - 生成变更提案（添加 search 命令）
4. **design** - 设计搜索功能（关键词匹配、高亮显示）
5. **plan_tasks** - 分解任务
6. **implement** - 实现代码
7. **test** - 运行测试
8. **verify_spec** - 验证实现
9. **update_spec** - 更新 spec 记录新功能
10. **commit** - 提交变更，版本 bump 到 0.2.0
11. **summarize** - 生成总结

## Expected Changes
- `src/task_cli/cli.py`: 添加 `search` 命令
- `tests/test_cli.py`: 添加搜索功能测试
- `se3/specs/task-cli/spec.md`: 添加 search 场景

## Verification
- [ ] `task search` 命令可用
- [ ] 可以按关键词搜索
- [ ] 测试通过
- [ ] 版本更新为 0.2.0
- [ ] progress.md 有记录
