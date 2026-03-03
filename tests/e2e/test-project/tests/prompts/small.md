# Small Mode Test Prompt

## Mode
`se3 run --type=small`

## Test Prompt
```
在 README.md 中添加一个使用示例部分，展示如何添加任务、列出任务、
标记完成和删除任务的完整流程。保持简洁明了。
```

## Expected Workflow Steps
1. **analyze** - 识别为 small 变更
2. **implement** - 修改 README.md
3. **test** - 运行测试（如配置了文档测试）
4. **commit** - 提交变更
5. **summarize** - 生成总结

## Expected Changes
- `README.md`: 添加使用示例部分

## Verification
- [ ] README.md 包含使用示例
- [ ] 示例展示完整工作流程
- [ ] 版本不变（small 任务不 bump 版本）
- [ ] progress.md 有记录
