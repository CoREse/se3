# Tasks: SE3 Framework Simplification

## Goal
采用方案2：删掉 SE3.md，不指望 agent 自觉；同时实现 session guard 作为额外安全检查。

## Tasks

- [x] 修改 `output/SE3.md.template` - 方案2：大幅简化
- [x] 修改 `output/CLAUDE.minimal.md.template` - 简化为只保留 command 入口
- [x] 实现 session guard 机制（在 tools/se3_tools/commands/work.py 和 done.py 中检查 session 状态）
- [x] 运行测试验证
- [x] 提交修改

## Notes

方案2核心：
1. 删掉 SE3.md → agent 不知道"应该"怎么做
2. 用户需要 SE3 时，**手动**运行 `/se3:start`、`/se3:work`、`/se3:done`
3. 外部工具（`se3 collab`）也改为手动触发 command

Session Guard 作为额外安全检查：
- 在 `se3 work` 和 `se3 done` 中检查 session 是否已启动
- 如果未启动，报错并提示先运行 `se3 start`
