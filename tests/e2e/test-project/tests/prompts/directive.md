# Directive Mode Test Prompt

## Mode
`se3 run --type=directive`

## Test Prompt
```
给 task list 命令添加一个 --status 过滤选项，允许用户按状态筛选任务。
支持以下值：
- all: 显示所有任务（默认）
- pending: 只显示未完成的任务
- done: 只显示已完成的任务

实现要求：
1. 添加 --status/-s 选项到 list 命令
2. 根据筛选条件过滤显示的任务
3. 添加对应的测试用例
4. 更新 spec 添加新的场景
```

## Expected Workflow Steps
1. **analyze** - 识别为 directive
2. **read_spec** - 读取相关 spec
3. **plan_tasks** - 分解任务
4. **implement** - 实现功能
5. **test** - 运行测试
6. **verify_spec** - 验证实现
7. **commit** - 提交变更，版本 bump 到 0.2.0
8. **summarize** - 生成总结

## Expected Changes
- `src/task_cli/cli.py`: list 命令添加 --status 选项
- `tests/test_cli.py`: 添加过滤功能测试
- `se3/specs/task-cli/spec.md`: 添加过滤场景

## Verification
- [ ] `task list --status pending` 只显示未完成任务
- [ ] `task list --status done` 只显示已完成任务
- [ ] 测试通过
- [ ] 版本更新为 0.2.0
- [ ] progress.md 有记录
