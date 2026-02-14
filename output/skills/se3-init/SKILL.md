---
name: se3-init
description: Initialize SE 3.0 framework in the current project. Creates standard file structure and CLAUDE.md template for AI-first development.
metadata:
  author: se3
  version: "2.0"
---

Initialize the SE 3.0 (Software Engineering 3.0) development framework in the current project.

**Steps**

1. **Check existing project state**

   Check if the following already exist:
   - `demands.md`
   - `progress.md`
   - `se3.config.yaml`
   - `human-calls/` directory
   - `agent-comms/` directory
   - `openspec/` directory
   - `.claude/CLAUDE.md`

2. **Create missing directories**

   ```bash
   mkdir -p human-calls agent-comms
   ```

3. **Initialize openspec if needed**

   If `openspec/` doesn't exist:
   ```bash
   openspec init --tools claude
   ```

4. **Create missing files (do NOT overwrite existing files)**

   - `progress.md` (if missing): Create with template:
     ```markdown
     # Progress

     <!-- 按时间倒序记录每个session的工作内容 -->
     ```

   - `se3.config.yaml` (if missing): Create with default SE 3.0 configuration

   Note: Do NOT create `demands.md` — it will be created through the first human call when the agent starts working.

5. **Set up CLAUDE.md**

   If `.claude/CLAUDE.md` doesn't exist, create it with the SE 3.0 template.

   If it already exists, inform the user that they may want to merge SE 3.0 instructions into their existing CLAUDE.md.

6. **Initialize git if needed**

   If `.git/` doesn't exist:
   ```bash
   git init
   ```

7. **Output summary**

   Summarize what was created/skipped and provide next steps:
   - Run `自行迭代` to start — the agent will ask you about the project intent via human call
   - Check `human-calls/` for any pending async requests

**Guardrails**
- NEVER overwrite existing files — only create missing ones
- NEVER modify existing CLAUDE.md without user confirmation
- Do NOT create intentions.md — project intent is obtained through human calls
