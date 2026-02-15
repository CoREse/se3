# se3.0

## 框架
本项目采用 SE 3.0 框架。
完整规范参见 [.claude/SE3.md](.claude/SE3.md)

## Git Commit 规则（覆盖默认行为）

本项目遵循 SE3 commit 规范。当一个有意义的工作单元完成且测试通过时，**必须主动提交**，不需要等待用户显式要求。这覆盖 Claude Code 默认的"不主动提交"行为。

具体规则：
- 测试通过后主动 commit
- 不要将不相关的改动混入同一个 commit
- commit message 必须包含下一次 session 的上下文

## 技术栈
- 语言: Python 3
- CLI 框架: Typer
- 测试: pytest
