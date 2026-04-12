#!/usr/bin/env bash
# Hub Health Watchdog
# Checks Hub health every 60 seconds, restarts if down
# Run as: nohup bash hub_watchdog.sh &

set -u

LOG="/tmp/hub-watchdog.log"
MAX_FAILURES=3
failures=0

HUB_DIR="/home/openclaw/.openclaw/workspace/hub"
HUB_DATA_DIR="/home/openclaw/.openclaw/workspace/hub-data"
WORKSPACE_DIR="/home/openclaw/.openclaw/workspace"
OPENCLAW_CONFIG="/home/openclaw/.openclaw/openclaw.json"
GUNICORN_BIN="/home/openclaw/.local/bin/gunicorn"
GUNICORN_ARGS=(-k gevent -w 1 -b 127.0.0.1:8080 --timeout 120 server:app)
WS_PROBE_AGENT="brain"

timestamp() {
    date -u '+%Y-%m-%d %H:%M:%S UTC'
}

log_line() {
    echo "[$(timestamp)] $1" >> "$LOG"
}

probe_http_health() {
    curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:8080/health 2>/dev/null || echo "000"
}

probe_worker_topology() {
    # Canonical topology for current Hub: gevent + 1 worker.
    # This should produce one master process cmdline with "-w 1".
    local master
    master="$(pgrep -af 'gunicorn.*server:app' | head -n 1 || true)"
    if [ -z "$master" ]; then
        echo "missing_master"
        return 1
    fi
    if ! printf '%s' "$master" | grep -q -- ' -w 1 '; then
        echo "noncanonical_workers"
        return 1
    fi
    echo "ok"
    return 0
}

probe_ws_auth() {
    # Direct machine-usable websocket probe: connect local WS endpoint and require auth ack.
    local output
    output="$(
        WS_PROBE_AGENT="$WS_PROBE_AGENT" \
        python3 - <<'PY'
import json
import os
from pathlib import Path

agent_id = os.environ.get("WS_PROBE_AGENT", "brain")
url = f"ws://127.0.0.1:8080/agents/{agent_id}/ws"

secret = ""
agent_paths = [
    Path("/home/openclaw/.openclaw/workspace/hub-data/agents.json"),
    Path.home() / ".openclaw/workspace/hub-data/agents.json",
]
for p in agent_paths:
    if not p.exists():
        continue
    try:
        data = json.loads(p.read_text())
        candidate = data.get(agent_id, {}).get("secret", "")
        if candidate:
            secret = candidate
            break
    except Exception:
        pass

if not secret:
    cfg_paths = [
        Path("/home/openclaw/.openclaw/openclaw.json"),
        Path.home() / ".openclaw/openclaw.json",
    ]
    for p in cfg_paths:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
            candidate = (
                data.get("channels", {})
                .get("hub", {})
                .get("accounts", {})
                .get("default", {})
                .get("secret", "")
            )
            if candidate:
                secret = candidate
                break
        except Exception:
            pass

if not secret:
    print("missing_secret")
    raise SystemExit(2)

try:
    import websocket
except Exception as e:
    print(f"missing_websocket_client:{e}")
    raise SystemExit(3)

ws = None
try:
    ws = websocket.create_connection(url, timeout=6)
    ws.send(json.dumps({"secret": secret, "probe": True}))
    raw = ws.recv()
    ack = json.loads(raw)
    if ack.get("ok") is True and ack.get("type") == "auth":
        print("ok")
        raise SystemExit(0)
    print(f"bad_ack:{str(raw)[:120]}")
    raise SystemExit(1)
except Exception as e:
    print(f"ws_error:{type(e).__name__}:{e}")
    raise SystemExit(1)
finally:
    try:
        if ws is not None:
            ws.close()
    except Exception:
        pass
PY
    )"
    if [ "$output" = "ok" ]; then
        echo "ok"
        return 0
    fi
    echo "$output"
    return 1
}

restart_hub() {
    log_line "Hub DOWN/DEGRADED — restarting gunicorn with canonical topology (gevent, 1 worker)."

    pkill -f 'gunicorn.*server:app' 2>/dev/null || true
    sleep 2
    pkill -9 -f 'gunicorn.*server:app' 2>/dev/null || true
    sleep 1

    # Wait for port 8080 to be free (up to 15s)
    for i in $(seq 1 15); do
        if ! ss -tlnp 2>/dev/null | grep -q ':8080 '; then
            break
        fi
        if [ "$i" -eq 15 ]; then
            log_line "Port 8080 still occupied after 15s — killing holder: $(ss -tlnp 2>/dev/null | grep ':8080 ')"
            fuser -k 8080/tcp 2>/dev/null || true
            sleep 2
        fi
        sleep 1
    done

    cd "$HUB_DIR" || return 1
    export HUB_DATA_DIR="$HUB_DATA_DIR"
    export WORKSPACE_DIR="$WORKSPACE_DIR"
    export OPENCLAW_CONFIG="$OPENCLAW_CONFIG"
    export PATH="/home/openclaw/.local/bin:$PATH"

    # Keep runtime deps in sync with bootstrap expectations.
    pip3 install --break-system-packages -q gunicorn gevent flask flask-sock requests websocket-client >/dev/null 2>&1 || true

    nohup "$GUNICORN_BIN" "${GUNICORN_ARGS[@]}" >> /tmp/hub-gunicorn.log 2>&1 &
    log_line "Gunicorn started (PID $!), verifying..."
    sleep 5

    # Verify gunicorn actually started and is serving
    local verify_code
    verify_code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:8080/health 2>/dev/null || echo "000")"
    if [ "$verify_code" = "200" ]; then
        log_line "Restart verified — health returned 200."
    else
        log_line "Restart FAILED — health returned $verify_code. Port status: $(ss -tlnp 2>/dev/null | grep ':8080 ' || echo 'not listening')"
    fi
}

while true; do
    http_code="$(probe_http_health)"
    ws_status="$(probe_ws_auth)"
    topology_status="$(probe_worker_topology || true)"
    ws_probe_unavailable=0
    if [[ "$ws_status" == missing_secret ]] || [[ "$ws_status" == missing_websocket_client:* ]]; then
        ws_probe_unavailable=1
    fi
    # WS timeouts are non-fatal — gevent worker can be busy handling requests
    # Only restart if HTTP is also failing
    if [[ "$ws_status" == ws_error:WebSocketTimeoutException:* ]]; then
        ws_probe_unavailable=1
    fi

    if [ "$http_code" = "200" ] && [ "$topology_status" = "ok" ] && { [ "$ws_status" = "ok" ] || [ "$ws_probe_unavailable" -eq 1 ]; }; then
        failures=0
        if [ "$ws_probe_unavailable" -eq 1 ]; then
            log_line "Hub health degraded: websocket probe unavailable (ws=$ws_status). HTTP/topology healthy."
        fi
    else
        failures=$((failures + 1))
        log_line "Hub health check FAILED (http=$http_code ws=$ws_status topology=$topology_status, failure $failures/$MAX_FAILURES)"

        if [ "$failures" -ge "$MAX_FAILURES" ]; then
            restart_hub || log_line "Restart attempt failed before process launch."
            failures=0
        fi
    fi

    sleep 60
done
