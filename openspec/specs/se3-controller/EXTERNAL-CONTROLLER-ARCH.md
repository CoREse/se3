# SE3 External Controller Architecture

## 问题分析

### 当前架构的痛点

1. **嵌套限制**: `se3 collab --daemon` 在 Claude Code 内部运行时，worker 无法启动（Claude 禁止嵌套）
2. **提交遗漏**: Claude 没有确定性机制确保提交，依赖自我提醒
3. **进程管理**: bash orchestrator 功能受限（信号处理、状态恢复困难）

### 根本原因

控制权在 **Claude 内部** → 外部只能通过文件/信号间接控制 → 不可靠

## 新架构：外部控制模型

```
┌─────────────────────────────────────────────────────────────┐
│                      User Terminal                          │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  $ se3 session start                               │   │
│  │  [External Controller Daemon starts]               │   │
│  │  [Auto-commit watcher starts]                      │   │
│  │  [Claude Process spawned in subprocess]            │   │
│  └─────────────────────────────────────────────────────┘   │
└────────────────────┬──────────────────────────────────────┘
                     │ stdio/pty
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                   Claude Interactive Mode                    │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  User <-> Claude 交互（正常对话模式）               │   │
│  │  Claude 通过 MCP/文件与 External Controller 通信    │   │
│  └─────────────────────────────────────────────────────┘   │
└────────────────────┬──────────────────────────────────────┘
                     │ MCP / File System
                     ▼
┌─────────────────────────────────────────────────────────────┐
│              External Controller Daemon (Python)            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │ Session     │  │ Auto-Commit │  │ Worker Coordinator │ │
│  │ Manager     │  │ Watcher     │  │ (for collab mode)  │ │
│  └─────────────┘  └─────────────┘  └─────────────────────┘ │
│  ┌─────────────┐  ┌─────────────┐                          │
│  │ Status      │  │ Change      │                          │
│  │ Persistence │  │ Detector    │                          │
│  └─────────────┘  └─────────────┘                          │
└─────────────────────────────────────────────────────────────┘
```

## 核心组件

### 1. Session Controller (`se3 session`)

```python
class SessionController:
    """
    管理单个 Claude 会话的生命周期
    """

    def start(self):
        # 1. 启动 Claude 作为子进程（交互模式）
        # 2. 连接 stdio/pty
        # 3. 启动 auto-commit watcher
        pass

    def pause(self):
        # 保存状态，暂停 Claude 进程（非终止）
        pass

    def resume(self):
        # 恢复之前的会话状态
        pass

    def stop(self):
        # 确保提交后终止
        pass
```

#### CLI 接口

```bash
# 启动新会话
se3 session start

# 暂停当前会话（保存上下文）
se3 session pause

# 恢复会话
se3 session resume

# 强制提交并停止
se3 session stop --commit

# 查看状态
se3 session status
```

### 2. Auto-Commit Watcher

```python
class AutoCommitWatcher:
    """
    基于启发式规则的自动提交
    """

    def should_commit(self) -> bool:
        # 触发条件（满足任一）：
        # 1. 文件修改后 5 分钟无新修改（静默期）
        # 2. 测试通过且未提交（se3 commit 会运行测试）
        # 3. 用户显式发送 /commit 命令
        # 4. 即将停止会话时（SIGTERM）
        pass

    def commit_with_message(self):
        # 使用 AI 生成提交信息（可选）
        # 或基于修改的文件类型推断
        pass
```

### 3. Worker Coordinator (替代 collab --daemon)

```python
class WorkerCoordinator:
    """
    在当前 Claude 外部协调多个 Claude 进程
    解决嵌套限制问题
    """

    def spawn_worker(self, task_id: str):
        # 启动独立 Claude 进程作为 worker
        # 通过文件系统/MCP 通信
        pass

    def spawn_manager(self, event_type: str):
        # 启动 manager 审核结果
        pass
```

## 数据流

### 正常交互流

```
User Input
    │
    ▼
┌─────────────────┐
│ se3 session     │
│ (stdio proxy)   │
└────────┬────────┘
         │
    ┌────┴────┐
    ▼         ▼
┌────────┐  ┌──────────────┐
│ Claude │  │ Auto-Commit  │
│ Process│  │ Watcher      │
└───┬────┘  │ (file watch) │
    │       └──────┬───────┘
    │              │
    │  modifies    │ detects
    │  files       │ silence
    │              │
    └──────────────┘
              │
              ▼
        ┌──────────┐
        │ se3 commit│
        │ (auto)   │
        └──────────┘
```

### 协作模式流

```
External Controller (Daemon)
    │
    ├── spawn_manager("plan") → 独立 Claude 进程
    │                            │
    │                            ▼
    │                      [Manager: 生成 tasks]
    │                            │
    │◄───────────────────────────┘
    │
    ├── spawn_worker("task-001") → 独立 Claude 进程
    │                               │
    │                               ▼
    │                         [Worker: 实现任务]
    │                               │
    │◄──────────────────────────────┘
    │
    └── spawn_manager("review") → ...
```

## 关键设计决策

### 1. 为什么用外部 Daemon 而不是当前进程？

| 方案 | 优点 | 缺点 |
|------|------|------|
| Claude 内部控制 | 简单 | 嵌套限制、提交遗漏 |
| 外部 Daemon | 无嵌套限制、确定性提交 | 需要进程间通信 |

### 2. 自动提交触发策略

```python
COMMIT_TRIGGERS = {
    # 时间触发
    "silence_timeout": 300,  # 5 分钟无文件修改

    # 事件触发
    "tests_pass": True,      # 测试通过后
    "user_command": "/commit",  # 用户显式命令
    "session_end": True,     # 会话结束前强制提交

    # 内容触发
    "completed_task": True,  # progress.md 标记任务完成
}
```

### 3. 与现有 collab 的整合

```
se3 collab --daemon    →    se3 collab start
                              (由外部 controller 管理)

当前架构：                 新架构：
bash orchestrator    →    Python daemon
├─ spawn manager     →    ├─ spawn_claude("manager")
├─ spawn worker      →    ├─ spawn_claude("worker")
└─ file events       →    └─ async event loop
```

## 实现路线图

### Phase 1: Session Controller 基础
- [ ] `se3 session start/stop/status`
- [ ] Claude 子进程管理
- [ ] 基本的 stdio 代理

### Phase 2: Auto-Commit
- [ ] 文件系统监控（watchdog）
- [ ] 静默期检测
- [ ] 自动 `se3 commit` 触发

### Phase 3: Collab 迁移
- [ ] Worker Coordinator
- [ ] 独立 Claude 进程启动
- [ ] 任务状态机

### Phase 4: 增强功能
- [ ] 会话持久化（崩溃恢复）
- [ ] 多个并发会话管理
- [ ] Web UI 监控面板

## MCP 工具设计

Claude 通过 MCP 与外部 Controller 通信：

```python
# se3-controller MCP server

@mcp.tool()
def report_task_complete(task_id: str, summary: str):
    """Worker 完成任务时调用"""
    pass

@mcp.tool()
def request_human_input(question: str, urgency: str = "normal"):
    """需要人类输入时调用"""
    pass

@mcp.tool()
def trigger_commit(reason: str):
    """Claude 显式请求提交"""
    pass

@mcp.tool()
def spawn_worker_task(task_spec: dict):
    """Claude 请求启动 worker"""
    pass
```

## 与现有代码的兼容性

- `se3 collab` 命令保持不变（内部调用 controller）
- `.collab/` 目录结构保持不变
- `scripts/collab-orchestrator.sh` 逐步迁移到 Python daemon
