# Tasks: SE3 Loop Stream-JSON Renderer

## Completed

- [x] 分析现有 `loop.py` 实现
- [x] 设计 `STREAM_JSON_RENDERER` 嵌入脚本
- [x] 实现消息类型处理：
  - [x] `thinking` - 思考内容显示
  - [x] `tool_use` - 工具调用显示
  - [x] `tool_result` - 工具结果显示
  - [x] `output` / `message` - 内容输出
  - [x] `error` - 错误处理
- [x] 修改 `generate_loop_script()` 生成渲染器
- [x] 将 `--print` 改为 `--stream-json | python3 renderer`
- [x] 运行语法检查
- [x] 运行项目测试套件
- [x] 创建 change 文档

## Files Changed

- `tools/se3_tools/commands/loop.py` - 主要实现

## Verification

```bash
# Python 语法检查
python3 -m py_compile tools/se3_tools/commands/loop.py

# Bash 脚本检查
bash -n /tmp/test_loop.sh

# 项目测试
python -m pytest tests/ -q
# Result: 219 passed
```
