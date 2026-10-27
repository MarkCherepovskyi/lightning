#!/usr/bin/env python3
"""Plugin: wait before letting an incoming HTLC be processed (test helper)."""
import json
import sys
import time

DELAY = 40


def send(o):
    sys.stdout.write(json.dumps(o) + "\n\n")
    sys.stdout.flush()


buf = ""
for line in sys.stdin:
    buf += line
    if line.strip() == "" and buf.strip():
        req = json.loads(buf)
        buf = ""
        m = req.get("method")
        if m == "getmanifest":
            send({"jsonrpc": "2.0", "id": req["id"],
                  "result": {"dynamic": True, "options": [], "rpcmethods": [],
                             "hooks": [{"name": "htlc_accepted"}], "subscriptions": []}})
        elif m == "init":
            send({"jsonrpc": "2.0", "id": req["id"], "result": {}})
        elif m == "htlc_accepted":
            time.sleep(DELAY)
            send({"jsonrpc": "2.0", "id": req["id"], "result": {"result": "continue"}})
