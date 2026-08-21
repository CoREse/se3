# tianluo Daemon 与中心服务器

tianluo 的核心（`luo run`、`luo sync` ……）是一次性的 CLI：每个命令在前台运行、
随即退出。**daemon** 与**中心服务器**在该核心之上,叠加了一个可选的、常驻的
控制面:

- **`luo daemon`** —— 每台机器上的常驻进程。它发现并监管本机的 `luo run` 流程,
  可代为远端调用者 spawn 新流程,把每个流程在磁盘上的状态聚合为单一快照,并
  (可选地)维持一条到中心服务器的出站连接。
- **`tianluo-server`** —— 独立的中心服务器,接受任意数量 daemon 的连接,把它们的
  快照合并为多机 / 多流程视图,暴露 REST API,并提供自带的网页前端。

两者都完全可选。普通的 `pip install tianluo` 加上 `luo run` 不需要其中任何一个。

## 目录

1. [快速上手](#快速上手)
   - [安装](#安装)
   - [`luo daemon` 命令](#luo-daemon-命令)
   - [`tianluo-server` 命令](#tianluo-server-命令)
   - [一次典型会话](#一次典型会话)
2. [部署与运维](#部署与运维)
   - [出站连接模型](#出站连接模型)
   - [前台模式 vs 后台(detached)模式](#前台模式-vs-后台detached模式)
   - [运行时文件:pidfile 与状态文件](#运行时文件pidfile-与状态文件)
   - [流程的发现与监管](#流程的发现与监管)
   - [systemd 内存护栏](#systemd-内存护栏)
3. [架构与工作原理](#架构与工作原理)
   - [daemon 内部](#daemon-内部)
   - [中心服务器内部](#中心服务器内部)
   - [端到端:从远端机器发布任务](#端到端从远端机器发布任务)
   - [文件上传通道](#文件上传通道)
4. [鉴权与多租户访问](#鉴权与多租户访问)
   - [为什么鉴权是强制的](#为什么鉴权是强制的)
   - [持久化层(`~/.se3/server.db`)](#持久化层se3serverdb)
   - [引导首个管理员(`bootstrap-token`)](#引导首个管理员bootstrap-token)
   - [登录并创建用户](#登录并创建用户)
   - [签发 daemon key 并绑定机器](#签发-daemon-key-并绑定机器)
   - [在 TLS 反向代理后部署(wss)](#在-tls-反向代理后部署wss)
   - [owner 隔离](#owner-隔离)
5. [网页前端](#网页前端)

---

## 快速上手

### 安装

daemon 随核心 `tianluo` 包一起发布 —— 普通安装之后 `luo daemon` 即可开箱即用。
**中心服务器**会引入较重的 web 依赖(FastAPI、uvicorn、websockets),因此这些
依赖被收进一个可选的 `server` extra:

```bash
# 核心安装 —— 已包含 `luo daemon` 命令。
pip install tianluo

# 加装中心服务器(`tianluo-server`)与 daemon 的 WebSocket 客户端。
pip install 'tianluo[server]'
```

daemon 的*出站客户端*在拨入服务器时同样依赖 `server` extra。未带 `--server-url`
启动的 daemon 纯本地运行,完全不触及该 extra;带了 `--server-url` 但未安装该
extra 的 daemon 会记录一条安装提示,并降级为纯本地运行,而不是崩溃。

### `luo daemon` 命令

`luo daemon` 是核心 CLI 的一个子命令组(并非独立的二进制):

```bash
luo daemon start                          # 启动 daemon(脱离终端的后台进程)
luo daemon start --foreground             # 在当前终端中运行 daemon
luo daemon start --server-url ws://host   # 启动并拨入中心服务器
luo daemon stop                           # 停止运行中的 daemon
luo daemon status                         # 显示运行状态与已跟踪的流程
luo daemon status --json                  # 以 JSON 形式输出状态
```

| 子命令 | 选项 | 行为 |
|--------|------|------|
| `start` | `--server-url <url>`、`--daemon-key <key>`、`--foreground` | 启动 daemon。默认以**脱离终端的后台进程**启动;`--foreground` 则改为在当前终端中运行。`--server-url` 记录 daemon 要拨入的中心服务器地址 —— 端口可显式指定(`ws://host:9000`、`wss://host:8443`),未指定时**按 scheme 补全**:`wss://`(及 `https://`)默认补 **443**,`ws://`(及 `http://`)默认补 **8080**(`tianluo-server` 的明文默认值)。因此裸写 `wss://host` 会拨 `:443`,而不是 `:8080` —— 见[端口处理](#出站连接模型)。`--daemon-key` 记录 daemon 在 HELLO 中出示的密钥,使多租户服务器把机器绑定到某个 owner。若已有 daemon 在运行,命令会报告并以非零码退出。 |
| `stop` | —— | 停止运行中的 daemon(发送 `SIGTERM` 并等待其退出)。若没有 daemon 在运行,报告 `not running` 并以 `0` 退出;若进程在宽限期内未退出,报告停止超时并以非零码退出。 |
| `status` | `--json`、`-j` | 报告 daemon 是否在运行、其 pid、机器 id、配置的服务器地址、**真实的出站连接状态**(见下文),以及已跟踪流程的列表。`--json` 以 JSON 形式输出同样的信息,而非渲染的面板。 |

### `tianluo-server` 命令

中心服务器通过它自己的 `console_scripts` 入口 `tianluo-server` 启动(由
`tianluo[server]` extra 安装):

```bash
tianluo-server                                # 绑定到 127.0.0.1:8080(默认值)
tianluo-server --host 0.0.0.0 --port 9000     # 监听所有网卡,端口 9000
tianluo-server --db-path /var/lib/tianluo.db      # 覆盖 sqlite 存储位置
tianluo-server --log-level debug              # 提高 uvicorn 日志级别
tianluo-server bootstrap-token                # 铸发一次性的 break-glass admin token
```

| 选项 / 子命令 | 默认值 | 说明 |
|---------------|--------|------|
| `--host` | `127.0.0.1` | 服务器绑定主机。用 `0.0.0.0` 可接受来自其他机器的连接。 |
| `--port` | `8080` | 绑定端口。 |
| `--db-path <path>` | `~/.se3/server.db` | 承载身份 / 鉴权层的嵌入式 sqlite 存储路径。对单次启动覆盖配置中的 `server.db_path`。 |
| `--log-level` | `info` | uvicorn 日志级别。 |
| `bootstrap-token` | —— | 铸发一次性的 **break-glass admin token**,把明文向控制台打印且仅打印一次,只存其 hash。这是进入一台全新服务器的首个入口 —— 见 [鉴权与多租户访问](#鉴权与多租户访问)。可重复铸发。 |

`tianluo-server` 在前台运行并阻塞。若需常驻部署,请把它放在进程管理器(systemd、
supervisor、容器 ……)下运行。

> **服务器要求鉴权。** 自 8.0.0 起,每个 web/REST 请求与每条 daemon 连接都必须
> 解析到一个 *owner* —— 不存在匿名模式。在服务器可用之前,你必须铸发一个
> break-glass admin token 并登录;见 [鉴权与多租户访问](#鉴权与多租户访问)。

### 一次典型会话

```bash
# 在托管控制面的机器上 —— 启动服务器。
pip install 'tianluo[server]'
tianluo-server --host 0.0.0.0 --port 8080

# 铸发一个一次性的 break-glass admin token,然后在浏览器中打开服务器、用它登录,
# 并为你的工作机签发一把 daemon key(见下文「鉴权与多租户访问」)。
tianluo-server bootstrap-token

# 在一台工作机上 —— 启动一个拨入你的中心服务器的 daemon,携带 daemon key
# 以便把该机器绑定到你这个 owner。
pip install 'tianluo[server]'
luo daemon start --server-url ws://control.example.com:8080 --daemon-key <key>
luo daemon status

# 然后在浏览器中打开 http://control.example.com:8080、登录,即可观察你已连接的
# 机器及其流程,并发布新任务。

# 结束时:
luo daemon stop
```

---

## 部署与运维

### 出站连接模型

daemon 与服务器之间只朝一个方向连接:**由 daemon 拨出到服务器**,经一条
WebSocket。服务器永远不会反向主动连接 daemon。

```
  机器 A                             机器 B
  ┌─────────────┐                    ┌─────────────┐
  │ luo daemon  │──┐              ┌──│ luo daemon  │
  └─────────────┘  │  出站         │  └─────────────┘
                   │  WebSocket   │
                   ▼   /ws        ▼
              ┌────────────────────────┐
              │       tianluo-server       │
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

**端口处理。** `--server-url` 的取值可携带显式端口(`ws://host:9000`、
`wss://host:8443`),显式端口一律原样保留。当端口被省略时,daemon 会用一个
**随 scheme 而定的默认值**补全 URL,而不是让 WebSocket scheme 回退到它隐式的
端口(`ws` 为 80,`wss` 为 443):

| Scheme(归一 `http→ws`、`https→wss` 之后) | 补全的默认端口 |
|--------------------------------------------|----------------|
| `ws://`(及 `http://`) | **8080** —— `tianluo-server` 的明文默认端口 |
| `wss://`(及 `https://`) | **443** —— TLS 反向代理监听的标准 HTTPS 端口 |

这之所以重要,是因为 `wss://` 的 daemon 几乎总是在反向代理的 **443** 端口终结
TLS,而不是 tianluo-server 的明文 **8080**。在引入此规则之前,裸写的 `wss://host`
会被错误补全为 `wss://host:8080`,于是 daemon 把 TLS 拨到了错误的端口、永远连
不上(`luo daemon status` 显示 `not connected`)。现在 `wss://host` 开箱即拨
`:443`,而 `ws://host` 仍与不带 `--port` 启动的服务器一致。明文 / TLS 两个默认
端口都定义在同一个共享模块(`tianluo.daemon.protocol`)里,使两端不会漂移。需要非标准
端口?显式写出即可 —— `wss://host:8443` 会被原样保留。

**查看真实的连接状态。** 连上服务器是尽力而为的:若缺少 `tianluo[server]` extra 或
拨号失败,daemon 会记录原因并降级为纯本地运行,而不是崩溃。由于 `--server-url`
只是*记录*一个 URL,它并不能证明 daemon 真的连上了 —— 请始终用
`luo daemon status` 确认(见下文
[运行时文件](#运行时文件pidfile-与状态文件)),它会报告真实的出站连接状态。

### 前台模式 vs 后台(detached)模式

`luo daemon start` 支持两种模式:

- **后台(默认)。** daemon 通过双重 fork 完全脱离终端:其父进程变为 `init`,
  因此它不会成为启动它的 shell 的僵尸进程,也能在终端关闭后继续存活。
  `luo daemon start` 在 daemon 占用其 pidfile 之后立即返回。标准流被重定向到
  一个日志文件(见下文)。
- **前台(`--foreground`)。** daemon 在当前终端中运行,`luo daemon start` 会
  阻塞直到它停止。适用于调试、在期望非脱离进程的进程管理器(systemd、Docker
  ……)下运行,以及实时查看 daemon 日志。

两种模式下,每个 pid 目录都只能运行一个 daemon —— 第二次 `luo daemon start`
会检测到存活的 pidfile 并以非零码退出。

### 运行时文件:pidfile 与状态文件

daemon 默认把运行时文件放在 `~/.se3/`。该目录可用 `SE3_DAEMON_DIR` 环境变量
覆盖(便于测试,或并排运行相互隔离的多个 daemon):

| 文件 | 用途 |
|------|------|
| `~/.se3/daemon.pid` | pidfile。保存 daemon 的 pid、启动时间、服务器地址与机器 id。用于防止重复启动,也是 `stop` / `status` 的事实来源。干净关停时被删除。 |
| `~/.se3/daemon_status.json` | 最新的聚合状态快照,每次轮询时被改写。`luo daemon status` 读取它来列出已跟踪流程,无需联系 daemon 进程本身。它还携带**真实的出站连接状态** —— 见下文。干净关停时被删除。 |
| `~/.se3/daemon.log` | 脱离终端(后台)运行的 daemon 的日志输出 —— 其 stdout 与 stderr 被重定向到这里。每一行都带时间戳,因此可分辨日志出自哪一次 daemon 启动。 |

pidfile 与状态文件都以原子方式写入(临时文件 + rename),因此写到一半崩溃也不会
损坏它们。

#### `status` 中的连接状态

`daemon_status.json` 记录的是 daemon **真实的**出站连接结果,而不仅是配置的
URL;`luo daemon status` 会把它呈现在专门的 `Connection:` 行上:

- `Connection: local-only (no server configured)` —— 启动时未带 `--server-url`。
- `Connection: connected` —— 到服务器的出站 WebSocket 已建立。
- `Connection: not connected (<原因>)` —— 给了 `--server-url` 但 daemon 未连接;
  会原样显示**真实、可读的原因**,因此你无需翻日志即可诊断失败。该原因在每条失败
  路径上都会被写入 —— 缺依赖(`websockets not installed`,即缺 `tianluo[server]`
  extra)、握手失败、连接被拒 / 超时(`TimeoutError`)、TLS / 端口错配,或对 daemon
  key 的 `WELCOME(accepted=false)` 拒绝 —— 且绝不会塌缩成空的 `()`(消息为空的纯
  超时会回退到异常类型名)。正是这种情形下,即便 `luo daemon start` 报告了成功,
  该机器仍**不会**出现在服务器的机器列表中。万一原因实在不可得,该行会指引你去看
  `~/.se3/daemon.log`,而不是重复一句没有信息量的字面量。

因此,配置了 `Server:` URL 却同时出现 `Connection: not connected` 行,就是静默
降级的特征 —— 修复办法通常是 `pip install 'tianluo[server]'`,或更正 URL / 端口。

### 流程的发现与监管

daemon 在本机上跟踪两类 `luo run` 流程:

- **spawn 出来的流程** —— daemon 自己启动的流程(通常是为响应远端的任务发布
  请求)。daemon 是这些流程的父进程。
- **发现到的流程** —— 由用户在同一台机器上独立启动的 `luo run` 进程。daemon
  通过扫描进程命令行(尽力而为,经 `psutil`)找到它们,并纳入自己的跟踪表。

对每个被跟踪的流程,daemon 从该项目的 `tianluo/state/engine.json` 解析出 `flow_id`。
它以固定间隔(默认每 2 秒)轮询存活状态,清除已退出进程的记录,并在关停时优雅
终止它自己 spawn 的所有流程(先 `SIGTERM`,宽限期后再 `SIGKILL`)。发现到的
流程*不会*被杀死:daemon 只监管它自己拥有的进程的生命周期。

---

### systemd 内存护栏

服务器把每个被查看的 flow 的完整对话保存在内存中,而
`server.history_cache.budget_mb`(见
[docs/configuration.zh.md](configuration.zh.md#serverhistory_cache))就是这份内存
在代码层面的上限。**先配它**——真正给增长封顶的是它。

下面这些 unit 级设置是**兜底**,不是修复本身:它们给进程最终用掉的内存封一道硬顶,
并让一次被杀之后能自动恢复,从而把『预算配小了 / 内存 bug / 与本功能无关的分配』
降级成一次重启,而不是一个没人发现就一直躺着的控制面。

```ini
# /etc/systemd/system/tianluo-server.service
[Unit]
Description=tianluo central control-plane server
After=network-online.target
Wants=network-online.target
# 别让崩溃循环把宿主机拖死:超出下面的阈值 systemd 就放弃重启,unit 进入 failed
# 状态——那正是监控应该告警的信号。这两个键属于 [Unit] 段(见 systemd.unit(5);
# systemd.service(5) 只是交叉引用它们),写在 [Service] 里会被 systemd 忽略。
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=tianluo
ExecStart=/opt/tianluo/venv/bin/tianluo-server --host 127.0.0.1 --port 8080

# 兜底 1 —— 总能爬起来。没有它的话,一次 OOM kill(或任何未处理的崩溃)会让所有
# daemon 掉线、控制台一片黑,直到有人手动介入。给它封顶的崩溃循环限速在上面的
# [Unit] 段里。
Restart=on-failure
RestartSec=5s

# 兜底 2 —— 给进程封顶。MemoryHigh 是软膝点:超过它 cgroup 会被节流并回收,通常
# 足以给 history 缓存的淘汰争取到时间。MemoryMax 是硬墙:由该 cgroup 自己的 OOM
# killer 干掉服务器,而不是让内核在整台宿主机上另选一个受害者。MemoryHigh 要显著
# 高于 server.history_cache.budget_mb(预算只覆盖缓存的 bundle,不含解释器、
# sqlite 存储与请求处理中的临时缓冲)。
MemoryAccounting=yes
MemoryHigh=768M
MemoryMax=1G

[Install]
WantedBy=multi-user.target
```

事后归因靠的是缓存占用报告——服务器会周期性地、以及在占用越过报告阈值时,输出总
占用、预算、淘汰计数,以及按 `flow_id` 列出的占用最大的几个 flow:

```bash
journalctl -u tianluo-server | grep history-cache
```

daemon 的 unit 同样建议加 `Restart=on-failure`;daemon 轻得多(它只是流式推送
history,并不缓存),内存限制在那边是可选的。

---

## 架构与工作原理

### daemon 内部

daemon 是一个单一的、长寿命的 `asyncio` 进程,由四个部件组成:

- **Supervisor(监管器)** —— 发现并跟踪本机的 `luo run` 进程(spawn 出来的 +
  发现到的),轮询存活状态并回收已退出的流程。
- **Spawner(spawn 器)** —— 以 `luo run <task> --type <type>
  --output-format json` 子进程的形式启动新流程。daemon 是每个流程的*父进程*,
  绝不在进程内调用,因此 daemon 崩溃绝不会连带拖垮某个流程。子进程的
  stdout/stderr 被重定向到 `<project_root>/tianluo/logs/daemon/` 下的 per-flow 日志
  文件(子进程发出结构化的 NDJSON 事件流,daemon 之后可对其做 tail)。
- **Aggregator(聚合器)** —— 一个纯粹的磁盘文件读取者。它轮询每个被跟踪项目的
  `tianluo/state/`(引擎状态 + 摘要)、`tianluo/calls/`(人工调用队列)、`tianluo/logs/` 与
  `tianluo/issues/`,把它们折叠成单一的 `MachineStatus` 快照:每个流程的进度、当前
  step、待处理的调用、日志与 issue 计数。它从不伸手进流程的进程内部。
- **Client(客户端)** —— 可选的、到中心服务器的出站 WebSocket 客户端。它推送
  `MachineStatus` 快照、应答心跳,并把入站指令(`SPAWN_FLOW` → spawner、
  `RESPOND_CALL` → 一个 `tianluo/calls/` 响应文件)路由回本机。

监管器、聚合器轮询循环与出站客户端都作为并发任务跑在 daemon 的单一事件循环上,
并共享同一个优雅停止信号。

### 中心服务器内部

`tianluo-server` 是一个 FastAPI 应用,它:

- 在 `/ws` 上接受 daemon 的 WebSocket 连接,校验每个 daemon 的开场握手,并维护
  一个带心跳的 `machine_id → 连接` 连接池;
- 维护一份内存中的**多机 / 多流程**聚合视图 —— `ServerState` —— 由 daemon 推送
  的 `MachineStatus` 快照构建。这份机器 / 流程 / 历史的*实时*态刻意只保存在内存中
  并随 daemon 重连而重建,因此从不写入持久化层;
- 在一个**嵌入式单文件 sqlite 存储**(`~/.se3/server.db`,标准库 `sqlite3` ——
  不引入额外依赖)中,只持久化那些 daemon 重连*无法*重建的身份事实:owner 记录、
  `(provider, external_id)` 身份绑定、本地口令 hash、已签发的 daemon-key hash,
  以及 break-glass token hash(见 [鉴权与多租户访问](#鉴权与多租户访问));
- 暴露一组 REST API 用于查询该视图并对其执行操作:`GET /api/machines`、
  `GET /api/machines/{id}/flows`、`GET /api/flows/{id}`、`POST /api/flows`
  (发布新任务)、`POST /api/flows/{id}/respond`(应答某个流程待处理的
  介入/调用),外加 `GET /api/health`。每个 `/api/*` 数据路由都会解析并按调用方
  owner 过滤;
- 提供自带的网页前端,以及位于 `/ws/ui` 的前端 WebSocket。

daemon↔服务器的线上协议有单一事实来源 —— `tianluo.daemon.protocol` 模块 —— 由两端
共同 import,因此 schema 不会漂移。

### 端到端:从远端机器发布任务

当你从网页前端(或直接经 `POST /api/flows`)发布一个任务时,完整链路是:

1. 浏览器 / API 客户端把任务、目标 `machine_id` 与任务类型发给**服务器**。
2. 服务器查出该机器存活的 daemon 连接,把一条 `SPAWN_FLOW` 指令*沿着*既有的
   出站 WebSocket 下发。
3. 目标 **daemon** 收到 `SPAWN_FLOW`,让它的 spawner 在请求指定的项目中启动一个
   真实的 `luo run --output-format json` 子进程。
4. 这个新流程的运行方式与任何本地 `luo run` 完全一样。它在磁盘上的状态会在
   daemon 的**聚合器**下一次轮询时被拾取。
5. daemon 向服务器推送一份更新后的 `MachineStatus` 快照,服务器将其合并进
   `ServerState`,并向每个已连接的网页前端广播该变更。

应答某个流程待处理的介入/调用走的是镜像路径:
`POST /api/flows/{id}/respond` → 服务器 → `RESPOND_CALL` 沿 socket 下发 →
daemon 向该项目的 `tianluo/calls/` 队列写入一个响应文件,从而解除被暂停流程的阻塞。

### 文件上传通道

网页前端允许你把任意文件 —— 粘贴的截图、日志、小压缩包 —— 附到一段 prompt 上:
新任务输入框、运行中流程的回复框、以及插话框三处都支持。文件**不会**作为附件被送给
agent;它会被落盘到该流程所属的那台机器上,prompt 中携带的是它的**项目相对路径**。
agent 随后以项目根为工作目录按该路径读取它,与读取仓库里任何其他文件别无二致。

这条链路与其他所有指令共享同一套「只出站」拓扑:

1. 浏览器把原始字节 `POST` 到 `POST /api/uploads`,走的是**既有的已认证会话**
   (未认证的请求会被拒绝 —— 不存在匿名上传),并指明目标 `flow_id`,或显式的
   `machine_id` + `project_root`。
2. 服务器把目标解析到一台可证明归调用方 owner 所有的机器,随后把一条
   `UPLOAD_COMMAND` 帧*沿着*该机器既有的出站 WebSocket 下发,并等待匹配的
   `UPLOAD_RESULT`。
3. 目标 **daemon** 解码载荷,把它写入该项目运行时目录下的
   `<project_root>/tianluo/uploads/`。
4. daemon 回传项目相对路径,服务器将其返回浏览器,网页前端再用它就地替换掉此前插入
   在光标处的「上传中…」占位。

在依赖这条通道之前,有几条性质值得了解:

- **单文件上限 20 MiB**,并在三层各自独立校验 —— 浏览器端预检,使超限的粘贴根本不
  离开页面;服务器端复检请求体,因为浏览器不可信;daemon 端复检解码后的载荷,因为
  服务器同样不可信、而 daemon 的磁盘才是真正被保护的资源。三层遵循的是
  `tianluo.daemon.protocol` 中的同一个常量。
- **命名与去重。** 落盘文件名为 `<sha256 前 12 位>_<原文件名>`,其中原文件名经过
  sanitize(路径分隔符与控制字符被替换,因此任何文件名都无法指向 `uploads/` 之外的
  目录)。hash 前缀使两个都叫 `screenshot.png` 的不同文件可以共存而不互相覆盖;重复
  上传**内容相同**的文件会直接复用磁盘上已有的那一份,不再落盘。写入经临时文件 +
  原子改名完成,因此 agent 绝不会读到只写了一半的附件。
- **目标项目必须已在该机器的 daemon 上注册。** daemon 拒绝写入它并未跟踪的目录,因此
  即便服务器被攻陷或有 bug,也无法借这条通道往工作机的任意位置投放文件。
- **daemon 的协议版本必须 ≥ 5**(下文的读回方向要求 ≥ 6)。服务器在派发*之前*就检查已连接 daemon 上报的版本,
  对过旧的 daemon 直接返回一个明确的「daemon 版本过旧」错误,而不是让请求一直挂到
  超时 —— 上传发生在用户打字的关键路径上,静默停顿与卡死无法区分。升级该工作机上的
  `tianluo` 安装即可启用上传。
- **上传文件不进版本库。** `luo init` 会确保项目的 `.gitignore` 覆盖上传目录(仍是
  旧运行时布局的项目对应 `se3/uploads/`),因为它们是体积不可控、且可能携带任何被拖进
  prompt 的内容的运行时产物。
- **不做任何自动清理。** 既没有保留策略、没有 TTL,也没有对该目录整体的容量上限:
  只要还有人附加文件,`tianluo/uploads/` 就会持续增长,清理它是运维人员的工作。在网页
  前端的附件条上删除某一项,只是把该路径文本从 prompt 中移除 —— 项目机器上的文件仍
  原样保留。

#### 把附件读回来

上传的文件落在 *daemon* 那台机器的磁盘上,因此渲染会话的浏览器无法直接打开它:通往那台
机器的唯一通道就是 daemon 自己的出站 socket。**协议 revision 6** 补上了读回方向 ——
上传链路的镜像 —— 使网页前端能把附上的截图内联显示在会话里,而不是只留一串路径文本:

1. `GET /api/uploads/file?path=<项目相对路径>`,走的是同一个已认证会话,目标的指明方式
   与上传完全相同(`flow_id`,或 `machine_id` + `project_root`)。鉴权与 owner 校验也
   完全一致 —— 你只能读取归自己所有的机器上的附件。
2. 服务器沿该机器的 socket 下发一条 `FETCH_COMMAND` 帧,并等待(10 秒)匹配的
   `FETCH_RESULT`。
3. daemon 读出文件、以 base64 回传字节;服务器解码后把原始字节放进 HTTP 响应体返回。

让这条通道可以安全对外暴露的几条规则:

- **containment 判定基于*已解析*的路径。** 只有当请求路径解析后是该项目 uploads 目录的
  **直接子文件**时,daemon 才接受这次读取。这一条检查同时覆盖 `..` 路径段、绝对路径,以及
  读回方向独有的一种情形 —— 有人在 `uploads/` 里放了一个指向工作机上其他文件的符号链接。
  其余一律以 `invalid_path` 失败关闭;合法附件永远不会位于该目录之外。目标项目同样必须
  已在该 daemon 上注册,与上传方向一致。
- **同样适用 20 MiB 上限**,且在读取任何一个字节之前就由 `stat()` 判定,因此超限文件不会
  让工作机付出任何内存代价。
- **revision 6 在派发前就被门禁拦住。** 服务器检查 daemon 上报的协议版本,对过旧的
  daemon 直接返回 `501`,而不是发出一条老 daemon 只会静默丢弃的帧。这一点在这里比上传
  方向更要紧:一段会话里可能有很多张内联图片,没有这道门禁的话,每一张都会把一条浏览器
  连接占满整个超时窗口。
- **响应带长效缓存,但只落在发起请求的浏览器里**:`Cache-Control: private,
  max-age=31536000, immutable`,并带 `Vary: Cookie`。长生存期之所以成立,正是因为上文的
  content-hash 命名 —— 一个项目相对 uploads 路径只可能对应唯一一份字节,陈旧缓存在构造上
  就不可能出现。若没有它,回看历史会话时每张缩略图、每次重绘都会穿透到 daemon 打一个
  来回。这里刻意用 `private` 而非 `public`:该路由是 owner 作用域的,`public` 会允许
  server 前面的缓存型反向代理存下某个租户的附件,并对同一 URL 的未鉴权请求直接回放,
  完全绕过 owner 校验。
- **`Content-Type` 取自一份很小的白名单**(仅光栅图片类型)。白名单之外的一律以
  `application/octet-stream` 加 `X-Content-Type-Options: nosniff` 返回,因此上传的
  `.html` —— 以及被刻意排除在外的 `.svg`(它是可携带脚本的文档,而非光栅图片)—— 都不会
  在服务器自身的同源下被当作文档渲染。

**降级是刻意做成静默的。** 这条链路的每一种失败 —— daemon 离线(`503`)、版本过旧
(`501`)、响应过慢(`504`)、文件已被清理(`404`)、路径未通过 containment(`422`)——
在浏览器侧都表现为一次图片加载失败,网页前端会直接把那张缩略图隐藏掉。消息仍保留它一直
显示的那串路径文本 —— 那才是 agent 真正读到的字符串 —— 不会向阅读者抛出任何错误。内联
缩略图始终是对会话文本的**追加**,而非替换。

---

## 鉴权与多租户访问

自 8.0.0 起,中心服务器是一个**多租户控制面**:每个 web/REST 请求与每条 daemon
连接都必须解析到一个 *owner*,所有可见范围与控制权都按该 owner 过滤。早先那种
身份无关的「裸」模式 —— 任何能访问到服务器的人都能列出全部机器、并经
`POST /api/flows` 在任意 daemon 上派发 `luo run` —— 已被移除。

本节按端到端的搭建动线讲解:

```
tianluo-server bootstrap-token   →   登录(break-glass)   →   建本地用户
        →   每个 owner 签发 daemon key   →   luo daemon start --daemon-key
        →   机器与 flow 按 owner 隔离
```

### 为什么鉴权是强制的

服务器**fail-closed**(无可用 provider 即拒绝服务)。鉴权 provider 集合由
`tianluo.yaml` 的 `server.auth.providers` 配置,默认为 `["local"]`(内置的
用户名 + 口令 provider)。可识别的名称为 `local`、`oidc`、`proxy_header`;其中
`oidc` 与 `proxy_header` 是默认关闭、v1 不要求的接缝。若解析后的 provider 链
最终**没有任何可用 provider**(例如 `local` 被显式禁用且无其他 provider 启用),
服务器会在启动时抛出 `AuthNotConfigured` 并**拒绝服务**,而不是退回匿名访问。
同理,一个解析不到 owner 的 `/api/*` 请求会以 **HTTP 401** 拒绝。

### 持久化层(`~/.se3/server.db`)

服务器唯一的持久化是一个**嵌入式单文件 sqlite 存储**(标准库 `sqlite3`,不引入
额外依赖),默认位于 `~/.se3/server.db`。其路径来自 `tianluo.yaml` 的
`server.db_path`,并可用 `tianluo-server --db-path <path>` 对单次启动覆盖(显式的
`--db-path` 优先)。它**只存储那些 daemon 重连无法重建的身份事实**:

- owner 记录(以不透明、稳定的内部 `owner_id` 为主键);
- `(provider, external_id) → owner_id` 身份绑定(一个 owner 可携带多条绑定;
  一个外部身份映射到唯一一个 owner);
- 本地口令 hash(优先 argon2id,回退 bcrypt —— 绝不明文或快速 hash);
- 已签发的 **daemon-key hash**;
- 一次性的 **break-glass token hash**。

机器 / 流程 / 历史的实时态*不*存于此 —— 它们保留在内存中并随 daemon 重连重建。

### 引导首个管理员(`bootstrap-token`)

全新服务器没有任何账号,因此存在一个与 IdP 无关的唯一入口:

```bash
tianluo-server bootstrap-token
```

它会铸发一个**一次性的 break-glass admin token**,把明文向服务器控制台打印且
**仅打印一次**,只持久化其 SHA-256 hash(永不写入日志)。break-glass 是单一的
管理员主体,服务于两件正交的事:首个管理员的引导,以及当所配置的 provider 不可达
时的 fail-closed 兜底入口。该命令**可重复运行** —— 每次铸发一个新 token,先前的
token 在被消费或清除前仍然有效。该子命令依赖极轻,即便在仅核心安装(未装
`[server]` extra)上也可运行。

你可在网页登录界面消费该 token,或直接:

```
POST /api/auth/breakglass     # 消费一次性 token → break-glass admin owner
```

### 登录并创建用户

人 / UI 侧的鉴权流经 provider 链(daemon 从不走这一层)。本地 provider 的登录
仪式用用户名 + 口令换取一个服务端 session cookie:

```
POST /api/auth/login          # 用户名 + 口令 → session cookie
POST /api/auth/logout         # 结束 session
GET  /api/auth/me             # 当前的 OwnerIdentity
```

以管理员身份(break-glass admin,或任一 admin owner)登录后,你可创建 / 邀请
更多本地用户:

```
POST /api/users               # 仅限 admin:创建 / 邀请一个本地用户
```

**v1 不开放公开自助注册** —— 账号由首次引导的 bootstrap admin 加上管理员提供
(邀请或创建)的用户构成。多个不同 owner 由本地 provider 区分,绝不靠为每个用户
铸一个 break-glass token。

### 签发 daemon key 并绑定机器

登录之后,owner 自助管理自己的 **daemon key** —— 这是 daemon 出示、使服务器能把
上报机器绑定到该 owner 的凭据:

```
POST   /api/daemon-keys           # 铸发一把绑定到当前 owner 的 key(明文仅返回一次)
GET    /api/daemon-keys           # 列出该 owner 的 key 元数据(hash,绝不返回明文)
DELETE /api/daemon-keys/{key_id}  # 吊销一把 key
```

明文 key 仅在铸发时**返回一次**,只持久化其 hash。随后你用该 key 启动一个 daemon,
让它的机器加入该 owner 的信任域:

```bash
luo daemon start --daemon-key <key> --server-url wss://control.example.com
# 或者等价地:
SE3_DAEMON_KEY=<key> luo daemon start --server-url wss://control.example.com
```

daemon 在它的 HELLO 握手中携带该 key;服务器解析 `key → owner_id`,绑定机器
(`MachineRecord.owner_id`),并回 `WELCOME(accepted=true)`。**缺失或无效**的 key
会被以 `WELCOME(accepted=false)`(附一个不含 key 的原因)拒绝并关闭 socket,不进入
接收循环 —— daemon 会记录该拒绝,并停止在紧密的重连循环里重放被拒的 key。该 key
只存在于内存与 HELLO 报文中,绝不写入 daemon 状态文件或任何日志。无 key 的 daemon
(未带 `--daemon-key`)与本地 / 旧版单租户运行保持兼容,只是不绑定到任何 owner。

daemon→服务器的反向信任由 **TLS** 承载:daemon 拨入一个已知的 `wss://` 地址,
其服务器身份由证书背书(服务器自身不终结 TLS —— 由反向代理终结)。应用层不在其上
另建一套服务器鉴权机制。

### 在 TLS 反向代理后部署(wss)

`tianluo-server` 只讲明文 HTTP/WebSocket,自身**不**终结 TLS。要做公网 `wss://`
部署,你需要在它前面放一个反向代理(nginx、Caddy ……)来终结 TLS,并转发到
`tianluo-server` 的明文端口(默认 `8080`)。代理把两类很不一样的流量转发到**同一个**
后端:

- **静态网页请求** —— 自带前端的普通 HTTP `GET`/`POST`(`/`、`/app.js`、
  `/api/*`)。这些无需特殊处理。
- **WebSocket 长连接** —— daemon 拨 `/ws`,浏览器前端拨 `/ws/ui`。它们以一个携带
  `Upgrade: websocket` 头的 HTTP `GET` 开始,必须被**升级为一条持久连接**;代理
  得把 `Upgrade`/`Connection` 头透传过去,并保持连接不关。

**单个 `location /`** 即可兜住二者 —— 你无需为每个端点各写一段。诀窍是无条件透传
升级头:普通请求本身就不带 `Upgrade` 头、按纯 HTTP 转发即可,而 `/ws` 或 `/ws/ui`
请求带着它、于是被升级。

#### nginx

WebSocket 升级需要 HTTP/1.1,并把 `Upgrade`/`Connection` 头原样透传。nginx 的
惯用写法是用一个 `map` 从请求自身的 `Upgrade` 头推导出 `Connection` 头的值:

```nginx
# 必须写在 http{} 层(例如 conf.d 或主 nginx.conf 内),而**不是** server{} 里
# —— `map` 只在 http 上下文里合法。宝塔(BT)/ openresty 等面板若把你的片段塞进
# server 块,会报 "map directive is not allowed here"。
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl;
    server_name tianluo.example.com;

    ssl_certificate     /etc/ssl/tianluo.example.com.crt;
    ssl_certificate_key /etc/ssl/tianluo.example.com.key;

    # 一个 location 同时兜住静态前端与 /ws、/ws/ui 长连接。^~ 胜过面板可能注入的
    # 正则 location。
    location ^~ / {
        proxy_pass http://127.0.0.1:8080;

        # WebSocket 升级 —— 握手只能走 HTTP/1.1。
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        # 把原始 host / 客户端信息传给后端。
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # /ws 连接在两次状态快照之间大多空闲;默认 60s 读超时会把它拆掉。
        # 把它调得宽松些。
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

注意 / 常见坑:

- **`map` 必须在 `http{}` 层。** 它不能写进 `server{}` 里。宝塔(BT)/ openresty
  等面板的「反向代理」框会把你的片段塞进 `server` 块,在那里定义 `map` 会让 nginx
  启动失败 —— 把 `map` 放到面板的主配置 / 一个 `http` 层的 include 里,再从 server
  块引用 `$connection_upgrade`。
- **`proxy_http_version 1.1` 不可省。** nginx 默认对上游用 HTTP/1.0,无法升级;
  不写它,握手永远拿不到 `101`。
- **调大 `proxy_read_timeout`。** daemon 的 `/ws` 连接在两次状态推送之间空闲;
  默认 60s 超时会静默把它拆掉,你会看到 daemon 反复重连抖动。

#### Caddy

Caddy 自动终结 TLS(Let's Encrypt),并无需任何额外指令即可代理 WebSocket ——
`reverse_proxy` 透明处理升级:

```caddy
tianluo.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

若你想要和上面 nginx 范例一样宽松的空闲超时:

```caddy
tianluo.example.com {
    reverse_proxy 127.0.0.1:8080 {
        transport http {
            read_timeout 3600s
        }
    }
}
```

#### 验证代理:`curl --http1.1` 握手探测

要确认代理确实把一次 WebSocket 升级穿透到了后端,用 `curl` 发一个原始握手,
看是否返回 **`HTTP/1.1 101 Switching Protocols`**:

```bash
curl -i --http1.1 \
  -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: $(head -c16 /dev/urandom | base64)" \
  https://tianluo.example.com/ws
```

期望的响应(升级端到端成功):

```
HTTP/1.1 101 Switching Protocols
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Accept: <你的 key 的 base64 摘要>
```

读结果时的避坑点:

- **对 `/ws` 发普通 `GET` 返回 FastAPI 的 `404 {"detail":"Not Found"}` 是预期的
  —— 而且是*好消息*。** 它证明请求穿过了代理、抵达了 tianluo-server 后端;`/ws` 只是
  拒绝一个非升级的 GET。如果你看到的是代理自己的 404/502 页面,说明请求根本没到
  后端。
- **WebSocket 握手只能走 HTTP/1.1。** 在 HTTP/2 上你*永远*拿不到 `101` —— 去掉
  `--http1.1`,一个用 HTTP/2 打头的代理会返回普通响应,而不是升级。这正是 nginx
  范例要钉死 `proxy_http_version 1.1` 的原因。
- **默认端口随 scheme 走。** 不带端口的 `wss://host` 会被补全为 **443**(TLS 反向
  代理的 HTTPS 端口),`ws://host` 则补 **8080**(见
  [端口处理](#出站连接模型))。因此裸写的 `wss://tianluo.example.com` 会自动拨
  `:443` —— 只有当你的代理监听在别处时才需要显式加 `:port`。

### owner 隔离

两条通道都按 owner 限定范围:

- **前端 `/ws/ui` 与所有 `/api/*` REST 路由**经 provider 链解析出 owner,并按
  owner 过滤可见范围与控制权。一个 owner 只能看到自己的机器 / 流程 / 历史,且
  **只能**对自己的 daemon 执行 `POST /api/flows`、`respond`、`interject`。跨
  owner 的目标读作**未找到(404)**,而不是被派发。
- **daemon `/ws`** 把 HELLO 中的 key 解析为 owner 并绑定机器,如上所述。

日后增加第二个鉴权 provider 是纯叠加式的:经一道信任门(trust gate)把一条新的
`(provider, external_id)` 绑定挂到既有 `owner_id` 上,于是 `owner_id`、daemon→owner
绑定、以及已签发的 daemon key 全部保持不变 —— daemon 无需重新登记。

---

## 网页前端

`tianluo-server` 自带一个小巧的、纯静态的网页前端(`index.html`、`style.css`、
`app.js` —— 无构建步骤)。它被挂载在服务器根路径上,因此只要 `tianluo-server` 在
运行,你在浏览器中打开服务器地址即可:

```
http://<server-host>:<port>/        # 例如 http://127.0.0.1:8080/
```

**你必须先登录。** 该控制面是多租户的(见
[鉴权与多租户访问](#鉴权与多租户访问)),因此前端会先呈现一个登录界面 —— 用本地
用户名 + 口令登录,或在全新服务器上消费一次性的 break-glass token —— 然后才会显示
任何机器或流程。此后的一切都被限定在**你**这个 owner 的范围内:你只会看到并操作
自己的机器、流程与历史;跨 owner 的目标读作未找到。

页面经按 owner 限定的 `/ws/ui` WebSocket 连回服务器。服务器把该 owner 的机器列表
沿该 socket 下推:连上时先发一份初始 `snapshot`,此后每当*你的*任一 daemon 的状态
变化就发一次 `status_update` —— 因此视图无需轮询即可实时更新。

在前端中你可以:

- **查看流程进度。** 左侧面板列出每台已连接的机器;选中一台即可看到它的流程,
  每个流程都带有当前 step、进度,以及日志 / issue 计数。打开某个流程可看其详情
  抽屉。
- **远程发布任务。** **+ New Task** 按钮会打开一个对话框,用于挑选目标机器与
  任务类型并填写任务描述。提交后即触发上文描述的端到端发布链路 —— 任务在选定的
  远端机器上运行。
- **响应介入/调用。** 当某个流程暂停等待人工输入时,它会在 UI 中浮现一条待处理
  的调用。响应对话框让你键入答复并发送;服务器把它作为 `RESPOND_CALL` 路由给
  拥有该流程的 daemon,daemon 将响应写入该项目的 `tianluo/calls/` 队列并恢复流程。

前端在 WebSocket 上是只读的(它只*监听*状态推送);New Task 与响应操作走的是
服务器的 REST API。
