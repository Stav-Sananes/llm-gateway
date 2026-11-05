#!/usr/bin/env bash
# Installs the Helm chart on a local kind cluster and checks that it serves a streamed chat
# answer, survives an upgrade (the migration hook runs again) and a rolling restart.
# Needs docker, kind, kubectl and helm (brew install kind helm kubectl).
# Usage: deploy/kind/smoke-test.sh            (KEEP_CLUSTER=1 leaves the cluster running)
set -euo pipefail
cd "$(dirname "$0")/../.."

CLUSTER=llm-gateway
RELEASE=gw
NS=llm-gateway
CHART=deploy/helm/llm-gateway
VALUES=deploy/kind/values.yaml
LOCAL_PORT=18080

for tool in docker kind kubectl helm curl; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

cleanup() {
  [[ -n "${FORWARD_PID:-}" ]] && kill "$FORWARD_PID" 2>/dev/null || true
  if [[ "${KEEP_CLUSTER:-0}" != 1 ]]; then kind delete cluster --name "$CLUSTER"; fi
}
trap cleanup EXIT

step() { printf '\n--- %s\n' "$*"; }

step "cluster"
kind get clusters | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --wait 120s
kubectl config use-context "kind-$CLUSTER" >/dev/null

step "images"
docker build -q -t llm-gateway:kind .
docker build -q -f harness/Dockerfile -t llm-gateway-fake-llm:dev .
kind load docker-image --name "$CLUSTER" llm-gateway:kind llm-gateway-fake-llm:dev

step "dependencies (Postgres, Redis, fake LLM)"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" apply -f deploy/kind/dependencies.yaml
for d in postgres redis fake-llm; do kubectl -n "$NS" rollout status "deploy/$d" --timeout=180s; done

step "install (the migration hook runs first)"
helm upgrade --install "$RELEASE" "$CHART" -n "$NS" -f "$VALUES" --wait --timeout 5m
FULL="$RELEASE-llm-gateway"
kubectl -n "$NS" rollout status "deploy/$FULL-usage-worker" --timeout=120s

step "tenant and key"
kubectl -n "$NS" exec "deploy/$FULL" -- llm-gateway admin create-tenant acme >/dev/null
KEY=$(kubectl -n "$NS" exec "deploy/$FULL" -- llm-gateway admin create-key acme 2>/dev/null)
[[ "$KEY" == gw_* ]] || { echo "no API key printed" >&2; exit 1; }

forward() {
  [[ -n "${FORWARD_PID:-}" ]] && kill "$FORWARD_PID" 2>/dev/null || true
  kubectl -n "$NS" port-forward "svc/$FULL" "$LOCAL_PORT:80" >/dev/null 2>&1 &
  FORWARD_PID=$!
  for _ in $(seq 30); do curl -fs "localhost:$LOCAL_PORT/healthz" >/dev/null && return; sleep 1; done
  echo "port-forward never answered" >&2; exit 1
}

chat() {
  curl -fsS -N "localhost:$LOCAL_PORT/v1/chat/completions" \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d '{"model": "fake", "stream": true, "messages": [{"role": "user", "content": "hi"}]}' \
    | grep -q '^data: \[DONE\]' || { echo "streamed chat did not finish with [DONE]" >&2; exit 1; }
  echo "streamed chat: ok"
}

step "streamed chat"
forward
chat

step "upgrade (the migration hook runs again, on an up-to-date schema)"
helm upgrade "$RELEASE" "$CHART" -n "$NS" -f "$VALUES" --set gateway.podAnnotations.smoke=upgraded --wait --timeout 5m
forward
chat

step "security context"
uid=$(kubectl -n "$NS" exec "deploy/$FULL" -- id -u)
[[ "$uid" == 10001 ]] || { echo "runs as uid $uid, expected 10001" >&2; exit 1; }
if kubectl -n "$NS" exec "deploy/$FULL" -- touch /app/probe 2>/dev/null; then
  echo "root filesystem is writable" >&2; exit 1
fi
echo "uid 10001, read-only root filesystem: ok"

step "usage rows reached Postgres"
for _ in $(seq 20); do
  rows=$(kubectl -n "$NS" exec deploy/postgres -- psql -U gateway -tAc 'SELECT count(*) FROM usage_events')
  [[ "$rows" -ge 2 ]] && break; sleep 1
done
[[ "$rows" -ge 2 ]] || { echo "expected 2 usage rows, found $rows" >&2; exit 1; }
echo "usage rows: $rows"

printf '\nkind smoke test passed\n'
