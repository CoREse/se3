# Discovery Mode Test Prompt

## Mode
`se3 run --discover`

## Test Prompt
```
我想给 task-cli 添加一些数据导出功能，但还没想清楚具体要怎么做。
```

## Expected Workflow Steps
1. **discovery** - 多轮对话探索需求
   - AI 询问：导出什么格式？JSON/CSV？
   - AI 询问：导出全部还是支持筛选？
   - AI 询问：需要导入功能吗？
2. 用户回答后进入 **analyze**
3. 后续步骤根据最终确定的方案决定

## Expected Discovery Questions
- 希望支持哪些导出格式？(JSON, CSV, Markdown...)
- 是导出所有任务还是支持筛选？
- 需要相应的导入功能吗？
- 导出文件保存到哪里？

## Verification
- [ ] Discovery 对话正常进行
- [ ] AI 提出澄清问题
- [ ] 生成精炼的任务描述
- [ ] 用户确认后进入正常流程
