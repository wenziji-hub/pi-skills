#!/usr/bin/env python3
"""Import a Codex / Claude Code session into PI-Desktop as a real history session.

Unlike `transcript.py dump` (which only writes a readable file), this persists a
session into PI-Desktop's own store, the way Cline's session-import does:

  <pi-home>/pi.sqlite
    sessions                    one row (title, project, provider/model, seq)
    messages                    text rows per message, ordered by seq
    session_import_origins      source marker, also the dedupe key
  <pi-home>/sessions/<id>.jsonl one `session` header + `message` events

Usage
  import_to_pi.py --session <codex|claude session id> [--dry-run] [--json]
  import_to_pi.py --file <path.jsonl> [--dry-run]
  import_to_pi.py --list
  import_to_pi.py --undo <pi-session-id>

Requires a sibling transcript.py (the extractor). Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import transcript as T  # noqa: E402

PI_HOME = Path.home() / ".pi-desktop"
DB_PATH = PI_HOME / "pi.sqlite"
SESSIONS_DIR = PI_HOME / "sessions"
PLUGIN_ID = "local.codex-claude-transcript-import"
SOURCE_LABEL = {
    "codex": "Codex",
    "claude-code": "Claude Code",
}


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def ms(value: str | None, fallback: int) -> int:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def iso_ms(value: int) -> str:
    return (
        datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------- pi metadata


def default_target(con: sqlite3.Connection) -> tuple[str | None, str | None]:
    """Provider/model a new chat would run on, validated against providers."""
    try:
        raw = con.execute(
            "select value_json from kv where ns='app' and key='app'"
        ).fetchone()
        cfg = json.loads(raw["value_json"]) if raw else {}
    except Exception:
        cfg = {}
    provider = cfg.get("defaultProviderId")
    model = cfg.get("defaultModelId")
    row = con.execute(
        "select id, default_model_id from providers where id=? and enabled=1",
        (provider,),
    ).fetchone()
    if row:
        return row["id"], model or row["default_model_id"]
    row = con.execute(
        "select id, default_model_id from providers where enabled=1"
        " order by created_at limit 1"
    ).fetchone()
    if row:
        return row["id"], row["default_model_id"]
    return None, None


def pick_project(con: sqlite3.Connection, cwd: str) -> int | None:
    """Longest project path that prefixes the session cwd."""
    if not cwd:
        return None
    best: tuple[int, str] | None = None
    for row in con.execute("select id, path from projects"):
        path = row["path"].rstrip("/")
        if cwd == path or cwd.startswith(path + "/"):
            if best is None or len(path) > len(best[1]):
                best = (row["id"], path)
    return best[0] if best else None


def existing_import(
    con: sqlite3.Connection, source: str, external_id: str
) -> str | None:
    row = con.execute(
        "select session_id from session_import_origins"
        " where plugin_id=? and source_id=? and external_id=?",
        (PLUGIN_ID, source, external_id),
    ).fetchone()
    return row["session_id"] if row else None


# ------------------------------------------------------------- event mapping


def build_events(conversation: dict) -> list[dict]:
    """Cline-style messages -> PI event list (user / assistant / tool)."""
    events: list[dict] = []
    by_call: dict[str, dict] = {}
    for message in conversation["messages"]:
        role = message["role"]
        blocks = message.get("blocks") or []
        if role == "user":
            for block in blocks:
                if block["type"] != "tool_result":
                    continue
                target = by_call.get(block["tool_use_id"])
                if target is not None and target["result"] is None:
                    target["result"] = block.get("content") or ""
                    target["is_error"] = bool(block.get("is_error"))
            texts = [b["text"] for b in blocks if b["type"] == "text" and b.get("text")]
            if texts:
                events.append(
                    {
                        "kind": "message",
                        "role": "user",
                        "blocks": [{"type": "text", "text": "\n\n".join(texts)}],
                        "ts": message.get("ts"),
                    }
                )
            continue

        keep: list[dict] = []
        calls: list[dict] = []
        for block in blocks:
            kind = block["type"]
            if kind == "text" and block.get("text", "").strip():
                keep.append({"type": "text", "text": block["text"]})
            elif kind == "thinking" and block.get("thinking", "").strip():
                keep.append({"type": "thinking", "text": block["thinking"]})
            elif kind == "image":
                keep.append({"type": "text", "text": f"[image: {block.get('media_type')}]"})
            elif kind == "tool_use":
                calls.append(block)
        if keep:
            events.append(
                {"kind": "message", "role": "assistant", "blocks": keep, "ts": message.get("ts")}
            )
        for call in calls:
            event = {
                "kind": "tool",
                "name": call.get("name") or "tool",
                "args": call.get("input") or {},
                "call_id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "result": None,
                "is_error": False,
                "ts": message.get("ts"),
            }
            by_call[event["call_id"]] = event
            events.append(event)
    return events


def tool_block(event: dict) -> dict:
    text = event["result"]
    if text is None:
        text = T.MISSING_TOOL_RESULT
    result = {
        "content": [{"type": "text", "text": text}],
        "details": {},
        "isError": bool(event["is_error"]),
    }
    return {
        "type": "tool_call",
        "name": event["name"],
        "args": event["args"],
        "callId": event["call_id"],
        "status": "error" if event["is_error"] else "success",
        "text": json.dumps(result, ensure_ascii=False),
        "result": result,
        "completedAt": event.get("completedAt"),
        "durationMs": 0,
    }


def render_events(events: list[dict], created_ms: int) -> tuple[list[dict], list[dict]]:
    """Returns (jsonl message objects, db rows)."""
    messages: list[dict] = []
    rows: list[dict] = []
    for seq, event in enumerate(events):
        created = ms(event.get("ts"), created_ms)
        if event["kind"] == "tool":
            event["completedAt"] = iso_ms(created)
            message_id = event["call_id"]
            payload = {
                "type": "message",
                "id": message_id,
                "role": "tool",
                "toolName": event["name"],
                "blocks": [tool_block(event)],
                "meta": {"status": "complete"},
                "createdAt": iso_ms(created),
            }
            rows.append(
                {
                    "id": message_id,
                    "seq": seq,
                    "role": "tool",
                    "tool_name": event["name"],
                    "is_error": 1 if event["is_error"] else 0,
                    "text": "",
                    "created_at": created,
                }
            )
        else:
            message_id = str(uuid.uuid4())
            payload = {
                "type": "message",
                "id": message_id,
                "role": event["role"],
                "blocks": event["blocks"],
                "meta": {"status": "complete"},
                "createdAt": iso_ms(created),
            }
            text = "\n\n".join(
                b["text"] for b in event["blocks"] if b["type"] == "text"
            )
            rows.append(
                {
                    "id": message_id,
                    "seq": seq,
                    "role": event["role"],
                    "tool_name": None,
                    "is_error": 0,
                    "text": text,
                    "created_at": created,
                }
            )
        messages.append(payload)
    return messages, rows


# ------------------------------------------------------------------- writing


def backup_db() -> Path:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = PI_HOME / f"pi.sqlite.bak-{stamp}"
    source = sqlite3.connect(DB_PATH)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return target


def write_jsonl(session_id: str, created_iso: str, messages: list[dict]) -> Path:
    path = SESSIONS_DIR / f"{session_id}.jsonl"
    lines = [
        json.dumps(
            {
                "type": "session",
                "schema": 1,
                "sessionId": session_id,
                "createdAt": created_iso,
            },
            ensure_ascii=False,
        )
    ]
    lines += [json.dumps(m, ensure_ascii=False) for m in messages]
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def import_session(conversation: dict, dry_run: bool = False) -> dict:
    con = connect()
    try:
        source = conversation["source"]
        external_id = conversation["session_id"]
        already = existing_import(con, source, external_id)
        if already:
            return {
                "status": "already-imported",
                "session_id": already,
                "source": source,
                "external_id": external_id,
            }

        events = build_events(conversation)
        if not events:
            raise SystemExit("session has no importable messages")

        created_ms = ms(conversation.get("started"), now_ms())
        ended_ms = ms(conversation.get("ended"), created_ms)
        session_id = str(uuid.uuid4())
        messages, rows = render_events(events, created_ms)
        provider_id, model_id = default_target(con)
        project_id = pick_project(con, conversation.get("cwd") or "")
        title = (conversation.get("title") or "Untitled session")[:200]

        plan = {
            "status": "imported" if not dry_run else "dry-run",
            "session_id": session_id,
            "source": source,
            "external_id": external_id,
            "title": title,
            "project_id": project_id,
            "provider_id": provider_id,
            "model_id": model_id,
            "messages": len(rows),
            "tools": sum(1 for r in rows if r["role"] == "tool"),
            "cwd": conversation.get("cwd") or "",
        }
        if dry_run:
            return plan

        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        plan["backup"] = str(backup_db())

        jsonl_path = write_jsonl(session_id, iso_ms(created_ms), messages)
        try:
            with con:
                con.execute(
                    "insert into sessions (id,title,project_id,provider_id,model_id,"
                    "mode,title_source,thinking_level,permission_mode,source,pinned,"
                    "last_seq,todo_revision,created_at,updated_at) "
                    "values (?,?,?,?,?,'agent','manual','off','inherit',NULL,0,?,0,?,?)",
                    (
                        session_id,
                        title,
                        project_id,
                        provider_id,
                        model_id,
                        len(rows) - 1,
                        created_ms,
                        max(ended_ms, created_ms),
                    ),
                )
                con.executemany(
                    "insert into messages (id,session_id,turn_id,seq,role,tool_name,"
                    "is_error,text,created_at,streaming) "
                    "values (?,?,NULL,?,?,?,?,?,?,NULL)",
                    [
                        (
                            r["id"],
                            session_id,
                            r["seq"],
                            r["role"],
                            r["tool_name"],
                            r["is_error"],
                            r["text"],
                            r["created_at"],
                        )
                        for r in rows
                    ],
                )
                con.execute(
                    "insert into session_import_origins (plugin_id,source_id,external_id,"
                    "session_id,source_label,origin_json,created_at) values (?,?,?,?,?,?,?)",
                    (
                        PLUGIN_ID,
                        source,
                        external_id,
                        session_id,
                        SOURCE_LABEL.get(source, source),
                        json.dumps(
                            {
                                "sourcePath": conversation.get("source_path"),
                                "model": conversation.get("model"),
                                "cwd": conversation.get("cwd"),
                                "gitBranch": conversation.get("git_branch"),
                            },
                            ensure_ascii=False,
                        ),
                        now_ms(),
                    ),
                )
        except Exception:
            # Never leave a half-written session behind.
            con.rollback()
            try:
                jsonl_path.unlink()
            except OSError:
                pass
            raise
        return plan
    finally:
        con.close()


def undo(session_id: str) -> dict:
    con = connect()
    try:
        row = con.execute("select id from sessions where id=?", (session_id,)).fetchone()
        if not row:
            return {"status": "not-found", "session_id": session_id}
        with con:
            con.execute("delete from sessions where id=?", (session_id,))
        removed = []
        for path in (
            SESSIONS_DIR / f"{session_id}.jsonl",
            SESSIONS_DIR / f"{session_id}.jsonl.layout",
        ):
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                pass
        return {"status": "deleted", "session_id": session_id, "files": removed}
    finally:
        con.close()


def list_imported() -> list[dict]:
    con = connect()
    try:
        rows = con.execute(
            "select o.source_id, o.external_id, o.session_id, o.created_at, s.title"
            " from session_import_origins o join sessions s on s.id=o.session_id"
            " where o.plugin_id=? order by o.created_at desc",
            (PLUGIN_ID,),
        ).fetchall()
        return [
            {
                "source": r["source_id"],
                "external_id": r["external_id"],
                "session_id": r["session_id"],
                "title": r["title"],
            }
            for r in rows
        ]
    finally:
        con.close()


# ----------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a Codex / Claude session into PI-Desktop.")
    parser.add_argument("--session", help="Codex / Claude Code session id")
    parser.add_argument("--file", help="explicit source .jsonl path")
    parser.add_argument("--source", choices=["all", "codex", "claude-code"], default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--list", action="store_true", dest="do_list")
    parser.add_argument("--undo", metavar="PI_SESSION_ID")
    args = parser.parse_args(argv)

    if args.do_list:
        rows = list_imported()
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for row in rows:
                print(
                    f"{row['source']}\t{row['external_id']}\t{row['session_id']}\t{row['title'][:60]}"
                )
        return 0

    if args.undo:
        result = undo(args.undo)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "deleted" else 1

    if args.file:
        path = Path(args.file).expanduser()
        if not path.is_file():
            print(f"[error] no such file: {path}", file=sys.stderr)
            return 2
        source = args.source if args.source != "all" else T.path_source(path)
    elif args.session:
        found = T.find_session(args.session, args.source)
        if not found:
            print(f"[error] session not found: {args.session}", file=sys.stderr)
            return 2
        source, path = found
    else:
        print("[error] pass --session, --file, --list or --undo", file=sys.stderr)
        return 2

    conversation = T.build(source, path)
    conversation["messages"] = T.sanitize(conversation["messages"])
    if not conversation["messages"]:
        print("[error] no importable messages", file=sys.stderr)
        return 1

    result = import_session(conversation, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        code = main()
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        code = 0
    raise SystemExit(code)
