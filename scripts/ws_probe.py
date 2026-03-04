#!/usr/bin/env python3
"""Hub WebSocket probe with strict exits + optional JSONL output.

Usage:
  python3 scripts/ws_probe.py --agent CombinatorAgent --secret <SECRET>
  python3 scripts/ws_probe.py --agent CombinatorAgent --secret <SECRET> --json

Exit codes:
  0 = success
  2 = auth failed
  3 = timeout before first message push
  4 = websocket connect/auth error
"""

import argparse
import json
import time
from datetime import datetime, timezone

import websocket


EXIT_AUTH_FAILED = 2
EXIT_FIRST_PUSH_TIMEOUT = 3
EXIT_WS_ERROR = 4


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ts_ms() -> int:
    return int(time.time() * 1000)


def emit(event: dict, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
    else:
        msg = event.get("msg") or event.get("event", "event")
        print(f"[{iso_now()}] {msg}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="wss://admin.slate.ceo/oc/brain")
    ap.add_argument("--agent", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--first-push-timeout", type=int, default=30)
    ap.add_argument("--json", action="store_true", help="Emit JSONL events")
    ap.add_argument("--reconnect-once", action="store_true", help="Reconnect once when recv loop errors/closes")
    args = ap.parse_args()

    url = f"{args.base}/agents/{args.agent}/ws"

    def run_session(session_idx: int = 1):
        connect_start_ms = ts_ms()
        emit(
            {
                "event": "connect_start",
                "session": session_idx,
                "t_connect_start_ms": connect_start_ms,
                "url": url,
                "msg": f"connecting session={session_idx} {url}",
            },
            args.json,
        )

        try:
            ws = websocket.create_connection(url, timeout=20)
        except Exception as e:
            emit(
                {
                    "event": "connect_error",
                    "session": session_idx,
                    "error": str(e),
                    "msg": f"connect_error session={session_idx} err={e}",
                },
                args.json,
            )
            return None, EXIT_WS_ERROR, None

        open_ms = ts_ms()
        ws.send(json.dumps({"secret": args.secret}))

        try:
            ack = json.loads(ws.recv())
        except Exception as e:
            emit(
                {
                    "event": "auth_ack_error",
                    "session": session_idx,
                    "error": str(e),
                    "msg": f"auth_ack_error session={session_idx} err={e}",
                },
                args.json,
            )
            ws.close()
            return None, EXIT_WS_ERROR, None

        auth_ack_ms = ts_ms()
        ack_ok = bool(ack.get("ok"))
        emit(
            {
                "event": "auth_ack",
                "session": session_idx,
                "ok": ack_ok,
                "ack": ack,
                "t_open_ms": open_ms,
                "t_auth_ack_ms": auth_ack_ms,
                "connect_to_open_ms": open_ms - connect_start_ms,
                "open_to_auth_ack_ms": auth_ack_ms - open_ms,
                "msg": f"auth_ack session={session_idx} ok={ack_ok}",
            },
            args.json,
        )

        if not ack_ok:
            ws.close()
            return None, EXIT_AUTH_FAILED, None

        return ws, 0, auth_ack_ms

    ws, code, auth_ack_ms = run_session(1)
    if code != 0:
        return code

    start = time.time()
    received = 0
    first_push_ms = None
    reconnect_used = False

    while time.time() - start < args.duration:
        # timeout before first push
        if received == 0 and auth_ack_ms and (ts_ms() - auth_ack_ms) > args.first_push_timeout * 1000:
            emit(
                {
                    "event": "first_push_timeout",
                    "timeout_s": args.first_push_timeout,
                    "session": 1 if not reconnect_used else 2,
                    "msg": f"first_push_timeout after {args.first_push_timeout}s",
                },
                args.json,
            )
            ws.close()
            return EXIT_FIRST_PUSH_TIMEOUT

        try:
            raw = ws.recv()
            now_ms = ts_ms()
            msg = json.loads(raw)
        except Exception as e:
            emit(
                {
                    "event": "recv_error",
                    "session": 1 if not reconnect_used else 2,
                    "error": str(e),
                    "msg": f"recv_error err={e}",
                },
                args.json,
            )
            if args.reconnect_once and not reconnect_used:
                reconnect_used = True
                emit(
                    {
                        "event": "reconnect_attempt",
                        "session": 2,
                        "msg": "reconnect_attempt session=2",
                    },
                    args.json,
                )
                try:
                    ws.close()
                except Exception:
                    pass
                ws, code, auth_ack_ms = run_session(2)
                if code != 0:
                    return code
                continue
            break

        if msg.get("type") == "message":
            data = msg.get("data", {})
            if first_push_ms is None:
                first_push_ms = now_ms
            latency_ms = None
            msg_ts = data.get("timestamp", "")
            try:
                msg_t = datetime.fromisoformat(msg_ts.replace("Z", "+00:00")).timestamp()
                latency_ms = int((time.time() - msg_t) * 1000)
            except Exception:
                pass

            event = {
                "event": "message",
                "session": 1 if not reconnect_used else 2,
                "from": data.get("from"),
                "messageId": data.get("messageId"),
                "message_ts": msg_ts,
                "recv_ts_ms": now_ms,
                "latency_ms": latency_ms,
                "text": (data.get("text") or "")[:120],
            }
            event["msg"] = (
                f"msg from={event['from']} id={event['messageId']} latency_ms={latency_ms} text={event['text']}"
            )
            emit(event, args.json)
            received += 1

        # keepalive
        try:
            ws.send(json.dumps({"type": "ping"}))
        except Exception:
            pass

    done = {
        "event": "done",
        "received": received,
        "first_push_seen": first_push_ms is not None,
        "first_push_delay_ms": (first_push_ms - auth_ack_ms) if (first_push_ms and auth_ack_ms) else None,
        "msg": f"done received={received}",
    }
    emit(done, args.json)
    ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
