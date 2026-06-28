# Setup & Execution Instructions

This covers bringing up the stack, initializing the `ChatHistory` graph, and
verifying Tasks 1–3. The deployment is Docker Compose (TigerGraph + graphrag
API + ECC + UI + nginx).

## 1. Prerequisites
- **Docker Desktop** (tested on Windows 11 / WSL2 backend).
- Git. **Windows note:** this repo uses git **symlinks** for some config/prompt
  paths that don't materialize on a Windows checkout. The stack is configured
  to work without them (the chat agent falls back to a built-in default prompt
  in `common/llm_services/base_llm.py`). See `memory`/`docs/study` notes.
- LLM credentials (the default config targets an OpenAI/Gemini-compatible
  service — set keys in `configs/server_config.json`).

## 2. Configure
Edit `configs/server_config.json` (mounted into the containers):
- **`db_config`** — `hostname: "tigergraph"`, `restppPort: 9000`, `gsPort: 14240`.
  No username/password is required here (per-request auth); the chat-history
  store uses the service account below.
- **`llm_config`** — your completion/embedding/chat service + API keys.
- **`chat_history`** *(added for Task 1)* — service account + graph name:
  ```json
  "chat_history": {
    "graph": "ChatHistory",
    "service_username": "tigergraph",
    "service_password": "tigergraph"
  }
  ```
  (A `configs/server_config.gemini.json` variant is provided for Gemini.)

## 3. Build the images
> **Gotcha (Windows/WSL2):** building the heavy `graphrag-ui` (Node/Vite) image
> while TigerGraph is running can exhaust Docker Desktop's VM and crash the
> engine. **Build the UI with the stack stopped.**

```bash
docker compose build graphrag graphrag-ui     # build with stack down
docker compose up -d                          # start everything
```

## 4. Initialize the ChatHistory graph (one-time)
Install the schema, then the queries (order matters):

```bash
docker cp common/gsql/chat_history/chat_history_schema.gsql  tigergraph:/tmp/
docker cp common/gsql/chat_history/chat_history_queries.gsql tigergraph:/tmp/
docker exec tigergraph gsql /tmp/chat_history_schema.gsql
docker exec tigergraph gsql /tmp/chat_history_queries.gsql
```

This creates the `ChatHistory` graph (`User`, `Conversation`, `Message`,
`TraceLog` vertices + edges) and installs the access-scoped queries
(`get_user_conversations`, `get_recent_messages`, …).

*(Optional)* migrate legacy data from the retired Go service:
```bash
docker exec -e CHATS_DB=/path/chats.db -e TRACE_DIR=/path/trace_logs \
  graphrag python -m common.chat_history.migrate
```

## 5. Run & access
- Wait for TigerGraph to come online (GPE/GSE/RESTPP). The graphrag service is
  healthy when `GET http://localhost:8000/health` returns `200`.
- Open the **UI** at **`http://localhost`** (nginx) and log in as
  `tigergraph` / `tigergraph`.

Quick smoke test:
```bash
curl -s http://localhost:8000/health
# chat (conversation_id rides in a header, not the URL — Task 2 hardening)
curl -s "http://localhost:8000/ui/TigerGraphRAG/query?q=what%20is%20gsql" \
     -H "Authorization: Basic $(printf 'tigergraph:tigergraph'|base64)" \
     -H "X-Conversation-Id: new"
```

## 6. Run the tests
```bash
docker cp tests/chat_history graphrag:/code/tests_chat_history
docker exec -w /code/tests_chat_history graphrag python run_all.py
# -> SUITES: 4/4 passed
```
See [`docs/TEST-CASES.md`](TEST-CASES.md) for the documented cases.

## 7. Rebuild after code changes
- **Backend** (`graphrag/`, `common/`): `docker compose build graphrag && docker compose up -d graphrag && docker compose restart nginx`
- **Frontend** (`graphrag-ui/`): stop the stack first (see the gotcha), then
  `docker compose build graphrag-ui && docker compose up -d`. **Hard-refresh**
  the browser to drop the cached bundle.

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `open //./pipe/dockerDesktopLinuxEngine … cannot find the file` | Docker engine crashed (heavy UI build). Restart Docker Desktop; data persists on the `tigergraph` volume. Rebuild the UI with the stack stopped. |
| `/health` 200 but `/ui/user/...` 500 | TigerGraph still warming up; wait for GPE/GSE/RESTPP to come online. |
| Conversation loads 404 in the UI | Stale cached frontend bundle — hard-refresh after a UI rebuild. |
| `ChatHistory` shows in the graph dropdown | Old session cache — log out/in (the list is filtered server-side). |
