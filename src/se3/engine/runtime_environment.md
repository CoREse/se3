## se3 Runtime Environment

You are running inside an se3-managed project. The lists below tell which se3 commands are safe to call proactively, and which are not.

### Safe to call proactively (read-only)

**View past session history** — trigger on 上一个 session / 上次 / 之前的 session / 上次跑到哪了 / 历史记录 / 前几次的对话 / 刚才那次, or similar references to prior runs.

Overview & navigation (preferred first):
- `se3 history list` — list recent flow runs
- `se3 history show <flow_id>` — show structured details of one run
- `se3 history archived` — list archived engine state snapshots

Free-form content search (fallback):
- `se3/history/<flow_id>/<step>.jsonl` — full conversation per step
- `se3/state/archive/engine_*.json` — archived engine state snapshots

**Recommended workflow:** 先用 `se3 history list` 找到目标 `<flow_id>`，再用 `se3 history show <flow_id>` 看结构概貌；若需按关键词搜索，则 `grep -r '关键词' se3/history/<flow_id>/`。

**Consult related issue context** — trigger when the task references an issue, or the user mentions an issue ID.

Overview & navigation (preferred first):
- `se3 issue list` — list issues
- `se3 issue show <id>` — show issue details

Free-form content search (fallback):
- `se3/issues/open/*.yaml`, `se3/issues/closed/*.yaml`

**Recommended workflow:** 先用 `se3 issue list` 看清单；若需按关键词搜索 issue 内容，则 `grep -r '关键词' se3/issues/`。

**Locate code via the code-index** — trigger before reading source: to answer "where does X live / what symbols are in this file", consult the code-index structure map first instead of reading whole files.

Overview & navigation (preferred first):
- `se3 code-index` — show the adaptive root map: a zoomable directory tree expanded to a byte budget (top level always shown; code directories drilled a few levels deep)
- `se3 code-index index <path>` — show exactly ONE literal level at `<path>`: a directory's immediate children (subdirs + files), or a file's functions/methods. Use this to open a directory shown collapsed in the root map.
- `se3 code-index show <path>` — print one file's full function/method-level detail
- `se3 code-index search <pattern>` — search every code-index item (directory / file / symbol) by keyword or regex and print each matching item on one line. Use this **instead of** `grep 'pattern' se3/code-index.md`: a raw grep line for a symbol lacks the file it lives in, whereas each search hit carries the item's full locating path (a symbol renders as `relpath::local_id`). Syntax matches grep: `pattern` is a regex by default (case-sensitive); `-i`/`--ignore-case` for case-insensitive, `-F`/`--fixed-strings` for literal-substring matching, `-m`/`--max-count N` to cap output. Exit code follows grep — 0 on a match, 1 on no match.

These display commands read the committed `se3/code-index.md`; if the map has not been built yet they report that and exit, so run `se3 code-index rebuild` once to generate it (flow steps then keep it fresh incrementally).

The code-index root map is also injected into this step automatically; use `se3 code-index index <path>` to open a collapsed directory one more level, and `se3 code-index show <path>` to pull a file's per-symbol detail on demand.

**Recommended workflow:** 先看注入的 code-index 地图定位相关目录/文件，用 `se3 code-index index <path>` 把折叠的目录再展开一层，再用 `se3 code-index show <path>` 拉取该文件的函数级细节，避免盲目通读整份源码。若要按关键词/正则搜索 code-index item（例如「哪个符号叫 X」），用 `se3 code-index search <pattern>` 而非直接 `grep se3/code-index.md`——search 每条命中自带完整定位路径（symbol 会带上其所属文件），grep 单行拿不到。

### Do NOT call proactively

以下 se3 命令存在但**不在你应主动调用的范围**，除非用户在当前会话中**明确要求**：

- `se3 history restore` — rolls back flow state
- `se3 issue create` / `se3 issue reset` — write operations; do not create issues on your own
- `se3 salvage` — auto-commits, creates issues, archives sessions
- `se3 merge` / `se3 merge respond` — mutates git merge state
- `se3 sync` / `se3 sync respond` — modifies spec files
- `se3 init` — only for initializing brand-new projects
