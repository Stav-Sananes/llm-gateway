#!/usr/bin/env bash
# Measure the latency the gateway adds: the same k6 load against the fake LLM directly, then
# through the gateway. Needs Docker. Usage: load/run.sh [RPS] [DURATION]
set -euo pipefail
cd "$(dirname "$0")/.."
# The stack publishes the same host ports as the dev stack (8080, 16686, ...): stop that first.
# Each run leaves one tenant behind (named load-<timestamp>).

RPS="${1:-50}"
DURATION="${2:-60s}"
PROJECT=gwload
COMPOSE=(docker compose -p "$PROJECT" -f compose.yaml -f load/compose.load.yaml)
K6_IMAGE=grafana/k6:1.3.0

"${COMPOSE[@]}" up -d --build --wait gateway fake-llm usage-worker
tenant="load-$(date +%s)"
"${COMPOSE[@]}" exec -T gateway llm-gateway admin create-tenant "$tenant" >/dev/null
key="$("${COMPOSE[@]}" exec -T gateway llm-gateway admin create-key "$tenant")"

k6() {  # name, target URL, API key
  echo "--- $1: $RPS requests/s for $DURATION"
  docker run --rm -i --network "${PROJECT}_default" \
    -e TARGET_URL="$2" -e API_KEY="$3" -e RPS="$RPS" -e DURATION="$DURATION" \
    -v "$PWD/load:/load:ro" "$K6_IMAGE" run --quiet \
    /load/chat.js | python3 load/summary.py "$1"
}

# Alternating rounds, so drift on the machine affects both paths alike.
for round in 1 2; do
  k6 "round $round, fake LLM directly" http://fake-llm:8000/v1/chat/completions ""
  k6 "round $round, through the gateway" http://gateway:8080/v1/chat/completions "$key"
done
