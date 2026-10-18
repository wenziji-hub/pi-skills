# 真实字段对照表（Codex / Claude Code）

样本取自本机真实会话（2026-10 版本），字段名可直接依赖。算法参考实现：Cline Desktop
`sdk/packages/core/src/services/session-import/`。

## 归一化后的中间结构

所有源都翻译成这套结构，后续 sanitize / render 只认它：

```jsonc
{
  "source": "codex" | "claude-code",
  "session_id": "...",
  "source_path": "/abs/path.jsonl",
  "title": "...", "cwd": "...", "git_branch": "main",
  "model": "gpt-5-codex" | "claude-sonnet-4-5",
  "started": "2026-10-14T12:11:07+00:00",
  "ended": "2026-10-14T12:40:52+00:00",
  "messages": [
    { "role": "user" | "assistant", "ts": "...", "model": "...", "usage": {...},
      "blocks": [
        { "type": "text", "text": "..." },
        { "type": "thinking", "thinking": "..." },
        { "type": "tool_use", "id": "call_…", "name": "shell", "input": {...} },
        { "type": "tool_result", "tool_use_id": "call_…", "name": "shell",
          "content": "...", "is_error": true },
        { "type": "image", "media_type": "image/png", "data": "<base64>" }
      ] }
  ]
}
```

---

## Claude Code

位置：`$CLAUDE_CONFIG_DIR/projects` 或 `~/.claude/projects/<项目目录>/<session-uuid>.jsonl`
（目录名是 cwd 的有损编码，例如 `-home-user-proj`；**真实 cwd 读行内字段**）

### 行类型（`type`）

| `type` | 用途 | 关键字段 |
|---|---|---|
| `user` | 用户消息 | `uuid` `parentUuid` `isSidechain` `message{role,content}` `timestamp` `cwd` `gitBranch` |
| `assistant` | 助手消息 | 同上 + `message{id,model,content[],usage}` |
| `attachment` | 文件/图片附件 | `uuid` `parentUuid` `attachment` `rendered` |
| `system` | 系统事件 | `subtype` `isMeta` `durationMs` |
| `ai-title` | 会话标题 | `aiTitle` `sessionId` |
| `summary` | 历史摘要标题 | `summary` `leafUuid` |
| `last-prompt` | 最后提示 | `lastPrompt` `leafUuid` |
| `file-history-snapshot` / `mode` / `permission-mode` / `atis-latch` / `cost-state` | 纯元数据 | 忽略 |

### 解析要点

- `message.content` 可能是 **string** 或 **block 数组**；block 类型：`text`、`thinking`、
  `tool_use{id,name,input}`、`tool_result{tool_use_id,content,is_error}`、`image{source{type:base64,media_type,data}}`。
- 树的还原：对**所有**带 `uuid` 的行建 `uuid -> 行` 索引（含 attachment/system），
  从**最后一条** conversation 行出发沿 `parentUuid` 回溯，再反转。
- 过滤：`isSidechain === true`（子代理）、`isMeta === true`、以及以
  `<command-name>` / `<local-command-stdout>` / `<local-command-stderr>` /
  `<command-message>` / `<local-command-caveat>` 开头的用户文本。
- 同一 API turn 会拆成多条 assistant 行，共享 `message.id` → 必须合并。
- `message.model` 为 `<synthetic>` 时是本地错误提示，跳过。
- `usage`：`input_tokens` / `output_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens`。

---

## Codex

位置：`$CODEX_HOME` 或 `~/.codex`；会话在 `sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`，
标题索引在 `session_index.jsonl`（`{id, thread_name}`）。

每个文件都是 `{timestamp, ordinal, type, payload}` 行。顶层 `type` 与 `payload.type` 组合：

| 顶层 `type` | `payload.type` | 用途 | 关键字段 |
|---|---|---|---|
| `session_meta` | — | 会话头 | `payload.id`（= 会话 id，**文件名里没有**）、`payload.session_id`、`cwd`、`git.branch`、`model_provider`、`base_instructions` |
| `turn_context` | — | 每轮配置 | `model`、`cwd`、`effort`、`approval_policy`、`sandbox_policy` |
| `response_item` | `message` | 消息 | `role`∈{user,assistant,developer}、`content[{type:input_text\|output_text,text}]` |
| `response_item` | `reasoning` | 思考 | `summary[{text}]`、`encrypted_content`（不可用） |
| `response_item` | `function_call` | 工具调用 | `name`、`arguments`（JSON **字符串**）、`call_id` |
| `response_item` | `custom_tool_call` | 自定义工具 | `name`、`input`（字符串或对象）、`call_id` |
| `response_item` | `function_call_output` / `custom_tool_call_output` | 工具结果 | `call_id`、`output`（字符串**或 content block 数组**） |
| `event_msg` | `user_message` | 用户提示 | `message`（纯字符串） |
| `event_msg` | `token_count` | 用量 | `info.last_token_usage{input_tokens,output_tokens,cached_input_tokens}` |
| `event_msg` | `task_started` / `task_complete` / `turn_aborted` / `item_completed` | 生命周期 | 可忽略 |
| `compacted` | — | 上下文压缩 | `message`（摘要正文）、`replacement_history`、`window_number` |
| `token_usage_record` | — | 用量记录 | `usage`、`turn_token_usage`、`thread_token_usage` |
| `world_state` | — | 环境快照 | 忽略 |

### 解析要点

- **用户输入有两种落法**：`event_msg.user_message`，或 `response_item.message(role=user)`。
  本机 2026-10 版只有后者，且其中大量是注入内容。规则：若该文件出现过
  `event_msg.user_message`，**忽略** `response_item` 的 user 消息；否则才回退，并过滤
  `<environment_context>` / `<user_instructions>` / `<turn_aborted>` /
  `# AGENTS.md instructions` / `<INSTRUCTIONS>`。
- **一个会话可能横跨多个 rollout 文件**（续聊会新写一个，并重新嵌入原 `session_meta.id`）。
  按 `session_meta.id` 建索引，取"用户消息最多、其次 `ended` 最新"的文件。
- `custom_tool_call_output.output` 可能是 `[{type:"input_text", text:"..."}]`，需拍平。
- `compacted.message` 可作为一条用户消息保留（提醒后续模型这段是被压缩过的历史）。
- `developer` 角色是系统/指令内容，导入时丢弃。

---

## opencode（本 skill 不含脚本，供实现参考）

- 位置：`$XDG_DATA_HOME/opencode/opencode.db`（默认 `~/.local/share/opencode`）
- SQLite；**必须连 `-wal` / `-shm` 一起复制到临时目录再读**，不要直接开原库。
- 每个 source 会话的 provider/model 是 opencode 自己的 id，Cline 不认识，导入时要替换成目标 agent 的模型。

---

## Cline 源码位置（原始算法出处）

| 文件 | 作用 |
|---|---|
| `sdk/packages/core/src/services/session-import/types.ts` | `SessionImportAdapter` / `ConvertedImportedSession` 契约 |
| `sdk/packages/core/src/services/session-import/paths.ts` | 三家 store 路径解析（含 Windows `USERPROFILE`/`HOMEDRIVE`） |
| `sdk/packages/core/src/services/session-import/claude-code.ts` | 树还原、块转换、按 `message.id` 合并 |
| `sdk/packages/core/src/services/session-import/codex.ts` | rollout 索引、注入上下文过滤、输出拍平 |
| `sdk/packages/core/src/services/session-import/sanitize.ts` | 签名剥离、空块清理、tool 配对修复 |
| `sdk/packages/core/src/services/session-import/service.ts` | 去重（`importedFrom` 标记）、落库、失败回滚 |
| `sdk/packages/core/src/extensions/context/compaction.ts` | `createImportedHistoryCompactionPrepareTurn`：续聊时先总结外来历史 |
| `apps/examples/desktop-app/sidecar/commands.ts` | IPC：`list_importable_sessions` / `import_sessions` |

导入后落库的两个关键决定（自己实现时建议照做）：

1. session id 用 `<原始开始时间戳>_<随机后缀>`——历史列表按 id 里的 epoch 前缀排序，这样导入的会话保持原始时间线。
2. 源工具的 provider/model 只写进 `metadata.importedFrom`；会话行上的 provider/model 用当前 agent 的配置，否则续聊会去调一个没配过的 provider。
