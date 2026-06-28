"""Task 1 - Chat history persistence in TigerGraph.

Covers: conversation/message persistence, monotonic seq ordering, the
per-message graphname filter (filter-then-limit), and delete.
"""
import uuid

from helpers import Reporter, get_store, new_message, uid


def run():
    r = Reporter("Task 1 - Chat history store")
    store = get_store()
    user = uid("t1_user")
    conv = str(uuid.uuid4())

    # --- persistence + ordering ---
    store.upsert_message(new_message(conv, "user", "first question"), user, graphname="TigerGraphRAG")
    store.upsert_message(new_message(conv, "system", "first answer"), user, graphname="TigerGraphRAG")

    convos = store.get_user_conversations(user)
    r.check("conversation persisted and listed for its owner",
            any(c.get("conversation_id") == conv for c in convos), f"{len(convos)} conversation(s)")

    msgs = store.get_conversation_messages(user, conv, 100, "")
    r.check("messages stored with monotonic seq 0,1",
            [m.get("seq") for m in msgs] == [0, 1], str([m.get("seq") for m in msgs]))
    r.check("message content round-trips intact",
            [m["content"] for m in msgs] == ["first question", "first answer"])

    # --- graphname filter-then-limit ---
    # 5 turns alternating graphs: A,B,A,B,A  -> GraphA msgs = seq 0,1,4,5,8,9
    conv2 = str(uuid.uuid4())
    for g in ["GraphA", "GraphB", "GraphA", "GraphB", "GraphA"]:
        store.upsert_message(new_message(conv2, "user", "q"), user, graphname=g)
        store.upsert_message(new_message(conv2, "system", "a"), user, graphname=g)

    a4 = store.get_conversation_messages(user, conv2, 4, "GraphA")
    r.check("filter-then-limit: top_k=4 graphname=GraphA returns 4 GraphA msgs (skips B, reaches back)",
            len(a4) == 4 and all(m["graphname"] == "GraphA" for m in a4),
            f"seqs={[m.get('seq') for m in a4]}")

    nofilter = store.get_conversation_messages(user, conv2, 4, "")
    r.check("no-filter top_k=4 returns the latest 4 overall",
            len(nofilter) == 4 and [m.get("seq") for m in nofilter] == [6, 7, 8, 9],
            f"seqs={[m.get('seq') for m in nofilter]}")

    # --- delete ---
    store.delete_conversation(user, conv)
    store.delete_conversation(user, conv2)
    gone = store.get_user_conversations(user)
    r.check("delete_conversation removes the conversation from the owner",
            not any(c.get("conversation_id") in (conv, conv2) for c in gone))

    return r.summary()


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
