"""Shared helpers for the chat-history / access-control integration tests.

These are INTEGRATION tests: they exercise the running stack (TigerGraph +
graphrag service), not mocks. Run them from inside the graphrag container so
both the store layer (`common.chat_history`) and the HTTP API
(`http://localhost:8000`) are reachable. See README.md.
"""
import base64
import json
import os
import sys
import uuid

import requests

# Make `common.*` importable regardless of the current working directory.
if "/code" not in sys.path:
    sys.path.insert(0, "/code")

BASE = os.environ.get("GRAPHRAG_BASE", "http://localhost:8000")
TG_USER = os.environ.get("TG_USER", "tigergraph")
TG_PASS = os.environ.get("TG_PASS", "tigergraph")
KG = os.environ.get("TEST_GRAPH", "TigerGraphRAG")


def auth_headers(extra=None):
    tok = base64.b64encode(f"{TG_USER}:{TG_PASS}".encode()).decode()
    h = {"Authorization": f"Basic {tok}"}
    if extra:
        h.update(extra)
    return h


def get_store():
    from common.chat_history import get_chat_history_store
    return get_chat_history_store()


def new_message(conversation_id, role, content, graphname=None):
    from common.py_schemas.schemas import Message
    return Message(
        conversation_id=conversation_id,
        message_id=str(uuid.uuid4()),
        role=role,
        content=content,
        model="test",
    )


def api_get(path, headers=None, timeout=30, **kw):
    return requests.get(BASE + path, headers=headers or auth_headers(), timeout=timeout, **kw)


def ask_agent(question, conversation_id="new", timeout=150):
    """Send a question to the KG agent and return the parsed response dict."""
    h = auth_headers({"X-Conversation-Id": conversation_id})
    r = requests.get(BASE + f"/ui/{KG}/query", params={"q": question}, headers=h, timeout=timeout)
    data = r.json()
    if isinstance(data, str):  # endpoint double-encodes the GraphRAGResponse
        data = json.loads(data)
    return data


def uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class Reporter:
    """Minimal PASS/FAIL harness so the suites need no pytest runtime."""

    def __init__(self, suite):
        self.suite = suite
        self.cases = []
        print(f"\n=== {suite} ===")

    def check(self, name, ok, detail=""):
        ok = bool(ok)
        self.cases.append((name, ok, detail))
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {name}" + (f"  ::  {detail}" if detail else ""))
        return ok

    def summary(self):
        passed = sum(1 for _, ok, _ in self.cases if ok)
        total = len(self.cases)
        print(f"  --> {self.suite}: {passed}/{total} checks passed")
        return passed == total
