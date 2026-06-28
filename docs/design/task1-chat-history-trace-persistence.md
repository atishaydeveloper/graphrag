# Task 1 — Chat History & Trace Log Persistence in TigerGraph

**Status:** Design proposal
**Scope:** Persist conversation history and GraphRAG execution traces in TigerGraph, replacing the current SQLite + flat-file stores.

---

## 1. Objective

Design and implement a solution to persist **conversation history** and **GraphRAG execution trace information** inside TigerGraph, supporting storage, retrieval, and management of both. The schema, data model, and approach are open-ended; this document records and justifies each decision.

---

## 2. Background: how it works today

### 2.1 Chat history — Go microservice + SQLite
- A dedicated **Go service** (`chat-history`, port 8002) persists conversations to a **SQLite** file (`chats.db`) via GORM.
- Two tables: `Conversation` (`conversation_id`, `user_id`, `name`) and `Message` (`message_id`, `conversation_id`, `parent_id`, `role`, `content`, `model_name`, `feedback`, `comment`, `response_time`).
- Messages reference a `parent_id`, theoretically forming a **tree** (to support branching). The Python `graphrag` service calls this service over HTTP (`chat_history_api` config) to read/write history and passes the conversation into the LangGraph agent.

### 2.2 Trace logs — flat JSON files
- For every assistant answer, `_save_trace_log` writes one file `/code/trace_logs/{message_id}.json`.
- Fields: `message_id`, `conversation_id`, `username`, `user_query`, `response_time`, `response_type`, `answered_question`, `query_sources` (which retriever/queries ran), `natural_language_response`, `timestamp`.
- Read via `GET /ui/trace/{message_id}`, gated **superuser-only + owner-only** (traces may contain PII, full LLM output, and cost). Files age out after 30 days. The Trace Logs UI reconstructs the "agent steps / tool calls / citations" view **client-side** from this single blob.

### 2.3 User identity
- **There is no separate user store.** Users are **TigerGraph database users**; identity comes from HTTP Basic Auth / token login, and roles from `SHOW USER` in GSQL. The `User` struct in the Go service is dead code (never migrated). The `user_id` on a conversation is simply the TigerGraph username.

### 2.4 Limitations motivating this work
1. **Two heterogeneous stores** (SQLite + JSON files) outside the database of record.
2. **Not durable by default** — neither `chats.db` nor `trace_logs/` is mounted to a volume; both are wiped on container recreation.
3. **Traces are unqueryable** — isolated files can't answer cross-cutting questions ("all low-rated community-search answers slower than 5 s").
4. **Extra language boundary** — the Go service exists only as a thin fetch/store layer.

---

## 3. Goals and non-goals

**Goals**
- Single source of truth for chat history **and** traces inside TigerGraph.
- Preserve existing behaviour: per-user isolation, recent-history retrieval for follow-ups, feedback, and superuser/owner-gated trace access.
- Make traces **graph-queryable**.
- Lossless migration path from the current stores.

**Non-goals**
- Semantic search over past conversations (no embeddings — see D11).
- Replacing TigerGraph as the identity provider (auth stays with TG — see D10).
- Re-architecting the LangGraph agent (only the storage/fetch layer changes).

---

## 4. Design decisions & justifications

| # | Decision | Justification |
|---|----------|---------------|
| **D1** | Store chat/trace data in a **dedicated, separate graph**, not inside any knowledge-graph (e.g. `TigerGraphRAG`). | The retrievers traverse `Entity`/`DocumentChunk`/`Community`. Mixing operational chat data into a KG risks it being swept into retrieval and corrupting answers. A separate graph gives hard isolation. |
| **D2** | Model `User`/`Conversation`/`Message`/`TraceLog` as **global vertex/edge types**. | Today chat history is **global** across all knowledge graphs (one SQLite, no `graphname` on conversations). Global types reproduce that semantics faithfully — a user's history is visible regardless of which KG they chat against. |
| **D3** | Guarantee the chat graph is **never reached by GraphRAG retrieval**. | Correctness. Because it is a physically separate graph (D1), the retrievers — scoped to the selected KG — cannot traverse it. No `is_structural_type` / `retrieval_include_entity` tuning needed. |
| **D4** | **Retire the Go service; reimplement history in Python** (`graphrag` service). | The Python service already owns the TigerGraph connection (`pyTigerGraph`), the agent, and the trace-save path. Consolidating removes a language boundary; Go has no first-class TG client and only ever acted as a fetcher. The HTTP `chat_history_api` call becomes an in-process TigerGraph query. |
| **D5** | Model conversations as a **linear `NEXT` chain**, not a tree. | Branching is **unimplemented**: the "Regenerate" button is a no-op `alert("Regenerate!!")`, and the write path only ever creates linear user→assistant pairs (`prev_id` resets each request). A linear chain models actual behaviour. The edge **generalises to a tree for free** (a message simply gains a second incoming edge) if regenerate is ever built — present modelled without foreclosing the future. |
| **D6** | **Separate `Message` vertex per role** (user vs assistant), not a combined Q&A node. | Feedback and traces attach to the *assistant* message specifically; regeneration (if added) forks at the assistant message; migration stays 1:1 with today's per-role rows; uniform node shape keeps traversal simple. |
| **D7** | Store **role as an attribute**, not as an edge type (`USER_ASKED`/`SYSTEM_REPLIED`). | Principle: *edges model what you traverse; attributes model what you filter/display.* Role is a property you filter on, not a path you walk. Encoding it in edge types breaks uniform chain traversal for no query benefit; the Q→A pairing is already implied by the chain. |
| **D8** | Order via a **`NEXT` edge + monotonic `seq` integer**, plus a `Conversation —LAST_MESSAGE→ Message` tail pointer. | A graph does not auto-return rows in insertion order. `NEXT` encodes sequence structurally; `seq`/`created_at` gives robust sort + tie-breaking; the tail pointer makes "most recent N" an O(N) reverse walk with no global sort. |
| **D9** | **One `TraceLog` vertex per assistant message**; store `query_sources` as a serialized **JSON string**. | The agent captures no discrete per-step telemetry — the UI's "steps" are reconstructed client-side from this single blob. Stepwise vertices would model structure that doesn't exist. A blob gives lossless 1:1 migration *and* the graph upgrade (traces become queryable via links to `Message`/`User`). Stepwise decomposition is deferred until the agent emits per-step data. |
| **D10** | **Lazily upsert a `User` vertex** keyed by the TigerGraph username; TG remains the identity/auth source of truth. | There is no user store to migrate. The `User` vertex is an *anchor* for conversations, not an auth record. Upsert-by-username is idempotent (same name → same vertex) and doubles as the "new vs returning user" signal. |
| **D11** | **No embeddings** on chat/trace vertices. | Retrieval here is by **recency + thread**, never semantic similarity. Vectors would add embedding cost and storage with zero benefit. (Semantic search over history is possible future work, explicitly out of scope.) |
| **D12** | Track the KG **per message** (`Message.graphname`); `Conversation.graphname` records only the **starting** KG. Conversations **may span KGs** (Option B). | The KG is **chosen by the user** per request (URL path `/ui/{graphname}/...`, RBAC-enforced). The UI does **not** reset the conversation when the graph dropdown changes, so a single conversation can switch KGs mid-thread — binding a whole conversation to one KG would *mislabel* it. Recording `graphname` on **each message** keeps the per-answer KG authoritative and immutable; the conversation-list filter matches conversations that **contain a message from** the target KG (a multi-KG conversation appears under *each* of its KGs). Contextualization loads the full conversation thread — intra-conversation cross-KG context is intentional once we allow spanning. *(Original design said "one conversation = one KG, switching starts a new conversation" — Option A; revised to B because nothing enforced that invariant. See §13.)* |

---

## 5. Proposed schema (GSQL)

```gsql
// Dedicated chat-history graph; global types shared across all knowledge graphs.
VERTEX User(PRIMARY_ID user_id STRING)

VERTEX Conversation(PRIMARY_ID conversation_id STRING, name STRING,
                    graphname STRING,   /* the KG this conversation STARTED in (D12) */
                    created_at UINT, updated_at UINT)

VERTEX Message(PRIMARY_ID message_id STRING, role STRING, content STRING,
               model_name STRING, graphname STRING, /* per-message KG (D12/Option B) */
               feedback INT, comment STRING,
               response_time DOUBLE, seq UINT, created_at UINT)

VERTEX TraceLog(PRIMARY_ID message_id STRING,
                conversation_id STRING, username STRING,   /* denormalized — see D9 note */
                user_query STRING,
                query_sources STRING /* serialized JSON */, response_type STRING,
                answered_question BOOL, response_time DOUBLE,
                natural_language_response STRING, timestamp UINT)

DIRECTED EDGE HAS_CONVERSATION (FROM User,         TO Conversation) WITH REVERSE_EDGE="reverse_HAS_CONVERSATION";
DIRECTED EDGE HAS_MESSAGE      (FROM Conversation, TO Message)      WITH REVERSE_EDGE="reverse_HAS_MESSAGE";
DIRECTED EDGE NEXT             (FROM Message,      TO Message)      WITH REVERSE_EDGE="reverse_NEXT";
DIRECTED EDGE LAST_MESSAGE     (FROM Conversation, TO Message)      WITH REVERSE_EDGE="reverse_LAST_MESSAGE";
DIRECTED EDGE HAS_TRACE        (FROM Message,      TO TraceLog)     WITH REVERSE_EDGE="reverse_HAS_TRACE";
```

```
User(user_id)
  └─HAS_CONVERSATION→ Conversation(conversation_id, name, graphname, created_at, updated_at)
        ├─HAS_MESSAGE→ Message(message_id, role, content, model_name,
        │                      feedback, comment, response_time, seq, created_at)
        │     ├─NEXT→ Message …            (linear order; seq attribute backs it)
        │     └─HAS_TRACE→ TraceLog(...)   (assistant messages only)
        └─LAST_MESSAGE→ Message            (tail pointer → fast "recent N")
```

**Naming:** types live in a separate graph, so there is no collision with the SupportAI/GraphRAG reserved names (`Document`, `Entity`, `Community`, …).

---

## 6. Data-model details

- **Primary IDs** reuse existing UUIDs (`message_id`, `conversation_id`) as STRING primary ids; `User.user_id` is the TG username. Deterministic ids make all writes idempotent **upserts**.
- **Recency query** ("last *N* messages of a conversation"): start at `Conversation —LAST_MESSAGE→`, walk `reverse_NEXT` *N* times (or `ORDER BY seq DESC LIMIT N`). Returned to the agent in the same shape it expects today, so the agent is unchanged.
- **Feedback update**: `feedback`/`comment` are attributes on `Message`; a thumbs-up/down is an attribute upsert on the existing vertex — mirrors the current "update only feedback/comment" path.
- **Access control is preserved in the Python layer**, not the database: history reads are filtered to the authenticated TG username; trace reads keep the **superuser + owner** gate (role via `SHOW USER`, owner via `TraceLog`/`Message`→`User`). Moving storage must not relax this.

### 6.1 Isolation model — one graph, a per-user forest

All users' data lives in **a single chat-history graph**, *not* one graph per user. Within it, each user's data forms a **disconnected component** rooted at their `User` vertex (unique UUIDs mean no `Conversation`/`Message` is ever shared across users), so the graph is naturally a **forest** of per-user subgraphs:

```
USER A cluster        USER B cluster        USER C cluster
User(alice)           User(bob)             User(carol)
  └─ convos/msgs/…      └─ convos/msgs/…      └─ convos/msgs/…
       (no edges cross between user clusters)
```

**Critical:** this structural separation is *organization, not authorization*. A graph database keeps all vertices in one queryable space — an unscoped query (`SELECT all Message`) would return **every** user's data. Therefore **per-user isolation must be enforced at the query layer, not by graph topology**:
- Every history read **starts from the authenticated user's `User` vertex** (`User(<tg-username>) → HAS_CONVERSATION → …`) — never from a global vertex scan.
- Trace reads additionally enforce the **superuser + owner** gate.
- This mirrors the current SQLite service, which checks conversation ownership before returning messages; the new queries must preserve that check (add a regression test — see §12).

**Why not a graph per user?** Per-user named graphs would each carry their own schema and installed-query set — unmanageable at scale (N users ⇒ N graphs to migrate/install). One graph with global types and query-time scoping is the efficient, scalable model.

---

## 7. Read / write flows

**Write (per chat turn), in the Python `graphrag` service:**
1. Upsert `User` (by TG username) and `Conversation` (create on first turn, stamping `graphname` from the request path per D12; bump `updated_at`).
2. Upsert the **user** `Message`; link `HAS_MESSAGE`, `NEXT` (from previous tail), repoint `LAST_MESSAGE`, set `seq`.
3. Run the agent; upsert the **assistant** `Message` likewise.
4. Upsert the `TraceLog` for the assistant message and link `HAS_TRACE`.

**Read:**
- *List conversations* → `User —HAS_CONVERSATION→ Conversation`.
- *Recent history for follow-up contextualization* → tail-walk as above, **scoped to the conversation's `graphname`** (D12) so cross-KG context can't bleed in; hand the conversation list to the agent (unchanged `contextualize_question` flow).
- *Trace view* → `Message —HAS_TRACE→ TraceLog`, after the superuser/owner check.

All read/write paths are **installed GSQL queries** in the chat graph (compiled once, called by name), consistent with how the rest of the system calls TigerGraph.

---

## 8. Implementation surface (files expected to change)

| Area | Change |
|------|--------|
| **New GSQL** | `common/gsql/chat_history/` — schema job + installed queries (upsert message, list conversations, recent-N, write/read trace, update feedback). |
| **New Python module** | A `ChatHistory`/`TraceStore` class in the `graphrag` service wrapping the chat-graph connection (replaces the HTTP client to `chat-history:8002`). |
| **`graphrag/app/routers/ui.py`** | Replace `write_message_to_history`/history fetch with the new module; rewrite `_save_trace_log` and `get_trace_log` to write/read `TraceLog` vertices (keeping the access-control checks). |
| **Config** | Add chat-graph connection settings; remove `chat_history_api`. |
| **`docker-compose.yml` / k8s** | Remove the `chat-history` Go service; ensure the chat graph is initialized/installed on startup. |
| **Migration script** | One-off: read `chats.db` + `trace_logs/*.json` → upsert vertices/edges (see §9). |
| **Removed** | The entire `chat-history/` Go module. |

---

## 9. Migration plan

One-time, idempotent script (re-runnable thanks to upserts):
1. **Schema** — create the chat graph, apply the schema job, `INSTALL QUERY ALL`.
2. **Chat history** — read `Conversation`/`Message` rows from `chats.db`; upsert `User` (from `user_id`), `Conversation`, `Message`; derive `seq`/`NEXT`/`LAST_MESSAGE` from `parent_id` + timestamps. **Legacy caveat:** existing conversations carry no `graphname` (D12) — backfill as `""`/`"unknown"`; only conversations created after the migration will be KG-scoped.
3. **Traces** — read each `trace_logs/{message_id}.json`; upsert a `TraceLog` and link `HAS_TRACE` to the matching `Message`.
4. **Verify** — counts match source; spot-check a conversation reconstructs in order and a trace round-trips.

Default posture: **migrate existing data** (history has user value). If a clean cut is preferred, the same script doubles as the bootstrap and migration is "future work."

---

## 10. Trade-offs & alternatives considered

| Decision | Alternative | Why rejected |
|----------|-------------|--------------|
| Separate graph (D1) | Store in each KG graph | Per-corpus siloing + retrieval-pollution risk. |
| Retire Go (D4) | Keep Go API, swap SQLite→TG backend | Lowest blast radius, but Go lacks a first-class TG client and keeps an unnecessary language boundary. |
| Linear chain (D5) | Preserve full tree | Branching is unused (stub); linear is simpler and still tree-extensible. |
| Trace blob (D9) | Per-step `Step` vertices | Agent emits no per-step data; would model nonexistent structure. |
| No embeddings (D11) | Embed messages for semantic recall | No current requirement; pure cost. |

---

## 11. Future work
- Semantic search over past conversations (add a vector attribute + embedding on `Message`).
- Stepwise trace vertices **iff** the agent is instrumented to emit per-step timing/cost.
- Retention/TTL policy in-graph (replacing the 30-day file cleanup).
- Tree/branching support if a real "Regenerate/Edit" feature ships.

## 12. Risks & open questions
- **Write latency** — every turn now does several TG upserts vs one SQLite transaction; batch the per-turn writes into a single `upsertData` call.
- **Durability** — the chat graph must sit on TigerGraph's persistent volume (already true for the DB), removing today's ephemeral-file gap.
- **Backfill correctness** — reconstructing `seq`/`NEXT` from `parent_id` when historical chains are sparse; validate during migration.
- **Access-control parity** — the superuser/owner gate must be re-implemented exactly in the new read path; add a regression test.

---

## 13. Implementation status & deviations (as built)

**Status: implemented and live-tested.** All write/read/trace paths run against the TigerGraph `ChatHistory` graph; the Go service is retired. Three deviations from the design above were made during implementation and are recorded here so the doc matches the code:

| # | Deviation | Why |
|---|-----------|-----|
| **A** | **`TraceLog` denormalizes `username`, `conversation_id`, `natural_language_response`** (D9 originally listed only the trace-specific fields). | The trace read must enforce the **superuser + owner** gate, which needs `username` on the vertex; storing the answer text + conversation id too makes a trace a self-contained record (parity with the old JSON file) and a single-vertex read — no traversal. |
| **B** | **Reads use `ORDER BY seq DESC` (not a `reverse_NEXT` walk).** `NEXT` + `LAST_MESSAGE` are still maintained on write for structural fidelity (D5/D8), but the installed `get_recent_messages` query sorts by `seq`. | The design (§6) explicitly offered `ORDER BY seq` as the alternative; it's simpler and correct. The structural edges remain so the model stays graph-native and tree-extensible. |
| **C** | **Writes use `pyTigerGraph.upsertVertex/Edge` from Python; only the two non-trivial reads are installed GSQL.** | Matches how `ecc` writes; no write queries to maintain. |
| **D** | **D12 implemented as Option B (per-message KG), not Option A (one conversation = one KG).** `Message` gained a `graphname` attribute; `get_user_conversations(user_id, graphname="")` filters to conversations containing a message from that KG; `Conversation.graphname` = the *starting* KG. | The UI does **not** reset the conversation when the graph dropdown changes, so the original "switching starts a new conversation" invariant was never enforced — a conversation could silently span KGs while its single `graphname` went stale. Option B records the KG authoritatively per message. **The graph filter is applied to exactly one consumer: the LLM context.** `load_conversation_history` passes the request's `graphname` to `get_conversation_messages`, so a follow-up's agent context contains only the selected graph's turns (no cross-graph bleed). The **sidebar list and the visible conversation are intentionally NOT filtered** — the user sees all chats and the full thread; only what reaches the LLM is scoped. The optional `graphname` params on `get_user_conversations`/`get_recent_messages`/the read endpoints remain as capabilities (default = no filter) but the UI does not pass them. Verified: a GraphA→GraphB conversation has per-message `[A,A,B,B]`, and `get_conversation_messages` returns 2/2/0 for GraphA/GraphB/other (the set that becomes LLM context). |

**Service-account connection (implemented).** The store no longer connects as the end-user — it uses a single **service-account** connection to the `ChatHistory` graph, configured under `chat_history` in `server_config.json` (`graph`, `service_username`, `service_password`; falls back to `db_config` creds then `tigergraph`). So non-superuser users do **not** need a per-graph grant. Isolation is unchanged — every query is still scoped to the authenticated `user_id`, which the endpoints pass from `creds.username` (never a path param). `get_chat_history_store()` takes no creds; one cached `_svc_conn` serves all requests.

**Deferred (future work, consistent with §12):**
- **`get_feedback` admin "all-users" view** — currently returns the authenticated user's feedback only; the admin/superuser full-corpus view needs an unscoped query.
- **Auto-init on startup** — the `ChatHistory` graph is created by running the two `.gsql` files once (like KG setup), not auto-created at boot (which needs the service-account credential above).

### File map (as built)
| File | Role |
|------|------|
| `common/gsql/chat_history/chat_history_schema.gsql` | Graph + global vertex/edge types |
| `common/gsql/chat_history/chat_history_queries.gsql` | Installed read queries (`get_user_conversations`, `get_recent_messages`) |
| `common/chat_history/store.py` | `ChatHistoryStore` + `get_chat_history_store` factory |
| `common/chat_history/migrate.py` | One-time SQLite + JSON → graph migration |
| `graphrag/app/routers/ui.py` | All chat/trace endpoints rewired to the store |
| `docker-compose.yml`, `configs/server_config.json` | Go service removed; `chat_history_api` removed |
