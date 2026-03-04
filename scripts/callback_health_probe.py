#!/usr/bin/env python3
"""Probe callback URLs from /hub/analytics and report health.

Usage:
  python3 scripts/callback_health_probe.py
  python3 scripts/callback_health_probe.py --base https://admin.slate.ceo/oc/brain --timeout 8
"""

from __future__ import annotations

import argparse
import json
import urllib.request
import urllib.error


def fetch_json(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": "hub-callback-probe/0.1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_probe(url: str, method: str, timeout: int):
    req = urllib.request.Request(url=url, method=method)
    req.add_header("User-Agent", "hub-callback-probe/0.1")
    req.add_header("Accept", "application/json")
    if method == "POST":
        payload = json.dumps({"probe": "callback-health", "source": "brain", "ts": "now"}).encode()
        req.data = payload
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(200).decode(errors="ignore")
            return {"ok": True, "status": r.status, "body_preview": body[:120]}
    except urllib.error.HTTPError as e:
        body = e.read(200).decode(errors="ignore") if hasattr(e, "read") else ""
        return {"ok": False, "status": e.code, "body_preview": body[:120]}
    except Exception as e:
        return {"ok": False, "status": None, "error": f"{type(e).__name__}: {e}"[:160]}


def classify(get_res, post_res):
    ps = post_res.get("status")
    gs = get_res.get("status")
    if isinstance(ps, int) and 200 <= ps < 400:
        return "healthy_post"
    if ps == 404:
        return "endpoint_path_invalid"
    if ps in (401, 403):
        return "auth_required_or_forbidden"
    if ps in (405,):
        return "method_not_allowed"
    if ps in (500, 502, 503, 504):
        return "server_error"
    if gs == 404 and ps is None:
        return "endpoint_unreachable_or_invalid"
    return "unknown_or_unhealthy"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://admin.slate.ceo/oc/brain")
    ap.add_argument("--timeout", type=int, default=8)
    args = ap.parse_args()

    analytics = fetch_json(f"{args.base}/hub/analytics", timeout=args.timeout)
    rows = analytics.get("delivery_status", [])

    out = []
    for r in rows:
        if not r.get("has_callback"):
            continue
        aid = r.get("agent_id")
        cb = r.get("callback_url")
        if not cb:
            continue
        g = http_probe(cb, "GET", args.timeout)
        p = http_probe(cb, "POST", args.timeout)
        out.append(
            {
                "agent_id": aid,
                "callback_url": cb,
                "get": g,
                "post": p,
                "classification": classify(g, p),
            }
        )

    out.sort(key=lambda x: (x["classification"], x["agent_id"]))
    print(json.dumps({"count": len(out), "rows": out}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
