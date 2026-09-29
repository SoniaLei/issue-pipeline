#!/usr/bin/env bash
#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Run the API and the worker together from one shell, against one store, with
# an optional public tunnel so GitHub can reach the webhook from a laptop or a
# throwaway VM. This is the "prove it end to end" arrangement, not hosting:
# when the shell goes, so does the endpoint.
#
#   scripts/run_live.sh            # API + worker on $PORT (default 8000)
#   scripts/run_live.sh --tunnel   # also a Cloudflare quick tunnel; prints the
#                                  # public URL to register on GitHub
#
# Reads .env from the repository root. Both processes are stopped on exit.

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo ".env is missing; start from .env.example" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

PORT="${PORT:-8000}"
BIND_ADDRESS="${BIND_ADDRESS:-127.0.0.1}"
if [[ "$BIND_ADDRESS" != "127.0.0.1" && -z "${DASHBOARD_TOKEN:-}" ]]; then
  echo "BIND_ADDRESS=$BIND_ADDRESS exposes the dashboard; set DASHBOARD_TOKEN in .env" >&2
  exit 1
fi
PYTHON="${PYTHON:-python}"
LOG_DIR="${LOG_DIR:-data/logs}"
mkdir -p "$(dirname "${DATABASE_PATH:-data/pipeline.db}")" "$LOG_DIR"

pids=()
cleanup() {
  for pid in "${pids[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

"$PYTHON" -m uvicorn app.main:get_app --factory --host "$BIND_ADDRESS" --port "$PORT" \
  >"$LOG_DIR/api.log" 2>&1 &
pids+=("$!")

"$PYTHON" -m app.worker >"$LOG_DIR/worker.log" 2>&1 &
pids+=("$!")

for _ in $(seq 1 30); do
  if curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
curl -fs "http://127.0.0.1:$PORT/health" >/dev/null || {
  echo "API did not come up; see $LOG_DIR/api.log" >&2
  exit 1
}

echo "api      http://127.0.0.1:$PORT   (log: $LOG_DIR/api.log)"
echo "worker   running                  (log: $LOG_DIR/worker.log)"
echo "env      ${DEVIN_MODE:-sim} devin / ${SLACK_MODE:-fake} slack"

if [[ "${1:-}" == "--tunnel" ]]; then
  [[ -n "${DASHBOARD_TOKEN:-}" ]] || {
    echo "--tunnel makes the dashboard public; set DASHBOARD_TOKEN in .env" >&2
    exit 1
  }
  command -v cloudflared >/dev/null || {
    echo "cloudflared not found: https://github.com/cloudflare/cloudflared/releases" >&2
    exit 1
  }
  cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate \
    >"$LOG_DIR/tunnel.log" 2>&1 &
  pids+=("$!")
  public=""
  for _ in $(seq 1 60); do
    public="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_DIR/tunnel.log" | head -1 || true)"
    [[ -n "$public" ]] && break
    sleep 1
  done
  if [[ -z "$public" ]]; then
    echo "tunnel did not report a URL; see $LOG_DIR/tunnel.log" >&2
    exit 1
  fi
  echo "webhook  $public/webhooks/github   (register on GitHub with GITHUB_WEBHOOK_SECRET)"
  echo "board    $public/dashboard?env=${DEVIN_MODE:-sim}"
fi

wait
