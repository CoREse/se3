# SE3 Daemon 与中心服务器

SE3 的核心（`se3 run`、`se3 sync` ……）是一次性的 CLI：每个命令在前台运行、
随即退出。**daemon** 与**中心服务器**在该核心之上,叠加了一个可选的、常驻的
控制面:

- **`se3 daemon`** —— 每台机器上的常驻进程。它发现并监管本机的 `se3 run` 流程,
  可代为远端调用者 spawn 新流程,把每个流程在磁盘上的状态聚合为单一快照,并
  (可选地)维持一条到中心服务器的出站连接。
- **`se3-server`** —— 独立的中心服务器,接受任意数量 daemon 的连接,把它们的
  快照合并为多机 / 多流程视图,暴露 REST API,并提供自带的网页前端。

两者都完全可选。普通的 `pip install se3` 加上 `se3 run` 不需要其中任何一个。

## 目录

1. [快速上手](#快速上手)
   - [安装](#安装)
   - [`se3 daemon` 命令](#se3-daemon-命令)
   - [`se3-server` 命令](#se3-server-命令)
   - [一次典型会话](#一次典型会话)
2. [部署与运维](#部署与运维)
   - [出站连接模型](#出站连接模型)
   - [前台模式 vs 后台(detached)模式](#前台模式-vs-后台detached模式)
   - [运行时文件:pidfile 与状态文件](#运行时文件pidfile-与状态文件)
   - [流程的发现与监管](#流程的发现与监管)
3. [架构与工作原理](#架构与工作原理)
   - [daemon 内部](#daemon-内部)
   - [中心服务器内部](#中心服务器内部)
   - [端到端:从远端机器发布任务](#端到端从远端机器发布任务)
4. [网页前端](#网页前端)

---

## 快速上手

### 安装

daemon 随核心 `se3` 包一起发布 —— 普通安装之后 `se3 daemon` 即可开箱即用。
**中心服务器**会引入较重的 web 依赖(FastAPI、uvicorn、websockets),因此这些
依赖被收进一个可选的 `server` extra:

```bash
# 核心安装 —— 已包含 `se3 daemon` 命令。
pip install se3

# 加装中心服务器(`se3-server`)与 daemon 的 WebSocket 客户端。
pip install 'se3[server]'
```

daemon 的*出站客户端*在拨入服务器时同样依赖 `server` extra。未带 `--server-url`
启动的 daemon 纯本地运行,完全不触及该 extra;带了 `--server-url` 但未安装该
extra 的 daemon 会记录一条安装提示,并降级为纯本地运行,而不是崩溃。

### `se3 daemon` 命令

`se3 daemon` 是核心 CLI 的一个子命令组(并非独立的二进制):

```bash
se3 daemon start                          # 启动 daemon(脱离终端的后台进程)
se3 daemon start --foreground             # 在当前终端中运行 daemon
se3 daemon start --server-url ws://host   # 启动并拨入中心服务器
se3 daemon stop                           # 停止运行中的 daemon
se3 daemon status                         # 显示运行状态与已跟踪的流程
se3 daemon status --json                  # 以 JSON 形式输出状态
```

| 子命令 | 选项 | 行为 |
|--------|------|------|
| `start` | `--server-url <url>`、`--foreground` | 启动 daemon。默认以**脱离终端的后台进程**启动;`--foreground` 则改为在当前终端中运行。`--server-url` 记录 daemon 要拨入的中心服务器地址。若已有 daemon 在运行,命令会报告并以非零码退出。 |
| `stop` | —— | 停止运行中的 daemon(发送 `SIGTERM` 并等待其退出)。若没有 daemon 在运行,报告 `not running` 并以 `0` 退出;若进程在宽限期内未退出,报告停止超时并以非零码退出。 |
| `status` | `--json`、`-j` | 报告 daemon 是否在运行、其 pid、机器 id、配置的服务器地址,以及已跟踪流程的列表。`--json` 以 JSON 形式输出同样的信息,而非渲染的面板。 |

### `se3-server` 命令

中心服务器通过它自己的 `console_scripts` 入口 `se3-server` 启动(由
`se3[server]` extra 安装):

```bash
se3-server                                # 绑定到 127.0.0.1:8080(默认值)
se3-server --host 0.0.0.0 --port 9000     # 监听所有网卡,端口 9000
se3-server --log-level debug              # 提高 uvicorn 日志级别
```

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `127.0.0.1` | 服务器绑定主机。用 `0.0.0.0` 可接受来自其他机器的连接。 |
| `--port` | `8080` | 绑定端口。 |
| `--log-level` | `info` | uvicorn 日志级别。 |

`se3-server` 在前台运行并阻塞。若需常驻部署,请把它放在进程管理器(systemd、
supervisor、容器 ……)下运行。

### 一次典型会话

```bash
# 在一台工作机上 —— 启动一个拨入你的中心服务器的 daemon。
pip install 'se3[server]'
se3 daemon start --server-url ws://control.example.com:8080
se3 daemon status

# 在托管控制面的机器上 —— 启动服务器。
pip install 'se3[server]'
se3-server --host 0.0.0.0 --port 8080

# 然后在浏览器中打开 http://control.example.com:8080,即可观察每台已连接
# 机器及其流程,并发布新任务。

# 结束时:
se3 daemon stop
```

---

## 部署与运维

### 出站连接模型

daemon 与服务器之间只朝一个方向连接:**由 daemon 拨出到服务器**,经一条
WebSocket。服务器永远不会反向主动连接 daemon。

```
  机器 A                             机器 B
  ┌─────────────┐                    ┌─────────────┐
  │ se3 daemon  │──┐              ┌──│ se3 daemon  │
  └─────────────┘  │  出站         │  └─────────────┘
                   │  WebSocket   │
                   ▼   /ws        ▼
              ┌────────────────────────┐
              │       se3-server       │
              │   (聚合 A + B ……)       │
              └────────────────────────┘
```

这一设计带来两个实际好处:

- **对 NAT 友好。** 工作机永远不需要入站端口或公网地址。只要 daemon 能访问到
  服务器,它就能加入控制面 —— 笔记本、NAT 后的机器、云实例都一视同仁。
- **有韧性。** 连接断开时,daemon 会以**指数退避**自动重连(从 1 秒起,每次翻倍,
  上限 60 秒)。每次(重新)连上后,它都会重新自报身份并立即推送一份完整的状态
  快照,因此服务器不会停留在陈旧状态上。

连上之后,daemon 每隔几秒推送一次状态快照,并应答服务器的心跳 ping;服务器把
任务发布与调用响应指令经同一条 socket 下发回来。

未带 `--server-url` 启动的 daemon 会跳过上述全部环节 —— 它不开启任何出站连接,
只在本地监管并聚合流程。

### 前台模式 vs 后台(detached)模式

`se3 daemon start` 支持两种模式:

- **后台(默认)。** daemon 通过双重 fork 完全脱离终端:其父进程变为 `init`,
  因此它不会成为启动它的 shell 的僵尸进程,也能在终端关闭后继续存活。
  `se3 daemon start` 在 daemon 占用其 pidfile 之后立即返回。标准流被重定向到
  一个日志文件(见下文)。
- **前台(`--foreground`)。** daemon 在当前终端中运行,`se3 daemon start` 会
  阻塞直到它停止。适用于调试、在期望非脱离进程的进程管理器(systemd、Docker
  ……)下运行,以及实时查看 daemon 日志。

两种模式下,每个 pid 目录都只能运行一个 daemon —— 第二次 `se3 daemon start`
会检测到存活的 pidfile 并以非零码退出。

### 运行时文件:pidfile 与状态文件

daemon 默认把运行时文件放在 `~/.se3/`。该目录可用 `SE3_DAEMON_DIR` 环境变量
覆盖(便于测试,或并排运行相互隔离的多个 daemon):

| 文件 | 用途 |
|------|------|
| `~/.se3/daemon.pid` | pidfile。保存 daemon 的 pid、启动时间、服务器地址与机器 id。用于防止重复启动,也是 `stop` / `status` 的事实来源。干净关停时被删除。 |
| `~/.se3/daemon_status.json` | 最新的聚合状态快照,每次轮询时被改写。`se3 daemon status` 读取它来列出已跟踪流程,无需联系 daemon 进程本身。干净关停时被删除。 |
| `~/.se3/daemon.log` | 脱离终端(后台)运行的 daemon 的日志输出 —— 其 stdout 与 stderr 被重定向到这里。 |

pidfile 与状态文件都以原子方式写入(临时文件 + rename),因此写到一半崩溃也不会
损坏它们。

### 流程的发现与监管

daemon 在本机上跟踪两类 `se3 run` 流程:

- **spawn 出来的流程** —— daemon 自己启动的流程(通常是为响应远端的任务发布
  请求)。daemon 是这些流程的父进程。
- **发现到的流程** —— 由用户在同一台机器上独立启动的 `se3 run` 进程。daemon
  通过扫描进程命令行(尽力而为,经 `psutil`)找到它们,并纳入自己的跟踪表。

对每个被跟踪的流程,daemon 从该项目的 `se3/state/engine.json` 解析出 `flow_id`。
它以固定间隔(默认每 2 秒)轮询存活状态,清除已退出进程的记录,并在关停时优雅
终止它自己 spawn 的所有流程(先 `SIGTERM`,宽限期后再 `SIGKILL`)。发现到的
流程*不会*被杀死:daemon 只监管它自己拥有的进程的生命周期。

---

## 架构与工作原理

### daemon 内部

daemon 是一个单一的、长寿命的 `asyncio` 进程,由四个部件组成:

- **Supervisor(监管器)** —— 发现并跟踪本机的 `se3 run` 进程(spawn 出来的 +
  发现到的),轮询存活状态并回收已退出的流程。
- **Spawner(spawn 器)** —— 以 `se3 run <task> --type <type>
  --output-format json` 子进程的形式启动新流程。daemon 是每个流程的*父进程*,
  绝不在进程内调用,因此 daemon 崩溃绝不会连带拖垮某个流程。子进程的
  stdout/stderr 被重定向到 `<project_root>/se3/logs/daemon/` 下的 per-flow 日志
  文件(子进程发出结构化的 NDJSON 事件流,daemon 之后可对其做 tail)。
- **Aggregator(聚合器)** —— 一个纯粹的磁盘文件读取者。它轮询每个被跟踪项目的
  `se3/state/`(引擎状态 + 摘要)、`se3/calls/`(人工调用队列)、`se3/logs/` 与
  `se3/issues/`,把它们折叠成单一的 `MachineStatus` 快照:每个流程的进度、当前
  step、待处理的调用、日志与 issue 计数。它从不伸手进流程的进程内部。
- **Client(客户端)** —— 可选的、到中心服务器的出站 WebSocket 客户端。它推送
  `MachineStatus` 快照、应答心跳,并把入站指令(`SPAWN_FLOW` → spawner、
  `RESPOND_CALL` → 一个 `se3/calls/` 响应文件)路由回本机。

监管器、聚合器轮询循环与出站客户端都作为并发任务跑在 daemon 的单一事件循环上,
并共享同一个优雅停止信号。

### 中心服务器内部

`se3-server` 是一个 FastAPI 应用,它:

- 在 `/ws` 上接受 daemon 的 WebSocket 连接,校验每个 daemon 的开场握手,并维护
  一个带心跳的 `machine_id → 连接` 连接池;
- 维护一份内存中的**多机 / 多流程**聚合视图 —— `ServerState` —— 由 daemon 推送
  的 `MachineStatus` 快照构建(本次交付没有数据库;状态随 daemon 重连而重建);
- 暴露一组 REST API 用于查询该视图并对其执行操作:`GET /api/machines`、
  `GET /api/machines/{id}/flows`、`GET /api/flows/{id}`、`POST /api/flows`
  (发布新任务)、`POST /api/flows/{id}/respond`(应答某个流程待处理的
  介入/调用),外加 `GET /api/health`;
- 提供自带的网页前端,以及位于 `/ws/ui` 的前端 WebSocket。

daemon↔服务器的线上协议有单一事实来源 —— `se3.daemon.protocol` 模块 —— 由两端
共同 import,因此 schema 不会漂移。

### 端到端:从远端机器发布任务

当你从网页前端(或直接经 `POST /api/flows`)发布一个任务时,完整链路是:

1. 浏览器 / API 客户端把任务、目标 `machine_id` 与任务类型发给**服务器**。
2. 服务器查出该机器存活的 daemon 连接,把一条 `SPAWN_FLOW` 指令*沿着*既有的
   出站 WebSocket 下发。
3. 目标 **daemon** 收到 `SPAWN_FLOW`,让它的 spawner 在请求指定的项目中启动一个
   真实的 `se3 run --output-format json` 子进程。
4. 这个新流程的运行方式与任何本地 `se3 run` 完全一样。它在磁盘上的状态会在
   daemon 的**聚合器**下一次轮询时被拾取。
5. daemon 向服务器推送一份更新后的 `MachineStatus` 快照,服务器将其合并进
   `ServerState`,并向每个已连接的网页前端广播该变更。

应答某个流程待处理的介入/调用走的是镜像路径:
`POST /api/flows/{id}/respond` → 服务器 → `RESPOND_CALL` 沿 socket 下发 →
daemon 向该项目的 `se3/calls/` 队列写入一个响应文件,从而解除被暂停流程的阻塞。

---

## 网页前端

`se3-server` 自带一个小巧的、纯静态的网页前端(`index.html`、`style.css`、
`app.js` —— 无构建步骤)。它被挂载在服务器根路径上,因此只要 `se3-server` 在
运行,你直接在浏览器中打开服务器地址即可:

```
http://<server-host>:<port>/        # 例如 http://127.0.0.1:8080/
```

页面经 `/ws/ui` WebSocket 连回服务器。服务器把完整的机器列表沿该 socket 下推:
连上时先发一份初始 `snapshot`,此后每当任一 daemon 的状态变化就发一次
`status_update` —— 因此视图无需轮询即可实时更新。

在前端中你可以:

- **查看流程进度。** 左侧面板列出每台已连接的机器;选中一台即可看到它的流程,
  每个流程都带有当前 step、进度,以及日志 / issue 计数。打开某个流程可看其详情
  抽屉。
- **远程发布任务。** **+ New Task** 按钮会打开一个对话框,用于挑选目标机器与
  任务类型并填写任务描述。提交后即触发上文描述的端到端发布链路 —— 任务在选定的
  远端机器上运行。
- **响应介入/调用。** 当某个流程暂停等待人工输入时,它会在 UI 中浮现一条待处理
  的调用。响应对话框让你键入答复并发送;服务器把它作为 `RESPOND_CALL` 路由给
  拥有该流程的 daemon,daemon 将响应写入该项目的 `se3/calls/` 队列并恢复流程。

前端在 WebSocket 上是只读的(它只*监听*状态推送);New Task 与响应操作走的是
服务器的 REST API。
