# Source Changes — Tasks 1–3

A reviewer's map of every change, grouped by task. `A` = added, `M` = modified.

## Task 1 — Chat history & traces in TigerGraph
Replace the legacy Go + SQLite chat-history service with a TigerGraph-native store.

| File | | What |
|---|---|---|
| `common/gsql/chat_history/chat_history_schema.gsql` | A | `ChatHistory` graph schema: `User`, `Conversation`, `Message`, `TraceLog` vertices + `HAS_CONVERSATION`/`HAS_MESSAGE`/`NEXT`/`LAST_MESSAGE`/`HAS_TRACE` edges |
| `common/gsql/chat_history/chat_history_queries.gsql` | A | Access-scoped queries: `get_user_conversations`, `get_recent_messages`, etc. (all start from the `User` vertex) |
| `common/chat_history/__init__.py` | A | Package + `get_chat_history_store()` factory |
| `common/chat_history/store.py` | A | `ChatHistoryStore`: upsert/read/delete, monotonic `seq`, per-message `graphname` filter (filter-then-limit) |
| `common/chat_history/migrate.py` | A | One-time, idempotent migration of the old `chats.db` + trace JSON into TigerGraph |
| `common/config.py` | M | Expose `chat_history_config` from `server_config.json` |
| `configs/server_config.json` | M | Add the `chat_history` block (graph name + service account) |
| `graphrag/app/routers/ui.py` | M | Rewire history/trace endpoints + `load_conversation_history` to the store |

## Task 2 — Conversation access control & hardening
| File | | What |
|---|---|---|
| `common/chat_history/store.py` | M | **Write-side ownership gate** in `upsert_message` (+ `_conversation_exists`): a user can't inject into / hijack / tamper-feedback a conversation they don't own. **Service-account** connection (`get_chat_history_store()` takes no creds) |
| `graphrag/app/routers/ui.py` | M | All history endpoints scope by `creds.username` (never a path id); `conversation_id` moved to the **`X-Conversation-Id` header** for read/delete/query (out of URLs/logs); `auth()` **filters `ChatHistory`** out of `list_graphs`; `/{graph}/query` + `/{graph}/chat` **reject `ChatHistory`** |
| `graphrag-ui/src/components/SideMenu.tsx` | M | Send `X-Conversation-Id` header instead of putting the id in the fetch URL; (earlier: date-render fix, all-conversations sidebar) |
| `common/config.py` | M | `chat_history_config` graph name drives the list filter |

## Task 3 — Agent access restrictions
| File | | What |
|---|---|---|
| `graphrag/app/routers/ui.py` | M | `_is_history_probe()` + a **deterministic refusal pre-check** at the top of `run_agent()` (the shared REST + WebSocket chokepoint) |
| `common/llm_services/base_llm.py` | M | A **"Scope — refuse out-of-scope data requests"** rule added to the default `chatbot_response` prompt (defense-in-depth; the prompt `.txt` files are absent on this Windows checkout, so the built-in default is what runs) |

## Infrastructure / setup (supporting the above)
| File | | What |
|---|---|---|
| `docker-compose.yml` | M | Retire the Go `chat-history` service |
| `docker-compose.override.yml` | A | Local dev overrides |
| `configs/nginx.conf` | M | Drop the Go-service proxy route |
| `configs/server_config.gemini.json` | A | Gemini config variant |
| `.dockerignore` | A | Build-context hygiene |
| `.gitignore` | M | Ignore local artefacts |
| `graphrag/Dockerfile`, `ecc/Dockerfile` | M | Repo build-context adjustments |

## Tests & docs (added)
| Path | What |
|---|---|
| `tests/chat_history/` | Integration suites for Tasks 1–3 + runner + README |
| `docs/design/task1-*.md`, `task2-*.md`, `task3-*.md` | Detailed per-task design docs |
| `docs/DESIGN-OVERVIEW.md` | Short consolidated design document |
| `docs/SETUP.md` | Setup & execution instructions |
| `docs/TEST-CASES.md` | Documented test cases + results |
| `docs/CHANGES.md` | This file |
