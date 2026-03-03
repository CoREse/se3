# SE3 测试流程文档

本文档详细描述了如何测试 SE3 的 `se3 run` 命令的各种模式。

## 目录

1. [测试环境准备](#测试环境准备)
2. [Feature 模式测试](#feature-模式测试)
3. [Bugfix 模式测试](#bugfix-模式测试)
4. [Review 模式测试](#review-模式测试)
5. [Small 模式测试](#small-模式测试)
6. [Directive 模式测试](#directive-模式测试)
7. [Discovery 模式测试](#discovery-模式测试)
8. [测试结果验证](#测试结果验证)
9. [测试恢复](#测试恢复)

---

## 测试环境准备

### 1. 确认测试项目状态

```bash
cd tests/e2e/test-project
git status
# 应该显示 "nothing to commit, working tree clean"
```

### 2. 确认 SE3 配置

```bash
cat se3.yaml
# 确认配置正确
```

### 3. 确认 specs 存在

```bash
ls -la se3/specs/
# 应该包含 base/ 和 task-cli/
```

---

## Feature 模式测试

### 测试目标
验证完整的功能开发流程，包括所有 11 个步骤。

### 测试步骤

```bash
# 1. 记录初始版本
cat pyproject.toml | grep version
# 应该显示 version = "0.1.0"

# 2. 运行 feature 模式
se3 run "实现一个任务搜索功能，让用户可以通过关键词搜索任务标题。需要添加一个新的 task search <keyword> 命令" --type=feature

# 3. 按流程执行各步骤
# - 分析阶段：确认识别为 feature
# - 提案阶段：确认添加 search 命令的方案
# - 设计阶段：确认关键词匹配和高亮设计
# - 任务规划：确认分解为可执行任务
# - 实现阶段：确认代码实现
# - 测试阶段：确认测试通过
# - 验证阶段：确认符合 spec
# - 更新阶段：确认 spec 更新
# - 提交阶段：确认 git 提交
# - 总结阶段：确认生成总结
```

### 预期结果

- [ ] `src/task_cli/cli.py` 添加了 `search` 命令
- [ ] `tests/test_cli.py` 添加了搜索测试
- [ ] `se3/specs/task-cli/spec.md` 更新了 search 场景
- [ ] `pyproject.toml` 版本更新为 `0.2.0`
- [ ] `README.md` 版本徽章更新
- [ ] `VERSIONS.md` 添加了 0.2.0 记录
- [ ] `progress.md` 有完整记录
- [ ] git log 显示提交

---

## Bugfix 模式测试

### 测试目标
验证 bug 修复流程，确认跳过 design 步骤。

### 前置步骤（注入 bug）

```bash
# 1. 创建一个 bug：删除任务后不重新排序 ID
sed -i 's/for j, t in enumerate(tasks):/for j, t in enumerate(tasks):  # BUG/' src/task_cli/cli.py
sed -i 's/t\["id"\] = j + 1/pass  # t["id"] = j + 1  # BUG/' src/task_cli/cli.py

# 2. 提交 bug
git add -A
git commit -m "Inject bug for testing"
```

### 测试步骤

```bash
# 1. 运行 bugfix 模式
se3 run "修复删除任务后 ID 不连续的 bug。当删除一个任务后，剩余任务的 ID 应该重新排序" --type=bugfix

# 2. 按流程执行
# - 分析阶段：确认识别为 bugfix
# - 提案阶段：确认修复方案
# - 实现阶段：确认修复代码
# - 测试阶段：确认测试通过
# - 验证阶段：确认符合 spec
# - 提交阶段：确认 git 提交
```

### 预期结果

- [ ] `src/task_cli/cli.py` bug 被修复
- [ ] 测试通过
- [ ] `pyproject.toml` 版本更新为 `0.1.1`
- [ ] `VERSIONS.md` 添加了 0.1.1 记录
- [ ] `progress.md` 有记录
- [ ] 工作流程中没有 design 步骤

---

## Review 模式测试

### 测试目标
验证代码审查流程，确认不修改代码。

### 测试步骤

```bash
# 1. 运行 review 模式
se3 run "审查当前的 task-cli 代码实现，检查是否符合 spec 中的要求" --type=review

# 2. 按流程执行
# - 分析阶段：确认识别为 review
# - 规范读取：确认读取 specs
# - 验证阶段：确认代码审查
# - 总结阶段：确认生成报告
```

### 预期结果

- [ ] 生成审查报告
- [ ] 报告包含功能对比
- [ ] 报告包含问题/建议
- [ ] **无代码文件被修改**
- [ ] `progress.md` 有审查记录

---

## Small 模式测试

### 测试目标
验证小型变更流程，确认跳过 proposal/design/plan_tasks。

### 测试步骤

```bash
# 1. 运行 small 模式
se3 run "在 README.md 中添加一个使用示例部分" --type=small

# 2. 按流程执行
# - 分析阶段：确认识别为 small
# - 实现阶段：确认修改 README
# - 测试阶段：确认运行测试
# - 提交阶段：确认 git 提交
# - 总结阶段：确认生成总结
```

### 预期结果

- [ ] `README.md` 添加了使用示例
- [ ] 版本不变（仍为 0.1.0）
- [ ] `progress.md` 有记录
- [ ] 工作流程中没有 proposal/design/plan_tasks 步骤

---

## Directive 模式测试

### 测试目标
验证指令执行流程。

### 测试步骤

```bash
# 1. 运行 directive 模式
se3 run "给 task list 命令添加一个 --status 过滤选项，支持 all/pending/done" --type=directive

# 2. 按流程执行
# - 分析阶段：确认识别为 directive
# - 规范读取：确认读取 specs
# - 任务规划：确认分解任务
# - 实现阶段：确认代码实现
# - 测试阶段：确认测试通过
# - 验证阶段：确认符合 spec
# - 提交阶段：确认 git 提交
```

### 预期结果

- [ ] `src/task_cli/cli.py` list 命令有 `--status` 选项
- [ ] 测试通过
- [ ] 版本更新（minor bump）
- [ ] `progress.md` 有记录

---

## Discovery 模式测试

### 测试目标
验证需求探索流程。

### 测试步骤

```bash
# 1. 运行 discovery 模式
se3 run --discover "我想给 task-cli 添加一些数据导出功能"

# 2. 预期 AI 会询问：
# - 希望支持哪些导出格式？(JSON, CSV, Markdown...)
# - 是导出所有任务还是支持筛选？
# - 需要相应的导入功能吗？

# 3. 回答 AI 的问题

# 4. 确认后进入正常流程
```

### 预期结果

- [ ] AI 提出澄清问题
- [ ] 生成精炼的任务描述
- [ ] 用户确认后继续
- [ ] 支持中断和恢复

---

## 测试结果验证

### 通用验证清单

每个测试完成后，检查以下内容：

```bash
# 1. 检查 git 状态
git status
git log --oneline -5

# 2. 检查版本
python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"

# 3. 检查 progress.md
head -100 progress.md

# 4. 检查 VERSIONS.md（如存在）
cat VERSIONS.md

# 5. 运行测试
python -m pytest tests/ -v
```

### 文件变更验证

```bash
# 查看变更的文件列表
git diff --name-only HEAD~5

# 查看具体变更
git diff HEAD~5
```

---

## 测试恢复

测试完成后，使用以下命令恢复项目：

```bash
# 1. 运行重置脚本
./tests/reset.sh

# 2. 验证恢复结果
git status
# 应该显示 "nothing to commit, working tree clean"
git log --oneline -1
# 应该显示 "Add SE3 configuration and specs"

# 3. 验证文件状态
ls se3/state/  # 应该为空或不存在
ls se3/tmp/    # 应该为空或不存在
```

### 手动恢复（如脚本失败）

```bash
# 1. 重置到初始提交
git reset --hard 5401add

# 2. 清理未跟踪文件
git clean -fd

# 3. 删除 SE3 运行时文件
rm -rf se3/state/ se3/tmp/ se3/logs/ se3/cache/ se3/history/
rm -f progress.md VERSIONS.md

# 4. 验证
git status
```

---

## 测试记录模板

每次测试完成后，记录以下内容：

```markdown
## 测试记录: [模式名称]

- **日期**: YYYY-MM-DD
- **测试人员**: [姓名]
- **模式**: [feature/bugfix/review/small/directive/discovery]
- **Prompt**: [使用的 prompt]

### 执行结果

- [ ] 所有步骤正常执行
- [ ] 输出符合预期
- [ ] 文件变更正确
- [ ] 版本更新正确
- [ ] progress.md 记录完整

### 问题记录

- [问题描述]: [解决方案]

### 改进建议

- [建议内容]
```
