---
name: se3-init
description: Initialize SE 3.0 framework in the current project. Creates standard file structure, configuration, and CLAUDE.md template for AI-first development.
metadata:
  author: se3
  version: "1.0"
---

Initialize the SE 3.0 (Software Engineering 3.0) development framework in the current project.

**Steps**

1. **Check existing project state**

   Check if the following already exist:
   - `intentions.md`
   - `demands.md`
   - `progress.md`
   - `se3.config.yaml`
   - `human-calls/` directory
   - `agent-comms/` directory
   - `openspec/` directory
   - `.claude/CLAUDE.md`

2. **Create missing directories**

   Create any missing directories:
   ```bash
   mkdir -p human-calls agent-comms
   ```

3. **Initialize openspec if needed**

   If `openspec/` doesn't exist:
   ```bash
   openspec init --tools claude
   ```

4. **Create missing files (do NOT overwrite existing files)**

   - `intentions.md` (if missing): Create with template:
     ```markdown
     # 意图

     [在此描述项目的核心意图]
     ```

   - `progress.md` (if missing): Create with template:
     ```markdown
     # Progress

     <!-- 按时间倒序记录每个session的工作内容 -->
     ```

   - `se3.config.yaml` (if missing): Create with default SE 3.0 configuration (see output/se3.config.yaml for template)

5. **Set up CLAUDE.md**

   If `.claude/CLAUDE.md` doesn't exist, create it with the SE 3.0 template (see output/CLAUDE.md for the full template).

   If it already exists, inform the user that they may want to merge SE 3.0 instructions into their existing CLAUDE.md.

6. **Initialize git if needed**

   If `.git/` doesn't exist:
   ```bash
   git init
   ```

7. **Output summary**

   Summarize what was created/skipped and provide next steps:
   - Edit `intentions.md` to describe your project intent
   - Run `自行迭代` to start AI-driven development
   - Check `human-calls/` for any pending human requests

**Guardrails**
- NEVER overwrite existing files - only create missing ones
- NEVER modify existing CLAUDE.md without user confirmation
- Always inform user what was created vs. what was skipped
