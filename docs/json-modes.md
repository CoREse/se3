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

```python
response = caller.call(prompt=prompt, require_json=True)
# 或者
response = caller.call(prompt=prompt, json_mode="strict")
```

**流程：**
```
Prompt + JSON约束 → LLM调用 → 解析JSON
                         ↓ 失败
                     重试调用（最多3次）
```

**特点：**
- 在 prompt 中添加强制性 JSON 指令
- 如果输出不是有效 JSON，会重新调用 LLM
- 每次重试都消耗完整的 LLM 调用

**适用：**
- `analyze` - 任务分类，输出简单
- `verify_spec` - 验证结果，格式规整

---

### 2. EXTRACT 模式

```python
response = caller.call(
    prompt=prompt,
    json_mode="extract",
    json_schema_hint='{"task_type": "...", "files": []}'
)
```

**流程：**
```
Prompt + JSON约束 → LLM调用 → 解析JSON
                         ↓ 失败
                     LLM提取（轻量级调用）
```

**特点：**
- 在 prompt 中要求 JSON 格式（和 STRICT 一样）
- **不重试**主调用，而是使用第二次轻量级 LLM 调用来提取 JSON
- 节省 token（提取调用比重新生成便宜）

**适用：**
- `propose` - 提案生成，偶尔格式出错
- `plan_tasks` - 任务规划，结构复杂但不算太大

---

### 3. TWO_PHASE 模式

```python
response = caller.call(
    prompt=prompt,
    json_mode="two_phase",
    # 或者 two_phase_json=True（向后兼容）
    json_schema_hint='{"files_changed": [{"path": "...", "content": "..."}]}'
)
```

**流程：**
```
Clean Prompt → LLM调用（自然生成）→ LLM提取 → JSON输出
                 无JSON约束
```

**特点：**
- **完全不添加 JSON 约束到 prompt**
- LLM 可以自然表达，不受格式限制
- 使用第二次 LLM 调用从自然语言中提取结构化数据
- 最适合处理大输出（如文件内容）

**适用：**
- `implement` - 代码实现，大文件内容，最容易截断
- 任何需要生成大量文本然后结构化的场景

---

## 成本对比

假设单次 LLM 调用成本为 1 单位：

| 场景 | STRICT | EXTRACT | TWO_PHASE |
|------|--------|---------|-----------|
| 成功（JSON 有效） | 1 | 1 | 2 |
| 失败需恢复 | 2-4（重试） | 1.2（提取） | 2（已包含提取） |
| **平均成本** | 1.2 | 1.1 | 2 |

**结论：**
- STRICT 有重试成本，但在简单场景最经济
- EXTRACT 在失败时比重试便宜，适合中等复杂度
- TWO_PHASE 成本固定为 2，但最可靠，适合复杂输出

---

## 使用建议

### 按步骤选择

```python
# analyze - 简单输出，用 STRICT
response = caller.call(prompt=prompt, json_mode="strict")

# propose - 中等复杂度，用 EXTRACT
response = caller.call(prompt=prompt, json_mode="extract")

# implement - 大输出，用 TWO_PHASE
response = caller.call(prompt=prompt, json_mode="two_phase")

# summarize - 不需要 JSON，用 off
response = caller.call(prompt=prompt, json_mode="off")
```

### 向后兼容

旧代码无需修改：

```python
# 这些仍然有效
response = caller.call(prompt=prompt, require_json=True)  # -> STRICT
response = caller.call(prompt=prompt, two_phase_json=True)  # -> TWO_PHASE
```

---

## 实现细节

### 模式解析优先级

```python
# 1. 显式 json_mode 参数优先
response = caller.call(prompt=prompt, json_mode="extract", require_json=True)
# 结果：EXTRACT 模式

# 2. 其次 two_phase_json 标志
response = caller.call(prompt=prompt, two_phase_json=True)
# 结果：TWO_PHASE 模式

# 3. 最后 require_json 标志
response = caller.call(prompt=prompt, require_json=True)
# 结果：STRICT 模式

# 4. 默认
response = caller.call(prompt=prompt)
# 结果：OFF 模式（不要求 JSON）
```

### Schema Hint

`json_schema_hint` 帮助提取 LLM 理解期望的输出格式：

```python
response = caller.call(
    prompt=prompt,
    json_mode="extract",
    json_schema_hint='{"files_changed": [{"path": "...", "action": "create|modify"}]}'
)
```

这在 EXTRACT 和 TWO_PHASE 模式中特别有用。
