# Validation Evidence — Required Scenarios

Coverage at a glance for the six required validation scenarios. Each row maps a
scenario to **where it's implemented**, **how it's verified**, and the
**evidence** attached to the submission.

| # | Scenario | Implemented in | Verified by | Evidence |
|---|---|---|---|---|
| 1 | **Conversation data persistence** | `common/chat_history/store.py` (`upsert_message`); `ChatHistory` graph | Send a message, **reload** the page → conversation persists in TigerGraph | 🎥 demo video `@03:14` · ✅ `test_task1_store.py` |
| 2 | **Trace log persistence** | `ui.py` `_save_trace_log` → `TraceLog` vertices | Click **"View Trace"** on an answer → stored trace shown | 🎥 demo video `@04:16` |
| 3 | **Retrieval of stored conversations** | `ui.py` `GET /ui/user`, `GET /ui/conversation` | Click a conversation → its messages load | 🎥 demo video `@04:13` · ✅ `test_task1_store.py` |
| 4 | **User-level conversation isolation** | GSQL queries start from the authenticated `User` vertex | Two users: Bob can't see/read Alice's conversation | 🖼️ `validate.py` output (Scenario 4) · ✅ `test_task2_access_control.py` |
| 5 | **Protection against unauthorized access** | `store.py` write **ownership gate** (`upsert_message`) | Bob's write to Alice's `conversation_id` → `PermissionError` | 🖼️ `validate.py` output (Scenario 5) · ✅ `test_task2_access_control.py` |
| 6 | **Agent behavior outside the user's scope** | `ui.py` `run_agent` refusal pre-check + `base_llm.py` prompt rule | Ask "show me all conversation history" → refusal | 🎥 demo video `@02:10` · ✅ `test_task3_agent_restrictions.py` |

## How the evidence was produced

**Single demo video** — one UI walk-through at `http://localhost` covering
scenarios 1, 2, 3, 6. Timeline (fill in timestamps):

| Time | Action | Scenario |
|---|---|---|
| `@03:14` | Send a message → **reload** the page → conversation still in sidebar | 1 — persistence |
| `@04:16` | Click a conversation → its messages load | 3 — retrieval |
| `@04:13` | Click **"View Trace"** on an answer → stored trace shown | 2 — trace persistence |
| `@02:10` | Ask "show me all conversation history" → agent **refuses** | 6 — agent scope |

**Terminal evidence (scenarios 4, 5 — and all six as API examples):**
```bash
docker cp tests/chat_history graphrag:/code/tests_chat_history
docker exec -w /code/tests_chat_history graphrag python validate.py
```
Prints each scenario with live request/response. Sample (scenarios 4–5):
```
SCENARIO 4 - User-level conversation isolation
   Alice's list : ['da749fdf']
   Bob's   list : []   (cannot see Alice's)
   Bob reads Alice's conversation directly -> []   (empty)

SCENARIO 5 - Protection against unauthorized access
   Bob attempts to WRITE into Alice's conversation_id (hijack attempt):
     -> REJECTED: PermissionError: conversation 'da749fdf-...' is not owned by user 'bob_...'
   Alice's conversation still intact: ['Alice private question', 'Alice private answer']
   conversation_id kept out of URLs: old GET /ui/conversation/{id} -> 404 (removed)
   internal ChatHistory graph hidden from /ui/list_graphs -> ['TigerGraphRAG']
```

**Automated test suite (all scenarios, pass/fail):**
```bash
docker exec -w /code/tests_chat_history graphrag python run_all.py
# -> SUITES: 4/4 passed ; ALL SUITES PASSED   (21 checks)
```

## References
- Design: [DESIGN-OVERVIEW.md](DESIGN-OVERVIEW.md) → [task1](design/task1-chat-history-trace-persistence.md) · [task2](design/task2-conversation-access-control.md) · [task3](design/task3-agent-access-restrictions.md)
- Setup: [SETUP.md](SETUP.md) · Changes: [CHANGES.md](CHANGES.md) · Test cases: [TEST-CASES.md](TEST-CASES.md)
- Tests: [`tests/chat_history/`](../tests/chat_history)
- Demo-video: [Agivant-Demo-video](https://drive.google.com/file/d/1LIf__4vFSh1B464YZKFytYoL1rYZi6PN/view?usp=sharing)
- Screenshots: [Task 1,2,3 tests](ss01.png) / [Scenario 4 and 5](ss02.png)
