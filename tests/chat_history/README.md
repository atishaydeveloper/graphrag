# Chat-history / access-control integration tests

These are **integration tests** for Tasks 1–3 (chat-history persistence,
conversation access control, agent access restrictions). They run against the
**live stack** (TigerGraph + the graphrag service) — not mocks — so they must
be executed from inside the running `graphrag` container, where both the store
layer (`common.chat_history`) and the HTTP API (`http://localhost:8000`) are
reachable.

## Prerequisites
- The stack is up (`docker compose up -d`) and TigerGraph is ready.
- The `ChatHistory` graph is initialized (see `docs/SETUP.md`).

## Run

```bash
# copy the suite into the running container, then run it
docker cp tests/chat_history graphrag:/code/tests_chat_history
docker exec -w /code/tests_chat_history graphrag python run_all.py
```

Run a single suite:

```bash
docker exec -w /code/tests_chat_history graphrag python test_task2_access_control.py
```

Exit code is `0` if all checks pass, non-zero otherwise. Each suite prints
`[PASS]`/`[FAIL]` per check and a per-suite tally.

## What each suite covers
| Suite | Task | Focus |
|---|---|---|
| `test_task1_store.py` | 1 | persistence, seq ordering, graphname filter-then-limit, delete |
| `test_task2_access_control.py` | 2 | read isolation, write ownership gate (hijack/feedback), delete scoping |
| `test_task2_leakage_graph_hiding.py` | 2 | conversation_id in header not URL, ChatHistory hidden + chat-guard |
| `test_task3_agent_restrictions.py` | 3 | agent refuses history/cross-user probes, still answers legit questions |

The suites create uniquely-named throwaway users/conversations and delete them
on the way out, so they don't pollute real data. A documented walk-through of
the same cases (with expected/actual results) is in `docs/TEST-CASES.md`.

## Config (env overrides)
| Var | Default | Meaning |
|---|---|---|
| `GRAPHRAG_BASE` | `http://localhost:8000` | API base URL |
| `TG_USER` / `TG_PASS` | `tigergraph` / `tigergraph` | login creds |
| `TEST_GRAPH` | `TigerGraphRAG` | KG used for agent tests |
