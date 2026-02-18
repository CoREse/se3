<!-- Generated on 2026-02-19 -->
<!-- SE3 Version: 2.12.19 -->
<!-- Checksum: 42882fbead24daeb410910cc3d0af8bd69ec880c54f694ebb2296ee5cd7b6c34 -->

<!--
  SE 3.0 Framework Reference File
  ===============================
  This file is installed by `se3 init` and serves as the official framework specification.

  Generated File: DO NOT MODIFY DIRECTLY
  Version: {{SE3_VERSION}}
  Checksum: {{CHECKSUM}}
-->

# {{PROJECT_NAME}}

> **Note**: SE3 2.x+ 采用"手动触发"模式。不再指望 agent 自觉遵循，而是通过 command 调用。

## SE3 Command 入口

| Command | 用途 |
|---------|------|
| `/se3:start` | 开始会话 |
| `/se3:work <描述>` | 开始/继续工作 |
| `/se3:done` | 结束会话 |

所有工作流通过 CLI (`se3 start`, `se3 work`, `se3 done`) 程序化驱动，返回 JSON actions 数组。

## Git Commit

使用 `se3 commit` 代替 `git commit`。

```bash
se3 commit -m "描述" -f "file1.py file2.py"
```

## Session Guard (2.1+)

- `se3 work` 和 `se3 done` 会检查 session 是否已通过 `se3 start` 启动
- 未启动时返回错误，提示先运行 `se3 start`