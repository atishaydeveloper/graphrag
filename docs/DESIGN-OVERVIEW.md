# Design Overview — Chat History & Access Control

A short, consolidated design document for the three tasks. Detailed, per-task
designs (with verification) live alongside this file:
- [Task 1 — chat-history persistence](design/task1-chat-history-trace-persistence.md) ([impl report](design/task1-implementation-report.md))
- [Task 2 — conversation access control](design/task2-conversation-access-control.md)
- [Task 3 — agent access restrictions](design/task3-agent-access-restrictions.md)

## 1. Problem & scope
1. **Persist chat history** (conversations, messages, traces) durably.
2. **Access control** — a user only ever sees their own conversations.
3. **Agent restrictions** — the chatbot can't be prompted into exposing others' history.

## 2. Chosen approach
- **History as a graph in TigerGraph.** The repo already runs TigerGraph; rather
  than keep the legacy Go + SQLite sidecar, history lives in a dedicated
  `ChatHistory` graph: `User -HAS_CONVERSATION-> Conversation -HAS_MESSAGE-> Message`
  (+ `NEXT`/`LAST_MESSAGE` pointers and `TraceLog`). One datastore, one backup,
  one auth model. The Go service is retired.
- **Access control in the query/application layer.** Every read query *starts
  from the authenticated user's `User` vertex*; endpoints pass `creds.username`,
  never a client-supplied id. Topology is organization, not authorization.
- **Agent isolation is structural.** The KG agent connects only to the selected
  knowledge graph and has no tooling for `ChatHistory`; conversation history is
  injected as the *current user's own* read-only context.

## 3. Key design decisions
| Decision | Why |
|---|---|
| **Per-message `graphname`** (D12 / "Option B") | A conversation can span KGs; storing the KG per message lets us feed the LLM only the *selected graph's* recent turns (filter-then-limit) without cross-graph bleed |
| **Service-account connection** for `ChatHistory` | The app writes history as one identity, so regular (non-superuser) users don't each need a grant on the internal graph. Isolation is unaffected — it's enforced by the `user_id` passed to queries |
| **Write-side ownership gate** | Read isolation alone left a hole: lazily creating a conversation let user B claim user A's `conversation_id` and read it. `upsert_message` now refuses a foreign existing `conversation_id` |
| **`conversation_id` in a header** | A UUID isn't a secret — in the URL it leaked into access logs / history / `Referer`. Moving it to `X-Conversation-Id` keeps it out of logs (defense-in-depth on top of the ownership gate) |
| **Hide + guard the internal graph** | `ChatHistory` is filtered from `list_graphs` and rejected as a chat target, so it's never a user-facing KG |
| **Deterministic agent pre-check + prompt rule** | A keyword pre-check in `run_agent()` refuses history/cross-user probes before the agent runs (un-jailbreakable); a prompt-scope rule covers phrasings the keywords miss |

## 4. Assumptions
- The demo logs in as the TigerGraph **superuser** (`tigergraph`); it doubles as
  the service account. A production deployment would use a dedicated low-priv
  service account and provision per-user TG identities.
- **TigerGraph is the identity source** — there's no separate user store; the
  authenticated TG username *is* the access-control principal.
- Single-instance Docker Compose deployment; conversations are created with
  their first message (so a conversation always has ≥1 message).

## 5. Tradeoffs / known limits
- **`seq` race:** per-turn writes aren't a single atomic transaction, so two
  concurrent writes to the *same* conversation could contend on `seq`. Acceptable
  for interactive chat; a future batched/transactional write would close it.
- **Keyword heuristic** for the agent pre-check catches the obvious asks; the
  prompt rule + the structural impossibility of access cover the rest. An LLM
  intent-classifier would be stronger but adds latency.
- **Prompt files absent on Windows checkout** (git-symlink breakage), so the
  Task 3 scope rule was added to the *built-in default* prompt; on a Linux deploy
  with real prompt files the same rule goes in `chatbot_response.txt`.
- **Service account == superuser in the demo** — correct behaviour, but a real
  deployment should scope it to just the `ChatHistory` graph.

## 6. Layered isolation (how it all composes)
Authentication (`ui_basic_auth`) → read isolation (queries from the `User`
vertex) → write ownership gate → leakage hardening (header) → internal graph
hidden + guarded → agent refusal. See the per-task docs for the threat tables
and live verification, and [TEST-CASES.md](TEST-CASES.md) for the 21 automated
checks + UI walk-throughs.
