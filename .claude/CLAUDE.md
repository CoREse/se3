# se3.0

## 框架
本项目采用 SE 3.0 框架。
完整规范参见 [.claude/SE3.md](.claude/SE3.md)

## 自举规则（CRITICAL）

本项目是 SE3 框架本身的开发仓库——一个自举系统。`.claude/SE3.md` 是**当前项目运行所依据的框架版本**，不是开发中的草稿。

**禁止直接修改 `.claude/SE3.md`。** 对框架规范的变更必须：
1. 先在 `openspec/specs/` 和 `output/SE3.md.template` 中开发
2. 通过 `se3 update` 从 template 生成并替换 `.claude/SE3.md`
3. 这样 `.claude/SE3.md` 始终代表一个明确的、已发布的框架版本

**原因**：如果直接改 `.claude/SE3.md`，就无法区分"项目当前遵循的规则"和"正在开发中的规则"。这对自举系统是致命的——agent 会困惑于自己到底该遵循哪套标准。

## 版本规则

SE3 框架使用语义化版本号 `MAJOR.MINOR`：

- **MAJOR**（如 1.0 → 2.0）：不向后兼容的框架变更。现有项目的 `.claude/SE3.md` 需要人工审核后才能升级。
  - 例：删除或重命名核心概念、改变 session protocol 的基本流程
- **MINOR**（如 1.0 → 1.1）：向后兼容的增量变更。现有项目可以直接 `se3 update` 升级。
  - 例：新增 CLI 命令、新增可选 spec、改进已有功能

**版本来源**：`output/SE3.md.template` 是框架的开发版本。`se3 update --se3-version X.Y` 将其发布为正式版本写入 `.claude/SE3.md`。

**变更记录**：每次版本升级时，在 `README.md` 的 Version History 中追加条目。

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
