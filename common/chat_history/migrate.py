# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#
# Task 1 — one-time migration of the legacy stores into TigerGraph.
#
#   - chat history  : the Go service's SQLite file (chats.db)
#   - trace logs    : the flat JSON files under trace_logs/
#
# Idempotent (everything is an upsert), so it can be re-run safely. Run AFTER
# the ChatHistory schema + queries are installed (chat_history_schema.gsql,
# chat_history_queries.gsql).
#
# Usage (inside the graphrag container, or anywhere with pyTigerGraph + access
# to the DB and the legacy files):
#
#   TG_USER=tigergraph TG_PASS=tigergraph \
#   CHATS_DB=/path/chats.db TRACE_DIR=/path/trace_logs \
#   python -m common.chat_history.migrate

import glob
import json
import logging
import os
import sqlite3

from common.chat_history.store import CHAT_GRAPH
from common.db.connections import get_db_connection_pwd_manual

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chat_history.migrate")

LEGACY_GRAPHNAME = "unknown"  # old conversations carry no graphname (D12 caveat)


def _epoch(value) -> int:
    """Best-effort convert a GORM timestamp to epoch seconds."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    # GORM/sqlite often stores datetimes as ISO-ish text; fall back to 0.
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(value).split(".")[0].replace("Z", "")).timestamp())
    except Exception:  # noqa: BLE001
        return 0


def migrate_chat_history(conn, chats_db: str) -> int:
    if not chats_db or not os.path.exists(chats_db):
        logger.info("No chats.db at %r — skipping chat-history migration", chats_db)
        return 0

    db = sqlite3.connect(chats_db)
    db.row_factory = sqlite3.Row
    n_msgs = 0

    convos = db.execute(
        "SELECT user_id, conversation_id, name, created_at, updated_at FROM conversations"
    ).fetchall()
    logger.info("Migrating %d conversation(s)", len(convos))

    for c in convos:
        user_id = c["user_id"]
        conv_id = c["conversation_id"]
        conn.upsertVertex("User", user_id, {"user_id": user_id})
        conn.upsertVertex("Conversation", conv_id, {
            "name": c["name"] or "",
            "graphname": LEGACY_GRAPHNAME,
            "created_at": _epoch(c["created_at"]),
            "updated_at": _epoch(c["updated_at"]),
        })
        conn.upsertEdge("User", user_id, "HAS_CONVERSATION", "Conversation", conv_id)

        # Messages in insertion order (GORM `id` is monotonic).
        msgs = db.execute(
            "SELECT message_id, role, content, model_name, feedback, comment, "
            "response_time, created_at FROM messages "
            "WHERE conversation_id = ? ORDER BY id ASC",
            (conv_id,),
        ).fetchall()

        prev_id = None
        for seq, m in enumerate(msgs):
            mid = m["message_id"]
            conn.upsertVertex("Message", mid, {
                "role": m["role"] or "",
                "content": m["content"] or "",
                "model_name": m["model_name"] or "",
                "graphname": LEGACY_GRAPHNAME,  # legacy messages carry no KG info
                "feedback": m["feedback"] or 0,
                "comment": m["comment"] or "",
                "response_time": m["response_time"] or 0.0,
                "seq": seq,
                "created_at": _epoch(m["created_at"]),
            })
            conn.upsertEdge("Conversation", conv_id, "HAS_MESSAGE", "Message", mid)
            if prev_id:
                conn.upsertEdge("Message", prev_id, "NEXT", "Message", mid)
            prev_id = mid
            n_msgs += 1

        if prev_id:  # tail pointer -> last message
            conn.upsertEdge("Conversation", conv_id, "LAST_MESSAGE", "Message", prev_id)

    db.close()
    logger.info("Chat-history migration done: %d message(s)", n_msgs)
    return n_msgs


def migrate_traces(conn, trace_dir: str) -> int:
    if not trace_dir or not os.path.isdir(trace_dir):
        logger.info("No trace dir at %r — skipping trace migration", trace_dir)
        return 0

    n = 0
    for path in glob.glob(os.path.join(trace_dir, "*.json")):
        try:
            with open(path, "r") as f:
                t = json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.warning("Skipping unreadable trace file %r", path)
            continue
        mid = t.get("message_id")
        if not mid:
            continue
        qs = t.get("query_sources")
        conn.upsertVertex("TraceLog", mid, {
            "conversation_id": t.get("conversation_id", ""),
            "username": t.get("username", ""),
            "user_query": t.get("user_query", ""),
            "query_sources": json.dumps(qs) if qs is not None else "",
            "response_type": t.get("response_type", "") or "",
            "answered_question": bool(t.get("answered_question", False)),
            "response_time": t.get("response_time", 0.0) or 0.0,
            "natural_language_response": t.get("natural_language_response", "") or "",
            "timestamp": int(t.get("timestamp", 0) or 0),
        })
        # Link to its Message if that message exists (it should post-migration).
        conn.upsertEdge("Message", mid, "HAS_TRACE", "TraceLog", mid)
        n += 1

    logger.info("Trace migration done: %d trace(s)", n)
    return n


def main():
    user = os.environ.get("TG_USER", "tigergraph")
    pwd = os.environ.get("TG_PASS", "tigergraph")
    chats_db = os.environ.get("CHATS_DB", "chats.db")
    trace_dir = os.environ.get("TRACE_DIR", "/code/trace_logs")

    conn = get_db_connection_pwd_manual(CHAT_GRAPH, user, pwd)
    logger.info("Connected to graph %s", CHAT_GRAPH)

    migrate_chat_history(conn, chats_db)
    migrate_traces(conn, trace_dir)
    logger.info("Migration complete.")


if __name__ == "__main__":
    main()
