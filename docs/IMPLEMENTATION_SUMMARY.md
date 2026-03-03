# 三模式 JSON 提取实现总结

## 实现概览

实现了三种 JSON 提取模式，支持从严格到灵活的不同策略：

| 模式 | 核心特性 | 失败处理 | 成本 |
|------|----------|----------|------|
| **STRICT** | Prompt 强制 JSON | 重试整个调用 | 可变（可能重试） |
| **EXTRACT** | Prompt 要求 JSON | LLM 提取（不重试） | 固定 + 提取成本 |
| **TWO_PHASE** | 无 Prompt 约束 | LLM 提取 | 固定 2 次调用 |

## 新增文件

### 1. `src/se3/engine/json_modes.py`
定义了 `JsonMode` 枚举和模式解析逻辑。

```python
class JsonMode(Enum):
    STRICT = "strict"      # 强制 JSON，失败重试
    EXTRACT = "extract"    # 要求 JSON，失败提取
    TWO_PHASE = "two_phase" # 无约束，两阶段提取
    OFF = "off"            # 不要求 JSON
```

### 2. `src/se3/engine/json_extractor.py`
实现了第二阶段 JSON 提取器。

```python
class JSONExtractor:
    def extract(self, raw_output: str, schema_hint: str) -> dict:
        # 1. 尝试直接解析（快速路径）
        # 2. 使用 LLM 提取（如果需要）
        # 3. 处理截断内容的补全
```

### 3. `docs/json-modes.md`
详细的使用文档，包含：
- 三种模式的详细对比
- 成本分析
- 使用建议
- 向后兼容说明

### 4. `docs/json_modes_demo.py`
可运行的演示脚本，展示：
- 模式解析逻辑
- Prompt 包装对比
- 使用示例

## 修改文件

### `src/se3/engine/llm_caller.py`

核心修改：

1. **新的 `call()` 方法签名**
```python
def call(
    self,
    prompt: str,
    ...
    require_json: bool = False,      # 向后兼容
    json_mode: Optional[str] = None, # 新的显式模式
    two_phase_json: bool = False,    # 向后兼容
    json_schema_hint: Optional[str] = None,
    ...
)
```

2. **三种模式的内部实现**
```python
def _call_strict(self, ...):     # Mode 1: 强制 + 重试
def _call_extract(self, ...):    # Mode 2: 要求 + 提取
def _call_two_phase(self, ...):  # Mode 3: 两阶段
```

3. **模式解析优先级**
```
json_mode 参数 > two_phase_json > require_json > 默认(off)
```

### `src/se3/engine/steps/implement.py`

启用 TWO_PHASE 模式用于代码实现步骤：

```python
# 修改前
response = caller.call(prompt=prompt, require_json=True)

# 修改后
response = caller.call(
    prompt=prompt,
    json_mode="two_phase",  # 或 two_phase_json=True
    json_schema_hint='{"files_changed": [...]}',
)
```

## API 使用指南

### 模式 1: STRICT（默认/向后兼容）

```python
# 方式 1: 向后兼容
response = caller.call(prompt=prompt, require_json=True)

# 方式 2: 显式指定
response = caller.call(prompt=prompt, json_mode="strict")
```

### 模式 2: EXTRACT（新）

```python
response = caller.call(
    prompt=prompt,
    json_mode="extract",
    json_schema_hint='{"task_type": "...", "files": []}'
)
```

### 模式 3: TWO_PHASE（新/推荐用于大输出）

```python
# 方式 1: 显式指定
response = caller.call(
    prompt=prompt,
    json_mode="two_phase",
    json_schema_hint='{"files_changed": [{"path": "...", "content": "..."}]}',
)

# 方式 2: 向后兼容
response = caller.call(prompt=prompt, two_phase_json=True)
```

## 成本对比

假设单次 LLM 调用成本为 1 单位：

| 场景 | STRICT | EXTRACT | TWO_PHASE |
|------|--------|---------|-----------|
| 简单任务（成功率 95%） | 1.05 | 1.01 | 2.0 |
| 复杂任务（成功率 80%） | 1.4 | 1.04 | 2.0 |
| 困难任务（成功率 50%） | 2.25 | 1.1 | 2.0 |

**建议：**
- 简单输出（<1K tokens）：STRICT
- 中等复杂度：EXTRACT
- 大输出（>5K tokens）：TWO_PHASE

## 向后兼容

所有旧代码无需修改即可运行：

```python
# 这些调用行为不变
response = caller.call(prompt=prompt)  # json_mode="off"
response = caller.call(prompt=prompt, require_json=True)  # json_mode="strict"
response = caller.call(prompt=prompt, two_phase_json=True)  # json_mode="two_phase"
```

## 下一步建议

1. **测试**：在实际项目中测试三种模式
2. **优化**：根据测试结果调整 `json_schema_hint` 的使用
3. **扩展**：考虑将 EXTRACT 模式用于 `propose` 和 `plan_tasks` 步骤
4. **监控**：添加指标收集，了解各模式的成功率和成本
