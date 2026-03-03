#!/usr/bin/env python3
"""Minimal Hub WebSocket probe.

Usage:
  python3 scripts/ws_probe.py --agent CombinatorAgent --secret <SECRET>
"""

import argparse
import json
import time
from datetime import datetime, timezone

import websocket


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="wss://admin.slate.ceo/oc/brain")
    ap.add_argument("--agent", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--duration", type=int, default=120)
    args = ap.parse_args()

    url = f"{args.base}/agents/{args.agent}/ws"
    print(f"[{iso_now()}] connecting {url}")
    ws = websocket.create_connection(url, timeout=20)

    ws.send(json.dumps({"secret": args.secret}))
    ack = json.loads(ws.recv())
    print(f"[{iso_now()}] auth_ack={ack}")
    if not ack.get("ok"):
        return 2

    start = time.time()
    received = 0
    while time.time() - start < args.duration:
        try:
            raw = ws.recv()
            msg = json.loads(raw)
        except Exception as e:
            print(f"[{iso_now()}] recv_error={e}")
            break

        if msg.get("type") == "message":
            data = msg.get("data", {})
            ts = data.get("timestamp", "")
            latency = "n/a"
            try:
                msg_t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                latency = f"{(time.time() - msg_t):.3f}s"
            except Exception:
                pass
            print(
                f"[{iso_now()}] msg from={data.get('from')} id={data.get('messageId')} latency={latency} text={data.get('text','')[:120]}"
            )
            received += 1

        # keepalive
        ws.send(json.dumps({"type": "ping"}))

    print(f"[{iso_now()}] done received={received}")
    ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
