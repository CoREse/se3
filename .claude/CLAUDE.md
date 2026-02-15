# se3.0

## 框架
本项目采用 SE 3.0 框架。
完整规范参见 [.claude/SE3.md](.claude/SE3.md)

## Git Commit 规则

**所有 commit 必须通过 `se3 commit` 命令执行，禁止直接使用 `git commit`。**

`se3 commit` 会自动：运行测试 → 检查敏感文件 → 暂存 → 提交。

```bash
# 标准用法
se3 commit -m "Add auth module" -f "src/auth.py tests/test_auth.py"

# 自动暂存所有已跟踪的修改
se3 commit -m "Fix login bug"

# 预览（不实际提交）
se3 commit --dry-run
```

当一个有意义的工作单元完成时，主动调用 `se3 commit`，不需要等待用户显式要求。

## 技术栈
- 语言: Python 3
- CLI 框架: Typer
- 测试: pytest
