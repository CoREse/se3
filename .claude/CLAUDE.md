# se3.0

## 框架
本项目采用 SE 3.0 框架。
完整规范参见 [.claude/SE3.md](.claude/SE3.md)

## 2.x 执行规则（强制）

### 会话生命周期

**开始工作时**（用户发出任何指令后）：
1. **必须**先运行 `se3 start --json` 获取会话状态
2. **必须**按顺序执行返回的 `actions` 数组中的每个 action
3. **必须**完成所有 startup actions 后才响应用户请求

**执行具体任务时**（理解用户意图后）：
1. **必须**运行 `se3 work <desc> --json` 或 `se3 work --new <type>/<name> --json`
2. **必须**按顺序执行返回的 `actions` 数组中的每个 action
3. **禁止**直接修改代码而不通过 `se3 work` 流程

**结束会话时**（用户说结束、或 `/new`、或工作完成）：
1. **必须**运行 `se3 done --json`
2. **必须**按顺序执行返回的 `actions` 数组（测试 → 提交 → handoff）
3. **禁止**直接 `git commit` 或跳过 `se3 handoff`

### Action 执行规则

每个 action 都有 `type` 和 `params`：
- `run_tests`: 运行测试，失败时必须暂停修复
- `commit`: 运行 `se3 commit`，禁止直接用 `git commit`
- `implement_task`: 实现指定任务，完成后标记 tasks.md
- `ask_user`: 使用 AskUserQuestion 工具提问
- `create_change`: 运行 `openspec new change <name>`
- `handoff`: 运行 `se3 handoff` 生成 session summary

### 错误处理

如果 `se3` 命令失败或返回错误：
1. 报告错误给用户
2. 不要绕过流程直接行动
3. 等待用户指示

## Git Commit 规则

**所有 commit 必须通过 `se3 commit` 命令执行，禁止直接使用 `git commit`。**

`se3 commit` 会自动：运行测试 → 检查敏感文件 → 暂存 → 提交。

```bash
se3 commit -m "描述" -f "file1.py file2.py"
```

当一个有意义的工作单元完成时，主动调用 `se3 commit`，不需要等待用户显式要求。

## 项目约定（示例）

<!-- 取消注释并自定义以下内容 -->

<!-- ## 技术栈
- 语言: JavaScript / TypeScript
- 框架: React / Vue
- 构建工具: Vite / Webpack
- 测试: Vitest / Jest -->

<!-- ## 团队约定
- 代码风格: ESLint + Prettier
- Git 流程: GitHub Flow
- 每日站会: 09:30 在线会议 -->

<!-- ## 自定义规则
- 提交信息格式: [类型] 描述
- 代码审查: 至少 1 人审核 -->
