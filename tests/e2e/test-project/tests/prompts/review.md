# Review Mode Test Prompt

## Mode
`se3 run --type=review`

## Test Prompt
```
审查当前的 task-cli 代码实现，检查是否符合 spec 中的要求。
特别关注：
1. 是否实现了所有 spec 中定义的命令
2. 错误处理是否完善
3. 代码风格是否符合项目约定
4. 测试覆盖率是否充分

请提供详细的审查报告。
```

## Expected Workflow Steps
1. **analyze** - 识别为 review 请求
2. **read_spec** - 读取 task-cli spec
3. **verify_spec** - 审查代码实现 vs spec
4. **summarize** - 生成审查报告

## Expected Output
- 审查报告，包含：
  - 已实现的功能列表
  - 发现的问题（如有）
  - 改进建议（如有）

## Verification
- [ ] 审查报告生成
- [ ] 报告包含功能对比
- [ ] 报告包含问题/建议
- [ ] 无代码变更（review 模式不修改代码）
- [ ] progress.md 有记录
