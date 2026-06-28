"""Task 2 - Conversation access control (store layer).

Covers: read isolation (a user can't read another's conversation), the
write-side ownership gate (can't inject into / hijack a foreign
conversation_id), and delete scoping.
"""
import uuid

from helpers import Reporter, get_store, new_message, uid


def run():
    r = Reporter("Task 2 - Conversation access control")
    store = get_store()
    A = uid("t2_A")
    B = uid("t2_B")
    convA = str(uuid.uuid4())

    # A creates a private conversation
    store.upsert_message(new_message(convA, "user", "A secret question"), A, graphname="TigerGraphRAG")
    store.upsert_message(new_message(convA, "system", "A secret answer"), A, graphname="TigerGraphRAG")

    # --- read isolation ---
    r.check("B cannot read A's conversation directly (read isolation)",
            store.get_conversation_messages(B, convA, 100, "") == [])
    r.check("A's conversation is absent from B's conversation list",
            not any(c.get("conversation_id") == convA for c in store.get_user_conversations(B)))

    # --- write ownership gate (the hijack attempt) ---
    blocked = False
    try:
        store.upsert_message(new_message(convA, "user", "B injection"), B, graphname="TigerGraphRAG")
    except PermissionError:
        blocked = True
    r.check("B's write to A's conversation_id is REJECTED (ownership gate)", blocked)

    r.check("B still cannot read A's conversation after the failed injection",
            store.get_conversation_messages(B, convA, 100, "") == [])

    a_msgs = store.get_conversation_messages(A, convA, 100, "")
    r.check("A's conversation is intact (not contaminated by B)",
            [m["content"] for m in a_msgs] == ["A secret question", "A secret answer"],
            f"contents={[m['content'] for m in a_msgs]}")

    # --- feedback tampering gate ---
    # B tries to update feedback on one of A's existing messages
    a_msg_id = a_msgs[0]["message_id"]
    tampered = False
    try:
        fb = new_message(convA, "user", "A secret question")
        fb.message_id = a_msg_id
        fb.feedback = 1
        store.upsert_message(fb, B)
    except PermissionError:
        tampered = True
    r.check("B cannot tamper feedback on A's message (ownership gate)", tampered)

    # --- delete scoping ---
    r.check("B cannot delete A's conversation", store.delete_conversation(B, convA) is False)

    # cleanup
    store.delete_conversation(A, convA)
    return r.summary()


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
