"""Task 2 - Leakage hardening & internal-graph hiding (HTTP API layer).

Covers: conversation_id travels in a header (not the URL), the old URL path
is gone, the internal ChatHistory graph is hidden from the graph list, and
it's rejected as a chat target.
"""
import requests

from helpers import BASE, Reporter, api_get, auth_headers


def run():
    r = Reporter("Task 2 - Leakage hardening & internal graph hidden")

    # grab a real conversation id from the authenticated user's list
    convos = api_get("/ui/user/tigergraph").json()
    cid = convos[0]["conversation_id"] if convos else None

    if cid:
        resp = api_get("/ui/conversation", headers=auth_headers({"X-Conversation-Id": cid}))
        r.check("GET /ui/conversation works via X-Conversation-Id header (not URL)",
                resp.status_code == 200 and isinstance(resp.json(), list),
                f"status={resp.status_code}")

        old = requests.get(BASE + f"/ui/conversation/{cid}", headers=auth_headers(), timeout=15)
        r.check("old /ui/conversation/{id} URL path is removed (id can't leak in logs)",
                old.status_code == 404, f"status={old.status_code}")
    else:
        r.check("a conversation exists to test header read", False, "no conversations found")

    graphs = api_get("/ui/list_graphs").json().get("graphs", [])
    r.check("ChatHistory is hidden from /ui/list_graphs", "ChatHistory" not in graphs, f"graphs={graphs}")

    ch = requests.get(BASE + "/ui/ChatHistory/query", params={"q": "hi"},
                      headers=auth_headers({"X-Conversation-Id": "new"}), timeout=20)
    r.check("/ui/ChatHistory/query is rejected (internal graph not chattable)",
            ch.status_code == 404, f"status={ch.status_code}")

    return r.summary()


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
