#!/usr/bin/env python3
"""Extract normalized chat transcripts from Codex / Claude Code local stores.

Mirrors Cline's session-import approach (sdk/packages/core/src/services/
session-import): discover cheaply, reconstruct the active thread, drop injected
context, repair tool_use/tool_result pairing, render one portable transcript.

Commands
  dirs                                    show store locations and presence
  list  [--source all|codex|claude-code] [--limit N]
  dump  (--session <id> | --file <path>) [--source ...] [--json]
        [--max-tool-chars N] [--no-sanitize] [-o OUT]

Stdlib only. Never writes to the source stores.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

MISSING_TOOL_RESULT = (
    "[import] Tool result was not captured in the source session history."
)

# Codex injects these as user-role response items; they are not typed by a person.
INJECTED_USER_PREFIXES = (
    "<user_instructions>",
    "<environment_context>",
    "<ENVIRONMENT_CONTEXT>",
    "<turn_aborted>",
    "# AGENTS.md instructions",
    "<INSTRUCTIONS>",
)

# Claude Code wrappers for non-conversational user lines (slash-command shells).
SKIPPED_USER_PREFIXES = (
    "<command-name>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<command-message>",
    "<local-command-caveat>",
)

EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- small helpers


def warn(message: str) -> None:
    print(f"[warn] {message}", file=sys.stderr)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def iter_jsonl(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        warn(f"cannot read {path}: {exc}")
        return
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue  # one corrupt line never blocks the file
        if isinstance(obj, dict):
            yield obj


def flatten_text(content: object) -> str:
    """Content value -> plain text (Codex content blocks, Claude strings)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(part for part in parts if part.strip())


def is_injected_user_text(text: str) -> bool:
    return text.lstrip().startswith(INJECTED_USER_PREFIXES)


def is_skipped_user_text(text: str) -> bool:
    return text.lstrip().startswith(SKIPPED_USER_PREFIXES)


def clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}\n… [{len(text) - limit} chars truncated]"


# ---------------------------------------------------------------- store paths


def claude_projects_dir() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(configured) if configured else Path.home() / ".claude"
    return base / "projects"


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured) if configured else Path.home() / ".codex"


def claude_files() -> list[Path]:
    root = claude_projects_dir()
    if not root.is_dir():
        return []
    files: list[Path] = []
    for project in sorted(root.iterdir()):
        if project.is_dir():
            files.extend(sorted(p for p in project.glob("*.jsonl") if p.is_file()))
    return files


def codex_files() -> list[Path]:
    root = codex_home() / "sessions"
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("rollout-*.jsonl") if p.is_file())


# ------------------------------------------------------------------- Claude Code


def is_claude_conversation(line: dict) -> bool:
    return (
        line.get("type") in ("user", "assistant")
        and line.get("isSidechain") is not True
        and isinstance(line.get("uuid"), str)
        and isinstance(line.get("message"), dict)
    )


def reconstruct_thread(lines: list[dict]) -> list[dict]:
    """Active branch = parent chain from the last conversation line, reversed."""
    by_uuid: dict[str, dict] = {}
    leaf: dict | None = None
    for line in lines:
        uuid = line.get("uuid")
        if not isinstance(uuid, str) or line.get("isSidechain") is True:
            continue
        by_uuid[uuid] = line
        if is_claude_conversation(line):
            leaf = line
    if leaf is None:
        return []
    thread: list[dict] = []
    seen: set[str] = set()
    current: dict | None = leaf
    while current is not None:
        uuid = current.get("uuid")
        if not isinstance(uuid, str) or uuid in seen:
            break
        seen.add(uuid)
        thread.append(current)
        parent = current.get("parentUuid")
        current = by_uuid.get(parent) if isinstance(parent, str) else None
    thread.reverse()
    return thread


def tool_result_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "image":
                    parts.append("[image]")
        if parts:
            return "\n".join(parts)
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def claude_blocks(content: object, role: str) -> list[dict]:
    if isinstance(content, str):
        if role == "user" and is_skipped_user_text(content):
            return []
        return [{"type": "text", "text": content}] if content.strip() else []
    if not isinstance(content, list):
        return []
    blocks: list[dict] = []
    for raw in content:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind == "text":
            text = raw.get("text")
            if (
                isinstance(text, str)
                and text.strip()
                and not (role == "user" and is_skipped_user_text(text))
            ):
                blocks.append({"type": "text", "text": text})
        elif kind == "thinking":
            thinking = raw.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                blocks.append({"type": "thinking", "thinking": thinking})
        elif kind == "tool_use":
            if isinstance(raw.get("id"), str) and isinstance(raw.get("name"), str):
                payload = raw.get("input")
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": raw["id"],
                        "name": raw["name"],
                        "input": payload if isinstance(payload, dict) else {},
                    }
                )
        elif kind == "tool_result":
            if isinstance(raw.get("tool_use_id"), str):
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": raw["tool_use_id"],
                        "name": "",
                        "content": tool_result_text(raw.get("content")),
                        **({"is_error": True} if raw.get("is_error") is True else {}),
                    }
                )
        elif kind == "image":
            source = raw.get("source")
            if (
                isinstance(source, dict)
                and source.get("type") == "base64"
                and isinstance(source.get("data"), str)
            ):
                blocks.append(
                    {
                        "type": "image",
                        "media_type": source.get("media_type") or "image/png",
                        "data": source["data"],
                    }
                )
    return blocks


def first_user_text(line: dict) -> str | None:
    message = line.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        if content.strip() and not is_skipped_user_text(content):
            return content
        return None
    if isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                if block["text"].strip() and not is_skipped_user_text(block["text"]):
                    return block["text"]
    return None


def claude_meta(path: Path, lines: list[dict]) -> dict:
    meta = {
        "session_id": path.stem,
        "title": None,
        "summary_title": None,
        "preview": None,
        "cwd": "",
        "git_branch": None,
        "model": None,
        "started": None,
        "ended": None,
        "messages": 0,
        "assistant": 0,
    }
    for line in lines:
        kind = line.get("type")
        if kind == "ai-title" and isinstance(line.get("aiTitle"), str):
            meta["title"] = line["aiTitle"].strip() or None
            continue
        if kind == "summary" and isinstance(line.get("summary"), str):
            meta["summary_title"] = line["summary"].strip() or None
            continue
        if not is_claude_conversation(line) or line.get("isMeta") is True:
            continue
        role = "assistant" if kind == "assistant" else "user"
        if role == "user" and not meta["preview"]:
            meta["preview"] = first_user_text(line)
        if role == "assistant":
            meta["assistant"] += 1
            model = (line.get("message") or {}).get("model")
            if isinstance(model, str) and model and not model.startswith("<"):
                meta["model"] = model
        meta["messages"] += 1
        if not meta["cwd"] and isinstance(line.get("cwd"), str):
            meta["cwd"] = line["cwd"]
        branch = line.get("gitBranch")
        if not meta["git_branch"] and isinstance(branch, str) and branch:
            meta["git_branch"] = branch
        ts = parse_ts(line.get("timestamp"))
        if ts:
            meta["started"] = meta["started"] or ts
            meta["ended"] = ts
    return meta


def claude_messages(lines: list[dict], meta: dict) -> list[dict]:
    tool_names: dict[str, str] = {}
    messages: list[dict] = []
    for line in reconstruct_thread(lines):
        if not is_claude_conversation(line) or line.get("isMeta") is True:
            continue
        role = "assistant" if line.get("type") == "assistant" else "user"
        content = (line.get("message") or {}).get("content")
        blocks = claude_blocks(content, role)
        if not blocks:
            continue
        message: dict = {
            "role": role,
            "ts": iso(parse_ts(line.get("timestamp"))),
            "blocks": blocks,
        }
        if role == "assistant":
            payload = line.get("message") or {}
            message_id = payload.get("id")
            model = payload.get("model")
            if isinstance(model, str) and model and not model.startswith("<"):
                message["model"] = model
            usage = payload.get("usage")
            if isinstance(usage, dict):
                message["usage"] = {
                    "input": as_int(usage.get("input_tokens")),
                    "output": as_int(usage.get("output_tokens")),
                    "cache_read": as_int(usage.get("cache_read_input_tokens")),
                    "cache_write": as_int(usage.get("cache_creation_input_tokens")),
                }
            previous = messages[-1] if messages else None
            if (
                previous
                and previous.get("role") == "assistant"
                and isinstance(message_id, str)
                and previous.get("_mid") == message_id
            ):
                previous["blocks"].extend(blocks)
                continue
            if isinstance(message_id, str):
                message["_mid"] = message_id
        messages.append(message)

    for message in messages:
        message.pop("_mid", None)
        for block in message["blocks"]:
            if block["type"] == "tool_use":
                tool_names[block["id"]] = block["name"]
    for message in messages:
        for block in message["blocks"]:
            if block["type"] == "tool_result":
                block["name"] = tool_names.get(block["tool_use_id"], "")
    return messages


# ------------------------------------------------------------------------ Codex


def codex_title_index() -> dict[str, str]:
    titles: dict[str, str] = {}
    path = codex_home() / "session_index.jsonl"
    if not path.is_file():
        return titles
    for line in iter_jsonl(path):
        sid = line.get("id")
        name = line.get("thread_name")
        if isinstance(sid, str) and isinstance(name, str) and name.strip():
            titles[sid] = name.strip()
    return titles


def codex_meta(path: Path, lines: list[dict] | None = None) -> dict:
    records = lines if lines is not None else list(iter_jsonl(path))
    meta: dict = {
        "path": path,
        "session_id": None,
        "cwd": "",
        "git_branch": None,
        "model": None,
        "started": None,
        "ended": None,
        "user_events": 0,
        "fallback_users": 0,
        "assistant_events": 0,
        "first_user": None,
    }
    for line in records:
        payload = line.get("payload")
        if not isinstance(payload, dict):
            continue
        ts = parse_ts(line.get("timestamp"))
        if ts:
            meta["started"] = meta["started"] or ts
            meta["ended"] = ts
        kind = line.get("type")
        if kind == "session_meta":
            sid = payload.get("id") or payload.get("session_id")
            if isinstance(sid, str) and sid:
                meta["session_id"] = sid
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd and not meta["cwd"]:
                meta["cwd"] = cwd
            git = payload.get("git")
            if isinstance(git, dict) and isinstance(git.get("branch"), str):
                meta["git_branch"] = meta["git_branch"] or git["branch"]
        elif kind == "turn_context":
            if isinstance(payload.get("model"), str):
                meta["model"] = payload["model"]
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd and not meta["cwd"]:
                meta["cwd"] = cwd
        elif kind == "event_msg" and payload.get("type") == "user_message":
            text = payload.get("message")
            if isinstance(text, str) and text.strip():
                meta["user_events"] += 1
                meta["first_user"] = meta["first_user"] or text
        elif kind == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            if role == "assistant":
                meta["assistant_events"] += 1
            elif role == "user":
                text = flatten_text(payload.get("content"))
                if text.strip() and not is_injected_user_text(text):
                    meta["fallback_users"] += 1
                    meta["first_user"] = meta["first_user"] or text
    return meta


def codex_user_count(meta: dict) -> int:
    return meta["user_events"] or meta["fallback_users"]


def richer(new: dict, old: dict) -> bool:
    new_users, old_users = codex_user_count(new), codex_user_count(old)
    if new_users != old_users:
        return new_users > old_users
    return (new["ended"] or EPOCH) > (old["ended"] or EPOCH)


def codex_index() -> dict[str, dict]:
    """session id -> {path, meta}, keeping the richest rollout per session.

    Resuming a Codex thread writes a new rollout that re-embeds the original
    session_meta id, so the filename alone cannot group a session.
    """
    best: dict[str, dict] = {}
    for path in codex_files():
        meta = codex_meta(path)
        sid = meta["session_id"]
        if not isinstance(sid, str) or not sid or codex_user_count(meta) == 0:
            continue
        current = best.get(sid)
        if current is None or richer(meta, current["meta"]):
            best[sid] = {"path": path, "meta": meta}
    return best


def codex_output_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts or not value:
            return "".join(parts)
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def codex_messages(path: Path, meta: dict, lines: list[dict] | None = None) -> list[dict]:
    records = lines if lines is not None else list(iter_jsonl(path))
    messages: list[dict] = []
    tool_names: dict[str, str] = {}
    assistant: list[dict] = []
    assistant_ts: datetime | None = None
    pending_results: list[dict] = []
    current_model = meta["model"]

    def flush_assistant() -> None:
        nonlocal assistant, assistant_ts
        if assistant:
            messages.append(
                {
                    "role": "assistant",
                    "ts": iso(assistant_ts),
                    "model": current_model,
                    "blocks": assistant,
                }
            )
            assistant = []
            assistant_ts = None

    def flush_results() -> None:
        nonlocal pending_results
        if pending_results:
            messages.append({"role": "user", "ts": None, "blocks": pending_results})
            pending_results = []

    for line in records:
        payload = line.get("payload")
        if not isinstance(payload, dict):
            continue
        ts = parse_ts(line.get("timestamp"))
        kind = line.get("type")

        if kind == "turn_context":
            if isinstance(payload.get("model"), str):
                current_model = payload["model"]
            continue

        if kind == "event_msg":
            event = payload.get("type")
            if event == "user_message":
                text = payload.get("message")
                if not isinstance(text, str) or not text.strip():
                    continue
                flush_assistant()
                flush_results()
                messages.append(
                    {"role": "user", "ts": iso(ts), "blocks": [{"type": "text", "text": text}]}
                )
            elif event == "token_count":
                info = payload.get("info")
                usage = info.get("last_token_usage") if isinstance(info, dict) else None
                if isinstance(usage, dict) and not assistant:
                    metrics = {
                        "input": as_int(usage.get("input_tokens")),
                        "output": as_int(usage.get("output_tokens")),
                        "cache_read": as_int(usage.get("cached_input_tokens")),
                        "cache_write": 0,
                    }
                    for message in reversed(messages):
                        if message["role"] == "assistant":
                            message["usage"] = metrics
                            break
            continue

        if kind == "compacted":
            summary = payload.get("message")
            if isinstance(summary, str) and summary.strip():
                flush_assistant()
                flush_results()
                messages.append(
                    {
                        "role": "user",
                        "ts": iso(ts),
                        "blocks": [
                            {
                                "type": "text",
                                "text": f"[Conversation summarized by Codex]\n{summary}",
                            }
                        ],
                    }
                )
            continue

        if kind != "response_item":
            continue

        item = payload.get("type")
        if item == "message":
            role = payload.get("role")
            text = flatten_text(payload.get("content"))
            if not text.strip():
                continue
            if role == "assistant":
                flush_results()
                assistant.append({"type": "text", "text": text})
                if ts and assistant_ts is None:
                    assistant_ts = ts
            elif role == "user":
                # Real prompts arrive as user_message events when present;
                # otherwise these user items are mostly injected context.
                if meta["user_events"] > 0 or is_injected_user_text(text):
                    continue
                flush_assistant()
                flush_results()
                messages.append(
                    {"role": "user", "ts": iso(ts), "blocks": [{"type": "text", "text": text}]}
                )
            continue

        if item == "reasoning":
            summary = payload.get("summary")
            text = flatten_text(summary) if isinstance(summary, list) else ""
            if not text.strip():
                continue
            flush_results()
            assistant.append({"type": "thinking", "thinking": text})
            if ts and assistant_ts is None:
                assistant_ts = ts
            continue

        if item in ("function_call", "custom_tool_call"):
            name = payload.get("name") if isinstance(payload.get("name"), str) else "tool"
            call_id = payload.get("call_id")
            if not isinstance(call_id, str):
                call_id = f"import_{len(tool_names)}"
            arguments = payload.get("arguments")
            tool_input: dict = {}
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments)
                    tool_input = parsed if isinstance(parsed, dict) else {"arguments": parsed}
                except Exception:
                    tool_input = {"arguments": arguments}
            elif isinstance(payload.get("input"), str):
                tool_input = {"input": payload["input"]}
            elif isinstance(payload.get("input"), dict):
                tool_input = payload["input"]
            tool_names[call_id] = name
            flush_results()
            assistant.append(
                {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}
            )
            if ts and assistant_ts is None:
                assistant_ts = ts
            continue

        if item in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id")
            if not isinstance(call_id, str):
                continue
            flush_assistant()
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "name": tool_names.get(call_id, ""),
                    "content": codex_output_text(payload.get("output")),
                }
            )
            continue

    flush_assistant()
    flush_results()
    return messages


# --------------------------------------------------------------------- sanitize


def sanitize(messages: list[dict]) -> list[dict]:
    """Make a foreign transcript replayable.

    Providers hard-reject broken tool pairing and signed thinking blocks, so:
    strip signatures / redacted thinking, drop empty text blocks, cull orphan
    tool_results, and consolidate each turn's results into the user message
    right after its tool_use (synthesizing placeholders for missing outputs).
    """
    cleaned: list[dict] = []
    for message in messages:
        blocks: list[dict] = []
        for block in message.get("blocks", []):
            kind = block.get("type")
            if kind == "redacted_thinking":
                continue
            if kind == "thinking":
                text = block.get("thinking") or ""
                if text.strip():
                    blocks.append({"type": "thinking", "thinking": text})
            elif kind == "text":
                text = block.get("text") or ""
                if text.strip():
                    blocks.append({"type": "text", "text": text})
            elif kind == "tool_use":
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.get("id") or f"import_{len(cleaned)}_{len(blocks)}",
                        "name": block.get("name") or "tool",
                        "input": block.get("input") or {},
                    }
                )
            elif kind == "tool_result":
                blocks.append(dict(block))
            elif kind == "image":
                blocks.append(dict(block))
        if blocks:
            cleaned.append({**message, "blocks": blocks})

    known: dict[str, str] = {}
    for message in cleaned:
        if message["role"] != "assistant":
            continue
        for block in message["blocks"]:
            if block["type"] == "tool_use":
                known[block["id"]] = block["name"]

    paired: list[dict] = []
    for message in cleaned:
        if message["role"] != "user":
            paired.append(message)
            continue
        kept = [
            block
            for block in message["blocks"]
            if block["type"] != "tool_result" or block["tool_use_id"] in known
        ]
        if kept:
            paired.append({**message, "blocks": kept})

    repaired: list[dict] = []
    index = 0
    while index < len(paired):
        message = paired[index]
        repaired.append(message)
        if message["role"] != "assistant":
            index += 1
            continue
        uses = [block for block in message["blocks"] if block["type"] == "tool_use"]
        if not uses:
            index += 1
            continue
        use_ids = {block["id"] for block in uses}
        results: dict[str, dict] = {}
        other: list[dict] = []
        end = index + 1
        while end < len(paired) and paired[end]["role"] == "user":
            for block in paired[end]["blocks"]:
                if (
                    block["type"] == "tool_result"
                    and block["tool_use_id"] in use_ids
                    and block["tool_use_id"] not in results
                ):
                    results[block["tool_use_id"]] = block
                elif block["type"] != "tool_result" or block["tool_use_id"] not in use_ids:
                    other.append(block)
            end += 1
        span = paired[index + 1 : end]
        missing = [block for block in uses if block["id"] not in results]
        consolidated = not missing and len(span) == 1 and not other
        if not consolidated:
            merged = {
                "role": "user",
                "ts": span[0].get("ts") if span else None,
                "blocks": [
                    results.get(block["id"])
                    or {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "name": block["name"],
                        "content": MISSING_TOOL_RESULT,
                    }
                    for block in uses
                ],
            }
            repaired.append(merged)
            if other:
                repaired.append(
                    {"role": "user", "ts": span[-1].get("ts") if span else None, "blocks": other}
                )
        index = end
    return repaired


# ---------------------------------------------------------------------- render


def render_markdown(conversation: dict, max_tool_chars: int) -> str:
    lines: list[str] = [f"# {conversation.get('title') or 'Untitled session'}", ""]
    for key in (
        "source",
        "session_id",
        "cwd",
        "git_branch",
        "model",
        "started",
        "ended",
    ):
        value = conversation.get(key)
        if value:
            lines.append(f"- {key}: {value}")
    lines.append(f"- messages: {len(conversation['messages'])}")
    lines += ["", "---", ""]

    for message in conversation["messages"]:
        header = message["role"]
        if message.get("ts"):
            header += f" · {message['ts']}"
        if message.get("model"):
            header += f" · {message['model']}"
        lines += [f"## {header}", ""]
        for block in message["blocks"]:
            kind = block["type"]
            if kind == "text":
                lines.append(block["text"])
            elif kind == "thinking":
                lines.append(
                    "<details><summary>thinking</summary>\n\n"
                    f"{clip(block['thinking'], max_tool_chars)}\n\n</details>"
                )
            elif kind == "tool_use":
                payload = json.dumps(block["input"], ensure_ascii=False, indent=2)
                lines.append(
                    f"**→ {block['name']}**\n```json\n{clip(payload, max_tool_chars)}\n```"
                )
            elif kind == "tool_result":
                label = block.get("name") or block["tool_use_id"]
                flag = " (error)" if block.get("is_error") else ""
                body = block.get("content") or ""
                lines.append(f"**← {label}{flag}**\n```\n{clip(body, max_tool_chars)}\n```")
            elif kind == "image":
                lines.append(f"_[image: {block.get('media_type')}]_")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------- discovery


def claude_summaries() -> list[dict]:
    out: list[dict] = []
    for path in claude_files():
        lines = list(iter_jsonl(path))
        meta = claude_meta(path, lines)
        # Slash-command shells (/fast and friends) have nothing to import.
        if meta["messages"] == 0 or (meta["assistant"] == 0 and not meta["preview"]):
            continue
        out.append(
            {
                "source": "claude-code",
                "session_id": meta["session_id"],
                "path": path,
                "updated": meta["ended"],
                "message_count": meta["messages"],
                "title": meta["title"] or meta["summary_title"] or meta["preview"] or "",
                "cwd": meta["cwd"],
            }
        )
    return out


def codex_summaries() -> list[dict]:
    out: list[dict] = []
    titles = codex_title_index()
    for sid, entry in codex_index().items():
        meta = entry["meta"]
        out.append(
            {
                "source": "codex",
                "session_id": sid,
                "path": entry["path"],
                "updated": meta["ended"],
                "message_count": codex_user_count(meta) + meta["assistant_events"],
                "title": titles.get(sid) or (meta["first_user"] or "").strip(),
                "cwd": meta["cwd"],
            }
        )
    return out


def path_source(path: Path) -> str:
    text = str(path)
    if "/.claude/" in text or path.parent.name.endswith("-projects"):
        return "claude-code"
    if "/.codex/" in text or "/sessions/" in text:
        return "codex"
    return "codex" if path.name.startswith("rollout-") else "claude-code"


def build(source: str, path: Path) -> dict:
    if source == "claude-code":
        lines = list(iter_jsonl(path))
        meta = claude_meta(path, lines)
        messages = claude_messages(lines, meta)
        title = meta["title"] or meta["summary_title"] or (meta["preview"] or "").strip()
        return {
            "source": "claude-code",
            "session_id": meta["session_id"],
            "source_path": str(path),
            "title": (title or "")[:120] or "Untitled session",
            "cwd": meta["cwd"],
            "git_branch": meta["git_branch"],
            "model": meta["model"],
            "started": iso(meta["started"]),
            "ended": iso(meta["ended"]),
            "messages": messages,
        }
    if source == "codex":
        lines = list(iter_jsonl(path))
        meta = codex_meta(path, lines)
        messages = codex_messages(path, meta, lines)
        titles = codex_title_index()
        sid = meta["session_id"] or path.stem
        title = titles.get(sid) or (meta["first_user"] or "").strip()
        return {
            "source": "codex",
            "session_id": sid,
            "source_path": str(path),
            "title": (title or "")[:120] or "Untitled session",
            "cwd": meta["cwd"],
            "git_branch": meta["git_branch"],
            "model": meta["model"],
            "started": iso(meta["started"]),
            "ended": iso(meta["ended"]),
            "messages": messages,
        }
    raise SystemExit(f"unsupported source: {source}")


def find_session(session_id: str, source: str) -> tuple[str, Path] | None:
    if source in ("all", "claude-code"):
        for path in claude_files():
            if path.stem == session_id:
                return "claude-code", path
    if source in ("all", "codex"):
        entry = codex_index().get(session_id)
        if entry:
            return "codex", entry["path"]
    return None


# ------------------------------------------------------------------------- cli


def command_dirs() -> int:
    rows = [
        ("claude-code", claude_projects_dir(), "$CLAUDE_CONFIG_DIR or ~/.claude/projects"),
        ("codex", codex_home() / "sessions", "$CODEX_HOME or ~/.codex/sessions"),
    ]
    for name, path, hint in rows:
        print(f"{name}\t{path}\t{'present' if path.is_dir() else 'missing'}\t{hint}")
    return 0


def command_list(args: argparse.Namespace) -> int:
    rows: list[dict] = []
    if args.source in ("all", "claude-code"):
        rows.extend(claude_summaries())
    if args.source in ("all", "codex"):
        rows.extend(codex_summaries())
    if not rows:
        print("[warn] no sessions found; check `dirs`", file=sys.stderr)
        return 1
    rows.sort(key=lambda row: row["updated"] or EPOCH, reverse=True)
    for row in rows[: args.limit]:
        title = " ".join((row["title"] or "Untitled session").split())[:80]
        cwd = row["cwd"] or "-"
        updated = iso(row["updated"]) or "-"
        print(
            "\t".join(
                [
                    row["source"],
                    row["session_id"],
                    updated,
                    str(row["message_count"]),
                    title,
                    cwd,
                ]
            )
        )
    return 0


def command_dump(args: argparse.Namespace) -> int:
    if args.file:
        path = Path(args.file).expanduser()
        if not path.is_file():
            print(f"[error] no such file: {path}", file=sys.stderr)
            return 2
        source = args.source if args.source != "all" else path_source(path)
    elif args.session:
        found = find_session(args.session, args.source)
        if not found:
            print(
                f"[error] session not found: {args.session} (run `list` for exact ids)",
                file=sys.stderr,
            )
            return 2
        source, path = found
    else:
        print("[error] pass --session or --file", file=sys.stderr)
        return 2

    conversation = build(source, path)
    if not args.no_sanitize:
        conversation["messages"] = sanitize(conversation["messages"])
    if not conversation["messages"]:
        print("[error] no importable messages in this session", file=sys.stderr)
        return 1

    if args.json:
        payload = json.dumps(conversation, ensure_ascii=False, indent=2)
    else:
        payload = render_markdown(conversation, args.max_tool_chars)

    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(
            f"[ok] {conversation['source']} {conversation['session_id']} "
            f"{len(conversation['messages'])} messages -> {args.out}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract normalized transcripts from Codex / Claude Code stores."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("dirs", help="show store locations")

    list_parser = sub.add_parser("list", help="list importable sessions")
    list_parser.add_argument(
        "--source", choices=["all", "codex", "claude-code"], default="all"
    )
    list_parser.add_argument("--limit", type=int, default=50)

    dump_parser = sub.add_parser("dump", help="export one session")
    dump_parser.add_argument("--session", help="session id from `list`")
    dump_parser.add_argument("--file", help="explicit .jsonl path")
    dump_parser.add_argument(
        "--source", choices=["all", "codex", "claude-code"], default="all"
    )
    dump_parser.add_argument("--json", action="store_true", help="structured output")
    dump_parser.add_argument("--max-tool-chars", type=int, default=800)
    dump_parser.add_argument("--no-sanitize", action="store_true")
    dump_parser.add_argument("-o", "--out", help="write to file instead of stdout")

    args = parser.parse_args(argv)
    if args.command == "dirs":
        return command_dirs()
    if args.command == "list":
        return command_list(args)
    if args.command == "dump":
        return command_dump(args)
    return 2


if __name__ == "__main__":
    try:
        exit_code = main()
        sys.stdout.flush()
    except BrokenPipeError:
        # `| head` closes stdout early; exit quietly like other CLI tools.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        exit_code = 0
    raise SystemExit(exit_code)
