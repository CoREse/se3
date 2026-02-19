# Proposal: SE3 Loop Stream-JSON Renderer

## Problem

当前 `se3 loop` 使用 `--print --output-format text` 模式运行 Claude Code，输出直接打印到终端。这导致：

1. **不可见状态**：无法看到 Claude 的思考过程、工具调用详情
2. **盲跑体验**：用户不知道 Claude 在做什么，只能等待最终结果
3. **调试困难**：当 iteration 失败时，难以定位问题

## Solution

将 `se3 loop` 改为使用 `--stream-json` 模式，并嵌入纯 Python 渲染器将 JSON 流转换为人类可读的格式。

## Changes

### 1. 修改 `tools/se3_tools/commands/loop.py`

- 添加 `STREAM_JSON_RENDERER` - 嵌入的 Python 脚本
- 修改 `generate_loop_script()` 生成包含渲染器的 bash 脚本
- 将 `--print --output-format text` 改为 `--stream-json | python3 renderer.py`

### 2. 渲染器功能

处理的消息类型：

| 类型 | 显示 | 颜色 |
|------|------|------|
| `thinking` | 💭 思考内容 | 灰色暗淡 |
| `tool_use` | 🔧 工具名 + 参数 | 青色 |
| `tool_result` | ✓ 成功 / ❌ 失败 | 绿色/洋红 |
| `output` | 直接输出内容 | 默认 |
| `error` | ❌ 错误信息 | 洋红 |

### 3. 设计原则

- **零外部依赖**：只使用 Python 标准库 (`sys`, `json`)，不依赖 `jq`
- **实时渲染**：流式处理，不缓存整个输出
- **内容截断**：长文本自动截断显示，保持可读性

## Test Plan

- [x] Python 语法检查通过
- [x] Bash 脚本语法检查通过
- [x] 渲染器功能测试通过
- [x] 项目测试套件全部通过 (219 tests)

## Implementation Notes

- 渲染器脚本在运行时动态生成临时文件
- 每个 iteration 结束后自动清理临时文件
- 保留原有的 timeout (30分钟) 和错误处理逻辑
