# SE3 自举项目

## 项目性质

这是一个**自举项目（Bootstrapping Project）**：

- **目的**：生成新的 SE3 规范
- **现状**：同时使用已发布的 SE3 规范进行开发

## 重要约束

生成新规范时：**不得更改项目使用的已发布规范**

- 已发布的规范位于 `.claude/` 目录中
- 开发依赖的规范文件应保持不变
- 新规范输出到独立的目录或文件

## SE3 命令入口

| Command | 用途 |
|---------|------|
| `/se3:start` | 开始会话 |
| `/se3:work <描述>` | 开始/继续工作 |
| `/se3:done` | 结束会话 |

## Git Commit

使用 `se3 commit` 代替 `git commit`：

```bash
se3 commit -m "描述" -f "file1.py file2.py"
```

## 常见陷阱（Lessons Learned）

### 规范输出位置混淆

**错误**：
- ❌ `.claude/commands/se3/fc.md` — 这是已发布的规范目录，不能修改
- ❌ `openspec/specs/se3-commands/spec.md` — 这是项目自身的规范定义，不是产出

**正确**：
- ✅ `output/commands/se3/fc.md` — 新规范产出目录
- ✅ `tools/` — 工具实现代码

**记忆方法**：
- `.claude/` = 开发依赖的框架规范（只读）
- `openspec/` = 本项目定义的规范（只读，是项目的spec本身）
- `output/` = 生成的新规范产出（可写）
- `tools/` = 工具实现（可写）
