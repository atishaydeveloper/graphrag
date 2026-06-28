# Task 1 — Implementation Report: Chat History & Trace Logs in TigerGraph

> Companion to the design doc ([`task1-chat-history-trace-persistence.md`](./task1-chat-history-trace-persistence.md)). This is the *what was built, how to run it, and how it was verified* record.

**Status:** ✅ Implemented, built into the `graphrag` image, and live-tested end-to-end.

---

## 1. What this task delivered

Conversation history **and** GraphRAG execution traces are now persisted in a dedicated **TigerGraph `ChatHistory` graph**, replacing two legacy stores:

| | Before | After |
|---|--------|-------|
| Chat history | Go `chat-history` service → **SQLite** (`chats.db`) | `graphrag` (Python) → **TigerGraph** `ChatHistory` graph |
| Trace logs | Flat **JSON files** (`trace_logs/*.json`) | `TraceLog` vertices in the same graph |
| Storage durability | Ephemeral (no volume) | On TigerGraph's persistent volume |
| Cross-cutting queries on traces | impossible (isolated files) | graph-queryable |

The Go service is **retired** (removed from `docker-compose.yml`).

## 2. Architecture (after)

```
Browser ──HTTP──> nginx ──> graphrag (FastAPI, Python)
                                │
                  ┌─────────────┴──────────────┐
                  │ ChatHistoryStore (pyTigerGraph)
                  │   writes: upsertVertex/Edge
                  │   reads : installed GSQL queries
                  ▼
        TigerGraph graph "ChatHistory"   (separate from KGs like TigerGraphRAG)
```

The chat graph is **physically separate** from every knowledge graph, so GraphRAG retrieval can never traverse chat data (design D1/D3).

## 3. The `ChatHistory` graph (schema)

```
User(user_id)
  └─HAS_CONVERSATION→ Conversation(conversation_id, name, graphname, created_at, updated_at)
        ├─HAS_MESSAGE→ Message(message_id, role, content, model_name,
        │                      feedback, comment, response_time, seq, created_at)
        │     ├─NEXT→ Message …            (linear order; seq attribute backs reads)
        │     └─HAS_TRACE→ TraceLog(message_id, conversation_id, username,
        │                           user_query, query_sources(JSON), response_type,
        │                           answered_question, response_time,
        │                           natural_language_response, timestamp)
        └─LAST_MESSAGE→ Message            (tail pointer)
```

- **No vector attributes** — chat retrieval is by recency/thread, not similarity (D11).
- **`graphname` is tracked per message** (`Message.graphname`), with `Conversation.graphname` = the *starting* KG (D12 / Option B). A conversation may span KGs (the UI doesn't reset on a graph switch).
- **Only the LLM context is graph-scoped, by design.** `load_conversation_history` (the history fed to the agent for follow-up contextualization) passes the request's `graphname` to `get_conversation_messages`, so the agent receives **only the selected graph's turns** of a conversation — never cross-graph context. The **sidebar conversation list and the visible conversation are NOT filtered** — the user sees all their chats and the full thread.
- The `graphname` filter parameters exist on `get_user_conversations` / `get_recent_messages` / the read endpoints as optional capabilities (default empty = no filter), but the UI does not pass them — only the server-side context loader uses the filter.
- **`User`** is just an anchor, lazily upserted from the authenticated TigerGraph username — TG remains the identity source of truth (D10).

## 4. How it works

**Write (per chat turn)** — `ChatHistoryStore.upsert_message`:
1. Upsert `User`; on the first message, create the `Conversation` (stamping `graphname`); otherwise bump `updated_at`.
2. Compute `seq` from the conversation tail; upsert the `Message`; wire `HAS_MESSAGE`, `NEXT`, repoint `LAST_MESSAGE`.
3. If the message already exists, update only `feedback`/`comment` (dual-purpose, mirroring the old single endpoint).

**Trace** — `save_trace` upserts a `TraceLog` and the `HAS_TRACE` edge.

**Read (scoped to the authenticated user)** — installed GSQL queries that start from the `User` vertex:
- `get_user_conversations(user_id)` → threads, newest first.
- `get_recent_messages(user_id, conversation_id, top_k)` → most-recent N (caller reverses to chronological).

**Isolation** — every read passes the *authenticated* `user_id`; the endpoints ignore any path-param user. A user can only reach data hanging off their own `User` vertex (design §6.1).

## 5. Setup / run (fresh deployment)

The `ChatHistory` graph must exist before `graphrag` uses it. One-time, via the gsql files:

```bash
# 1) schema (global scope) — creates the graph + types
docker exec -u tigergraph tigergraph bash -ic 'gsql -f /path/chat_history_schema.gsql'
# 2) read queries (installs/compiles them on the ChatHistory graph)
docker exec -u tigergraph tigergraph bash -ic 'gsql -f /path/chat_history_queries.gsql'
```

(Auto-init at service startup is deferred — see Future Work.)

## 6. Migrating an existing Go/file-based deployment

Idempotent, re-runnable. Run inside the `graphrag` container (has pyTigerGraph + access):

```bash
TG_USER=tigergraph TG_PASS=tigergraph \
CHATS_DB=/path/chats.db TRACE_DIR=/path/trace_logs \
python -m common.chat_history.migrate
```

It upserts `User`/`Conversation`/`Message` (reconstructing `seq`/`NEXT`/`LAST_MESSAGE` from insertion order) and `TraceLog` vertices. Legacy conversations have no `graphname` → backfilled as `"unknown"` (D12 caveat).

## 7. Verification performed

Module-level (`common/chat_history/store.py`) and live (REST endpoints on the rebuilt image):

| Check | Result |
|-------|--------|
| Multi-turn write → conversation lazily created with `name` + `graphname` | ✅ |
| `get_conversation_messages` → chronological order (seq 0,1,2…) | ✅ |
| Trace save + read; `query_sources` re-parsed from JSON | ✅ |
| **Isolation** — second user sees nothing; `/ui/user/<other>` returns caller's own data | ✅ |
| Feedback update on an existing message (not duplicated) | ✅ |
| `delete_conversation` removes messages + traces; not-owned → refused | ✅ |
| Live chat through `/ui/{graph}/query` → answer + persisted history + trace | ✅ |
| Trace read superuser/owner gate preserved | ✅ |
| **Multi-KG conversation** (GraphA→GraphB) — per-message `graphname` `[A,A,B,B]`, conversation under both KG filters, `Conversation.graphname=GraphA` | ✅ |
| **Per-message `graphname` filter** in `get_conversation_messages` (A→B convo: GraphA→2 msgs, GraphB→2 msgs, other→0) — powers the LLM-context scoping | ✅ |
| **LLM context is graph-scoped** (`load_conversation_history` passes the request graph) while the sidebar + visible conversation stay unfiltered | ✅ |

## 8. Files changed / added

| File | Change |
|------|--------|
| `common/gsql/chat_history/chat_history_schema.gsql` | **new** — graph + types |
| `common/gsql/chat_history/chat_history_queries.gsql` | **new** — installed reads |
| `common/chat_history/__init__.py`, `store.py` | **new** — Python store |
| `common/chat_history/migrate.py` | **new** — migration |
| `graphrag/app/routers/ui.py` | rewired ~9 call sites to the store |
| `docker-compose.yml` | removed the Go `chat-history` service + dependency |
| `configs/server_config.json` | removed `chat_history_api` |

## 9. Key decisions (see design doc for full justifications)

Separate dedicated graph (D1) · global types isolated from retrieval (D2/D3) · retire Go, reimplement in Python (D4) · linear `NEXT` chain, branching unused (D5) · per-role message vertices (D6) · role as attribute not edge (D7) · `seq` + `NEXT` + `LAST_MESSAGE` ordering (D8) · one self-contained `TraceLog` blob (D9) · lazy `User` from TG identity (D10) · no embeddings (D11) · `graphname` on `Conversation` (D12).

## 10. Deviations & future work

**Deviations** (recorded in design §13): `TraceLog` denormalized 3 fields for the owner-check + self-contained reads; reads sort by `seq` rather than reverse-walking `NEXT`; writes use `upsertVertex/Edge` (no write queries).

**Done since first draft:**
- **Service-account connection** — the store connects to `ChatHistory` as one configured service account (`chat_history` config: `graph`/`service_username`/`service_password`), so non-superuser users don't need a per-graph grant. `get_chat_history_store()` takes no creds; isolation still enforced via the authenticated `user_id` passed to queries.

**Future work:**
- **Batched per-turn writes** for atomicity (the `seq` race on concurrent same-conversation writes).
- **Auto-init** the chat graph on a fresh deploy.
- **`get_feedback` admin view** across all users.

**Frontend verification (done):** the conversation sidebar and Trace Logs modal render correctly against the new response shapes. One mismatch was found and fixed — the sidebar showed *"Invalid Date"* because the UI reads `create_ts`/`update_ts` (ISO strings) while the store returned `created_at`/`updated_at` (epoch ints). Fix: `ChatHistoryStore` now also emits `create_ts`/`update_ts` as ISO-8601 strings (no UI change needed).
