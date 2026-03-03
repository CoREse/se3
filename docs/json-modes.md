# JSON Extraction Modes

SE3 支持三种 JSON 提取模式，以适应不同的使用场景。

## 三种模式对比

| 模式 | 参数 | Prompt 约束 | 失败处理 | 适用场景 |
|------|------|-------------|----------|----------|
| **STRICT** | `require_json=True` | 强制 JSON 格式 | 重试整个调用 | 简单输出，可靠性要求高 |
| **EXTRACT** | `json_mode="extract"` | 要求 JSON 格式 | LLM 提取（不重试） | 平衡可靠性和效率 |
| **TWO_PHASE** | `json_mode="two_phase"` | 无约束 | LLM 提取 | 复杂输出，避免提示词污染 |

## 详细说明

### 1. STRICT 模式（默认）

