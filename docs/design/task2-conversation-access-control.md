# Task 2 — Conversation Access Control

**Objective:** ensure conversation history is visible only to the user who owns it; prevent any user from reading, modifying, or hijacking another user's conversation data.

**Status:** ✅ Implemented and verified (read + write paths), building on the Task 1 isolation model plus a write-side ownership gate added for this task.

---

## 1. Identity & the access-control model

Access control rests on three layers:

1. **Authentication.** Every chat/history endpoint requires HTTP Basic Auth, validated against TigerGraph. The authenticated identity is `creds.username` — the user's TigerGraph username. There is no separate user store; TG is the identity source of truth.
2. **Read isolation (Task 1 §6.1).** Every read query *starts from the authenticated user's `User` vertex* and traverses outward (`User(uid) -HAS_CONVERSATION-> Conversation -HAS_MESSAGE-> Message`). A user can only ever reach data hanging off **their own** `User` vertex. The endpoints pass `creds.username` into the queries — **never a path/body-supplied user id** — so the identity can't be spoofed by the client.
3. **Write ownership gate (added in Task 2).** A user may only append to / give feedback on a conversation they own, and may not create-into or inject-into a `conversation_id` that already belongs to someone else.

> Important: in a graph DB, all rows live in one queryable space — topology is *organization, not authorization*. So isolation is enforced **in the query/application layer** (always scoping by the authenticated `user_id`), not by the graph being disconnected.

## 2. The example scenario, satisfied

| Action | Result |
|---|---|
| User A authenticates, requests history (`GET /ui/user/A`) | Returns **only A's** conversations (query starts from `User(A)`) |
| User A requests `GET /ui/user/B` | Still returns **A's own** data — the endpoint uses `creds.username`, ignores the path param (no leak) |
| User A opens conversation `convB` (B's id) | **Empty** — the traversal from `User(A)` can't reach B's conversation |
| User A deletes `convB` | **Refused** — `delete_conversation` checks ownership first |
| Trace logs (`GET /ui/trace/{id}`) | **superuser + owner** gate |

## 3. The vulnerability found & fixed (write side)

**Read isolation alone was not enough.** Before this task, the *write* path had a hole:

- The store created a conversation lazily on the first message. If user **B** sent a message carrying user **A**'s `conversation_id`, the store saw "B doesn't own this id ⇒ treat as new" and created a `HAS_CONVERSATION` edge **from B to A's conversation**.
- That edge then let B **read A's entire private conversation** (and inject messages into it) — because reads traverse from the user's `User` vertex, and B now had an edge to A's conversation.

**Demonstrated (before fix):**
```
B writes to A's conversation_id  -> SUCCEEDED
B reads A's conversation         -> ["B's injection", "A's secret question", "A's secret answer"]   # LEAK
```

**Fix — ownership gate in `ChatHistoryStore.upsert_message`:**
- Probe ownership with `_get_tail(user_id, conversation_id)` (most-recent message reachable from *this* user's vertex). `None` ⇒ the user does not own it.
- **New message, `tail is None`:** if a `Conversation` with that id **already exists** (`_conversation_exists`), it belongs to someone else ⇒ **`raise PermissionError`**. Only a genuinely new id is created.
- **Feedback update (message exists):** allowed only if `tail is not None` (user owns the conversation); otherwise **`raise PermissionError`**.

The chat write path (`write_message_to_history`) catches the error and drops the write silently (the attacker gets no edge, no data, no confirmation); the feedback endpoint surfaces it as an error.

**Verified (after fix):**
```
B writes to A's conversation_id  -> REJECTED (PermissionError: conversation ... not owned by 'B')
B reads A's conversation         -> []          # no leak
A's conversation                 -> ["A's secret question", "A's secret answer"]   # intact
Legit user: new convo + continue own convo -> works (4 messages)   # gate doesn't break normal flow
```

## 4. Where it's enforced (code map)

| Concern | Enforcement |
|---|---|
| Auth | `ui_basic_auth` dependency on every endpoint |
| List own conversations | `get_user_conversations` endpoint passes `creds.username`; GSQL `get_user_conversations(user_id)` starts from `User(user_id)` |
| Read a conversation | `get_conversation_contents` passes `creds.username`; GSQL `get_recent_messages(user_id, conversation_id, …)` reachable only via `User(user_id)` |
| Write / append | `ChatHistoryStore.upsert_message` ownership gate (`_get_tail` + `_conversation_exists`) |
| Feedback | same gate (`tail is None ⇒ PermissionError`) |
| Delete | `delete_conversation` checks the conversation is in the user's own list |
| Traces | `get_trace_log` — superuser role + owner (`TraceLog.username`) |

## 5. Threat coverage

| Attack | Outcome |
|---|---|
| Request another user's history directly | Returns caller's own data (path id ignored) |
| Read a known foreign `conversation_id` | Empty (unreachable from caller's `User` vertex) |
| **Inject into / hijack a known foreign `conversation_id`** | **PermissionError — blocked** |
| Tamper feedback on a foreign `message_id` | PermissionError — blocked |
| Delete a foreign conversation | Refused (not in owner's list) |
| Unauthenticated request | 401 (auth dependency) |

## 6. Leakage hardening — conversation_id out of URLs (implemented)

The ownership gate (§3) makes knowing a `conversation_id` useless to an attacker. As defense-in-depth we *also* stopped the id from leaking in the first place. A `conversation_id` is a random UUIDv4 (server-generated, ~122 bits — unguessable), **but it is not a secret**: it travelled in URLs, so it landed in nginx/proxy **access logs**, **browser history**, and the **`Referer`** header.

**Change:** `conversation_id` now travels in an **`X-Conversation-Id` request header**, not in the URL path/query. nginx's access log records the request line (`$request`) and the `Referer`/`User-Agent` headers, but **not** arbitrary headers — so the id no longer appears in logs.

| Endpoint | Before | After |
|---|---|---|
| Read a conversation | `GET /ui/conversation/{id}` | `GET /ui/conversation` + `X-Conversation-Id` header |
| Delete a conversation | `DELETE /ui/conversation/{id}` | `DELETE /ui/conversation` + header |
| REST query (not used by UI) | `GET /ui/{g}/query?conversation_id=` | `…/query` + header |
| **Chat (WebSocket)** | already in the WS message body | unchanged — never leaked |

Frontend (`SideMenu.tsx`) sends the header instead of putting the id in the fetch URL.

**Verified:** header-based `GET /ui/conversation` returns the messages; the old `…/conversation/{id}` URL now **404s**; and the nginx access log shows `"GET /ui/conversation HTTP/1.1"` with the id appearing **nowhere** in the logs.

> Not changed: the question text `q` on the unused REST `/query` endpoint still rides in the query string. The UI's chat path is the WebSocket (body), so user questions don't leak via the UI; the REST `/query` is API-only.

## 6b. Hide the internal ChatHistory graph from the UI

The `ChatHistory` graph is a **backend operational store** (User/Conversation/Message/TraceLog), not a knowledge graph to chat against. But because it's a real TigerGraph graph, a **superuser** (e.g. `tigergraph`) saw it listed in the UI's graph selector alongside `TigerGraphRAG` — inviting a user to "switch to" it.

Why it appeared: the graph list comes from `conn.listGraphs()` in `auth()`, which returns every graph the authenticated user can access. A superuser can access all graphs; a properly-provisioned **regular** user is *not* granted on `ChatHistory` (the store uses the service account — Task 1), so they'd never have seen it. The fix makes it invisible to **everyone**, admins included.

**Fix (two layers):**
1. **Filter it from the list** — `auth()` drops the configured chat-history graph name from `graphs` after `listGraphs()`. Covers login, `/list_graphs`, and chat auth in one place.
2. **Guard the chat entry points** — `GET /{graph}/query` returns **404** and the `/{graph}/chat` WebSocket closes if `graphname` == the chat-history graph, so a hand-crafted request can't run the agent against it either.

**Verified:** `/list_graphs` and login return only `TigerGraphRAG`; `/ui/ChatHistory/query` → 404; `/ui/TigerGraphRAG/query` → 200 (unaffected).

> Note: chatting against `ChatHistory` would not have dumped other users' conversations anyway — GraphRAG retrieval runs over `Document`/`Entity`/embedding vertices, which `ChatHistory` doesn't have. This fix is about correctness and not exposing internal surface, layered on top of that.

## 7. Notes / future hardening
- The `/ui/user/{user_id}` endpoint *ignores* the path param and uses the authenticated user (safe). A stricter variant could return **403** when the path id ≠ authenticated user, for explicit API semantics.
- The write gate adds one ownership probe (`_get_tail`) + one existence check per new conversation — negligible cost, and only on conversation creation / feedback.
- Cross-user isolation is independent of the service-account connection (Task 1): the service account is only the *connection*; authorization is always by the authenticated `user_id` passed into queries.
