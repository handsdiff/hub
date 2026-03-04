#!/usr/bin/env python3
"""Reduce ws_probe JSONL into reliability summary metrics.

Usage:
  python3 scripts/ws_probe_reduce.py --jsonl /tmp/ws_probe_run.jsonl
  python3 scripts/ws_probe_reduce.py --jsonl /tmp/ws_probe_run.jsonl /tmp/ws_probe_reconn.jsonl --pager-threshold 0.99
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    idx = max(0, int(round(0.95 * (len(vals) - 1))))
    return float(vals[idx])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", nargs="+", required=True, help="One or more ws_probe JSONL files")
    ap.add_argument("--pager-threshold", type=float, default=0.99)
    args = ap.parse_args()

    events = []
    for p in args.jsonl:
        path = Path(p)
        if not path.exists():
            print(json.dumps({"ok": False, "error": f"missing file: {p}"}))
            return 2
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                # ignore non-JSON lines
                pass

    if not events:
        print(json.dumps({"ok": False, "error": "no JSON events parsed"}))
        return 3

    auth_open_to_ack = [e.get("open_to_auth_ack_ms") for e in events if e.get("event") == "auth_ack" and isinstance(e.get("open_to_auth_ack_ms"), (int, float))]
    connect_to_open = [e.get("connect_to_open_ms") for e in events if e.get("event") == "auth_ack" and isinstance(e.get("connect_to_open_ms"), (int, float))]
    first_push_delay = [e.get("first_push_delay_ms") for e in events if e.get("event") == "done" and isinstance(e.get("first_push_delay_ms"), (int, float))]
    message_latency = [e.get("latency_ms") for e in events if e.get("event") == "message" and isinstance(e.get("latency_ms"), (int, float))]

    reconnect_attempts = sum(1 for e in events if e.get("event") == "reconnect_attempt")
    reconnect_successes = sum(
        1
        for e in events
        if e.get("event") == "auth_ack" and int(e.get("session", 1)) >= 2 and bool(e.get("ok"))
    )

    reconnect_success_rate = None
    if reconnect_attempts > 0:
        reconnect_success_rate = reconnect_successes / reconnect_attempts

    summary = {
        "ok": True,
        "files": args.jsonl,
        "counts": {
            "events": len(events),
            "auth_ack": sum(1 for e in events if e.get("event") == "auth_ack"),
            "message": sum(1 for e in events if e.get("event") == "message"),
            "recv_error": sum(1 for e in events if e.get("event") == "recv_error"),
            "reconnect_attempt": reconnect_attempts,
            "done": sum(1 for e in events if e.get("event") == "done"),
        },
        "metrics_ms": {
            "auth_open_to_ack_p50": median(auth_open_to_ack) if auth_open_to_ack else None,
            "auth_open_to_ack_p95": p95(auth_open_to_ack),
            "connect_to_open_p50": median(connect_to_open) if connect_to_open else None,
            "connect_to_open_p95": p95(connect_to_open),
            "first_push_delay_p50": median(first_push_delay) if first_push_delay else None,
            "first_push_delay_p95": p95(first_push_delay),
            "message_latency_p50": median(message_latency) if message_latency else None,
            "message_latency_p95": p95(message_latency),
        },
        "reconnect": {
            "attempts": reconnect_attempts,
            "successes": reconnect_successes,
            "success_rate": reconnect_success_rate,
            "pager_threshold": args.pager_threshold,
            "page": (reconnect_success_rate is not None and reconnect_success_rate < args.pager_threshold),
        },
    }

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
