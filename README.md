# pi-skills

[PI-Desktop](https://pi-desktop.app) 用的 skills 集合。每个 skill 一个目录。

## codex-claude-transcript-import

把 **Codex** 和 **Claude Code** 的本地会话记录读出来：既能导出成可读的 transcript，也能**直接导入成 PI-Desktop 的历史会话**（可在 History 里打开、续聊）。

算法参考 Cline 的 `session-import` 模块（Apache-2.0），这里是为 PI-Desktop 独立重写的实现。

### 安装

把 `codex-claude-transcript-import/` 整个目录复制到本机的 skills 目录（就是 PI-Desktop 里 **Settings → Skills** 显示的那个位置），重启后即可在列表中看到。

也可以不安装，直接从本仓库运行。

### 用法

```bash
S=~/pi-skills/codex-claude-transcript-import/scripts

# 列出本机所有可导入的会话
python3 $S/transcript.py list

# 导出成可读的 Markdown（--json 输出结构化数据）
python3 $S/transcript.py dump --session <id> -o /tmp/session.md
python3 $S/transcript.py dump --session <id> --max-tool-chars 200 -o /tmp/session.md

# 真导入进 PI-Desktop（自动备份数据库）
python3 $S/import_to_pi.py --session <id> --dry-run   # 先看计划，不写库
python3 $S/import_to_pi.py --session <id>
python3 $S/import_to_pi.py --list                     # 已导入清单
python3 $S/import_to_pi.py --undo <pi-session-id>     # 回滚
```

### 支持的源

| 源 | 位置 | 状态 |
|---|---|---|
| Codex | `$CODEX_HOME` 或 `~/.codex/sessions/**/rollout-*.jsonl` | 支持 |
| Claude Code | `$CLAUDE_CONFIG_DIR` 或 `~/.claude/projects/**/*.jsonl` | 支持 |
| opencode | `$XDG_DATA_HOME/opencode/opencode.db` | 未实现（见 `references/format-mapping.md`） |

### 处理了哪些坑

- Claude 的 jsonl 是**树**（`parentUuid` 分叉），不是流水账：从最后一条对话行回溯出活跃分支
- 跳过子代理对话（`isSidechain`）与系统 meta 行；合并同一 `message.id` 的流式分片
- Codex 续聊会产生新 rollout 文件但复用原 `session_meta.id`：按 id 归并，取内容最全的一份
- 过滤 Codex 注入的 `<environment_context>` / `<user_instructions>` / AGENTS.md 等伪用户消息
- 修 `tool_use` ↔ `tool_result` 配对，剥掉只对原 provider 有效的思考签名

### 安全边界

- 只**读** Codex / Claude Code 的存储，绝不写回
- 导入 PI 前用 sqlite backup API 自动备份（`~/.pi-desktop/pi.sqlite.bak-<时间>`）
- 每次导入可用 `--undo` 干净回滚（删会话行、消息、来源登记与 `.jsonl`）

### 验证

```bash
S=~/pi-skills/codex-claude-transcript-import/scripts
python3 $S/transcript.py list | head -5
python3 $S/import_to_pi.py --list
```
