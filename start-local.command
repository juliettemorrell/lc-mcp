#!/usr/bin/env bash
# Double-click this file in Finder (or run from Terminal) to start the
# Longevity Copilot MCP locally and expose it via a public Cloudflare tunnel.
#
# What it does:
#   1) Installs python deps into a local virtualenv (no system python pollution)
#   2) Starts the MCP server on 127.0.0.1:8090
#   3) Downloads cloudflared if missing
#   4) Opens a public HTTPS tunnel and prints the URL
#   5) Waits. Ctrl-C to stop everything cleanly.

set -e
cd "$(dirname "$0")"

PORT=8090
VENV=".venv"

echo "==> 1. Setting up Python virtualenv at $VENV"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r requirements.txt

echo "==> 2. Starting MCP server on http://127.0.0.1:$PORT"
"$VENV/bin/uvicorn" server:app --host 127.0.0.1 --port $PORT > /tmp/lc-mcp.log 2>&1 &
MCP_PID=$!
trap 'echo ""; echo "Stopping..."; kill $MCP_PID 2>/dev/null; kill $CF_PID 2>/dev/null; exit 0' INT TERM

# Wait for health
for i in $(seq 1 15); do
  sleep 1
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "    MCP up after ${i}s"
    break
  fi
done

if ! curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
  echo "ERROR: MCP failed to start. Log tail:"
  tail -20 /tmp/lc-mcp.log
  exit 1
fi

echo "==> 3. Installing cloudflared if needed"
if ! command -v cloudflared >/dev/null 2>&1; then
  if [[ "$(uname -m)" == "arm64" ]]; then
    URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz"
  else
    URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
  fi
  curl -sSL "$URL" -o /tmp/cf.tgz
  tar -xzf /tmp/cf.tgz -C /tmp/
  CF="/tmp/cloudflared"
else
  CF="$(command -v cloudflared)"
fi
echo "    Using $CF"

echo "==> 4. Opening public tunnel to MCP"
"$CF" tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate > /tmp/lc-cf.log 2>&1 &
CF_PID=$!

# Wait for the trycloudflare URL to appear
TUNNEL_URL=""
for i in $(seq 1 30); do
  sleep 1
  TUNNEL_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/lc-cf.log 2>/dev/null | head -1)
  if [ -n "$TUNNEL_URL" ]; then break; fi
done

if [ -z "$TUNNEL_URL" ]; then
  echo "ERROR: cloudflared did not produce a URL. Log tail:"
  tail -30 /tmp/lc-cf.log
  kill $MCP_PID 2>/dev/null
  exit 1
fi

# Verify the tunnel works
sleep 4
HEALTH=$(curl -fsS "$TUNNEL_URL/healthz" 2>/dev/null || echo "FAILED")

echo ""
echo "================================================================"
echo "  Longevity Copilot MCP is LIVE"
echo "================================================================"
echo "  Local URL:   http://127.0.0.1:$PORT"
echo "  Public URL:  $TUNNEL_URL"
echo ""
echo "  Endpoint to paste into Po:"
echo "  $TUNNEL_URL/mcp"
echo ""
echo "  Catalog (try every tool in the browser):"
echo "  $TUNNEL_URL/catalog"
echo ""
echo "  Health probe response: ${HEALTH:0:120}"
echo "================================================================"
echo ""
echo "Tail of MCP log:   tail -f /tmp/lc-mcp.log"
echo "Tail of cf log:    tail -f /tmp/lc-cf.log"
echo ""
echo "Ctrl-C to stop everything."

# Wait forever (until Ctrl-C)
wait
