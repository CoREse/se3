# SE3 Loop 增加迭代总结功能

## 需求

给 `se3 loop` 增加默认行为：每次迭代结束后，调用 Claude Code 简单总结一下上一次迭代的内容，传递给下一个迭代。然后加上参数可以关闭这个行为。

## 实现方案

### 1. CLI 参数添加
- 在 `cli.py` 的 `loop_cmd` 函数中添加 `--no-summary` 参数
- 默认行为是开启总结（即不带参数时启用总结）
- 使用 `--no-summary` 来关闭总结功能

### 2. Loop 核心逻辑修改
- 修改 `commands/loop.py` 中的 `run_exclusive_loop` 函数
- 每次迭代结束后，调用 Claude Code 生成总结
- 将总结内容传递给下一个迭代的 prompt

### 3. 总结生成方式
- 在每次迭代结束后，读取该 change 目录下的相关文件（tasks.md, work.md 等）
- 调用 Claude Code 生成简短的总结（限制 token 数量）
- 将总结保存到内存中，用于下一次迭代

## 任务清单

- [x] 添加 CLI 参数 `--no-summary`
- [x] 实现迭代总结生成逻辑
- [x] 将总结传递给下一个迭代
- [x] 测试功能
- [x] 提交更改
- [x] 更新框架版本并同步 .claude/

## 提交记录

- `950561a` feat(loop): add iteration summary feature
- `6f3eee2` chore: bump SE3 framework to 2.11.0
