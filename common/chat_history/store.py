# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#
# Task 1 — Chat History & Trace Log persistence in TigerGraph.
#
# Storage layer that replaces the Go `chat-history` service. It talks to the
# dedicated `ChatHistory` graph via pyTigerGraph:
#   - writes  -> upsertVertex / upsertEdge (no installed query needed)
#   - reads   -> installed GSQL queries (get_user_conversations, get_recent_messages)
#
# Isolation (design §6.1): every read is scoped to the authenticated user by
# passing their user_id into queries that start from the User vertex. Callers
# MUST pass the authenticated username — never an arbitrary one.

import json
import logging
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional

from common.config import chat_history_config, db_config
from common.db.connections import get_db_connection_pwd_manual

logger = logging.getLogger(__name__)

# Graph name + service-account credentials for the ChatHistory store.
# The app talks to the ChatHistory graph as a single SERVICE ACCOUNT (not the
# end-user), so individual users don't need a per-graph grant. Per-user
# isolation is still enforced at the query layer (every query is scoped to the
# authenticated user_id). Resolution order for each value:
#   chat_history config  ->  db_config  ->  "tigergraph" default.
CHAT_GRAPH = chat_history_config.get("graph", "ChatHistory")
_SVC_USER = (chat_history_config.get("service_username")
             or db_config.get("username") or "tigergraph")
_SVC_PASS = (chat_history_config.get("service_password")
             or db_config.get("password") or "tigergraph")


def _now() -> int:
    return int(time.time())


def _iso(epoch) -> str:
    """Epoch seconds -> ISO-8601 string the frontend's `new Date(...)` accepts.
    Empty string for missing/zero so the UI can fall back gracefully."""
    try:
        e = int(epoch)
    except (TypeError, ValueError):
        return ""
    if e <= 0:
        return ""
    return datetime.fromtimestamp(e, tz=timezone.utc).isoformat()


def _attrs(rows: List[dict]) -> List[dict]:
    """Flatten pyTigerGraph vertex rows ({v_id, v_type, attributes}) to a plain
    list of attribute dicts. Tolerates rows that are already flat."""
    out = []
    for r in rows or []:
        if isinstance(r, dict) and "attributes" in r:
            out.append(r["attributes"])
        else:
            out.append(r)
    return out


class ChatHistoryStore:
    """All chat-history + trace persistence for one TigerGraph connection to
    the `ChatHistory` graph."""

    def __init__(self, conn):
        self.conn = conn

    # ------------------------------------------------------------------ #
    # Writes                                                             #
    # ------------------------------------------------------------------ #
    def upsert_message(self, message, user_id: str, graphname: Optional[str] = None) -> dict:
        """Create a message (and lazily its User + Conversation), or — if the
        message already exists — update only its feedback/comment.

        Access control (Task 2): a user may only append to / modify a
        conversation they own. A user cannot create-into or inject-into a
        conversation whose id already belongs to someone else, which would
        otherwise leak that conversation to the attacker.
        """
        message_id = message.message_id
        conversation_id = message.conversation_id

        # Ownership probe: the most-recent message reachable from THIS user's
        # vertex for this conversation. None => the user does not own it (or the
        # conversation doesn't exist yet). This is the access-control gate.
        tail = self._get_tail(user_id, conversation_id)

        # Feedback path: message exists -> update feedback/comment, but ONLY if
        # it lives in a conversation this user owns.
        if self._message_exists(message_id):
            if tail is None:
                raise PermissionError(
                    f"user {user_id!r} may not modify message {message_id!r}"
                )
            self.update_feedback(message_id, message.feedback, message.comment)
            return {"conversation_id": conversation_id, "updated": "feedback"}

        now = _now()

        if tail is None:
            # Looks like a new conversation — UNLESS a conversation with this id
            # already exists (it's then someone else's, since the user doesn't
            # own it). Refuse, to stop hijacking another user's conversation_id.
            if self._conversation_exists(conversation_id):
                raise PermissionError(
                    f"conversation {conversation_id!r} is not owned by user {user_id!r}"
                )
            self.conn.upsertVertex("User", user_id, {"user_id": user_id})
            name = (message.content or "")[:60]
            self.conn.upsertVertex("Conversation", conversation_id, {
                "name": name,
                "graphname": graphname or "",
                "created_at": now,
                "updated_at": now,
            })
            self.conn.upsertEdge("User", user_id, "HAS_CONVERSATION",
                                 "Conversation", conversation_id)
            seq = 0
            prev_id = None
        else:
            # Existing conversation the user owns: bump updated_at only (don't
            # clobber name/graphname/created_at).
            self.conn.upsertVertex("Conversation", conversation_id, {"updated_at": now})
            seq = int(tail.get("seq", 0)) + 1
            prev_id = tail.get("message_id")

        self.conn.upsertVertex("Message", message_id, {
            "role": message.role or "",
            "content": message.content or "",
            "model_name": message.model or "",
            # Per-message KG (Option B): a conversation may span KGs; each
            # message records the graph it was answered against.
            "graphname": graphname or "",
            "feedback": message.feedback or 0,
            "comment": message.comment or "",
            "response_time": message.response_time or 0.0,
            "seq": seq,
            "created_at": now,
        })
        self.conn.upsertEdge("Conversation", conversation_id, "HAS_MESSAGE",
                             "Message", message_id)

        # Maintain the linear chain + tail pointer.
        if prev_id:
            self.conn.upsertEdge("Message", prev_id, "NEXT", "Message", message_id)
            # Repoint LAST_MESSAGE: remove the old tail edge, add the new one.
            try:
                self.conn.delEdges("Conversation", conversation_id, "LAST_MESSAGE",
                                   "Message", prev_id)
            except Exception as e:  # noqa: BLE001 - non-fatal; stale pointer is harmless
                logger.warning(f"Failed to clear old LAST_MESSAGE edge: {e}")
        self.conn.upsertEdge("Conversation", conversation_id, "LAST_MESSAGE",
                             "Message", message_id)

        return {"conversation_id": conversation_id, "created": "message"}

    def update_feedback(self, message_id: str, feedback: Optional[int], comment: Optional[str]) -> None:
        """Update only the feedback/comment of an existing message (upsert is a
        partial update — other attributes are untouched)."""
        attrs: Dict[str, Any] = {}
        if feedback is not None:
            attrs["feedback"] = feedback
        if comment is not None:
            attrs["comment"] = comment
        if attrs:
            self.conn.upsertVertex("Message", message_id, attrs)

    def save_trace(self, trace: dict) -> None:
        """Persist one execution trace as a TraceLog vertex, linked 1:1 to its
        assistant Message via HAS_TRACE."""
        message_id = trace["message_id"]
        query_sources = trace.get("query_sources")
        self.conn.upsertVertex("TraceLog", message_id, {
            "conversation_id": trace.get("conversation_id", ""),
            "username": trace.get("username", ""),
            "user_query": trace.get("user_query", ""),
            "query_sources": json.dumps(query_sources) if query_sources is not None else "",
            "response_type": trace.get("response_type", "") or "",
            "answered_question": bool(trace.get("answered_question", False)),
            "response_time": trace.get("response_time", 0.0) or 0.0,
            "natural_language_response": trace.get("natural_language_response", "") or "",
            "timestamp": int(trace.get("timestamp", _now())),
        })
        self.conn.upsertEdge("Message", message_id, "HAS_TRACE", "TraceLog", message_id)

    def delete_conversation(self, user_id: str, conversation_id: str) -> bool:
        """Delete a conversation, its messages, and their traces — only if it
        belongs to `user_id`. Returns False if not owned/found."""
        owned = any(c.get("conversation_id") == conversation_id
                    for c in self.get_user_conversations(user_id))
        if not owned:
            return False
        msgs = self.get_conversation_messages(user_id, conversation_id, top_k=100000)
        for m in msgs:
            mid = m.get("message_id")
            if mid:
                # Deleting a vertex removes its edges automatically.
                try:
                    self.conn.delVerticesById("TraceLog", mid)
                except Exception:  # noqa: BLE001 - trace may not exist
                    pass
                self.conn.delVerticesById("Message", mid)
        self.conn.delVerticesById("Conversation", conversation_id)
        return True

    # ------------------------------------------------------------------ #
    # Reads (scoped to the authenticated user)                           #
    # ------------------------------------------------------------------ #
    def get_user_conversations(self, user_id: str, graphname: str = "") -> List[dict]:
        # Optional graphname filter (Option B): conversations that contain a
        # message answered against that KG. Empty = all.
        res = self.conn.runInstalledQuery(
            "get_user_conversations", {"user_id": user_id, "graphname": graphname or ""}
        )
        convos = _attrs(res[0].get("conversations", [])) if res else []
        # Frontend reads create_ts/update_ts as ISO date strings.
        for c in convos:
            c["create_ts"] = _iso(c.get("created_at"))
            c["update_ts"] = _iso(c.get("updated_at"))
        return convos

    def get_conversation_messages(self, user_id: str, conversation_id: str,
                                  top_k: int = 50, graphname: str = "") -> List[dict]:
        """Most-recent `top_k` messages, returned in chronological order.
        Optional `graphname` keeps only the turns answered against that KG (Option B)."""
        res = self.conn.runInstalledQuery("get_recent_messages", {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "top_k": top_k,
            "graphname": graphname or "",
        })
        msgs = _attrs(res[0].get("messages", [])) if res else []
        # Query returns newest-first (ORDER BY seq DESC); reverse to chronological.
        msgs.sort(key=lambda m: m.get("seq", 0))
        # Frontend reads create_ts/update_ts as ISO date strings (messages have
        # only created_at).
        for m in msgs:
            m["create_ts"] = _iso(m.get("created_at"))
            m["update_ts"] = _iso(m.get("created_at"))
        return msgs

    def get_trace(self, message_id: str) -> Optional[dict]:
        """Return the trace as the same shape the UI expects (query_sources
        re-parsed from JSON). None if not found."""
        rows = _attrs(self.conn.getVerticesById("TraceLog", message_id))
        if not rows:
            return None
        t = rows[0]
        qs = t.get("query_sources") or ""
        try:
            t["query_sources"] = json.loads(qs) if qs else {}
        except (ValueError, TypeError):
            t["query_sources"] = {}
        t["message_id"] = message_id
        return t

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    def _message_exists(self, message_id: str) -> bool:
        try:
            return bool(self.conn.getVerticesById("Message", message_id))
        except Exception:  # noqa: BLE001 - treat lookup failure as "not found"
            return False

    def _conversation_exists(self, conversation_id: str) -> bool:
        """True if any Conversation with this id exists (regardless of owner).
        Used by the access-control gate to detect a foreign conversation_id."""
        try:
            return bool(self.conn.getVerticesById("Conversation", conversation_id))
        except Exception:  # noqa: BLE001
            return False

    def _get_tail(self, user_id: str, conversation_id: str) -> Optional[dict]:
        """The most recent message of the conversation (scoped to user), or None."""
        msgs = self.conn.runInstalledQuery("get_recent_messages", {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "top_k": 1,
        })
        rows = _attrs(msgs[0].get("messages", [])) if msgs else []
        return rows[0] if rows else None


# ---------------------------------------------------------------------- #
# Factory + connection cache                                             #
# ---------------------------------------------------------------------- #
# One shared service-account connection to the ChatHistory graph, cached so we
# don't re-mint a token on every chat turn. The end-user's identity is NOT used
# for the connection — it flows in as the `user_id` argument to the store
# methods (data key + isolation).
_svc_conn: Any = None
_cache_lock = Lock()


def get_chat_history_store() -> ChatHistoryStore:
    global _svc_conn
    with _cache_lock:
        if _svc_conn is None:
            _svc_conn = get_db_connection_pwd_manual(CHAT_GRAPH, _SVC_USER, _SVC_PASS)
    return ChatHistoryStore(_svc_conn)
