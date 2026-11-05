#!/usr/bin/env bash
# Installs the Helm chart on a local kind cluster and checks that it serves a streamed chat
# answer, survives an upgrade (the migration hook runs again: a failed hook fails the
# upgrade), runs as uid 10001 on a read-only filesystem, and writes usage rows.
# Needs docker, kind, kubectl and helm (brew install kind helm kubectl).
# Usage: deploy/kind/smoke-test.sh            (KEEP_CLUSTER=1 leaves the cluster running)
set -euo pipefail
cd "$(dirname "$0")/../.."

CLUSTER=llm-gateway-smoke
RELEASE=gw
NS=llm-gateway
CHART=deploy/helm/llm-gateway
VALUES=deploy/kind/values.yaml
LOCAL_PORT=18080
GATEWAY_PODS="app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=gateway"
FULL="$RELEASE-llm-gateway"
FORWARD_LOG=$(mktemp)

for tool in docker kind kubectl helm curl; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

CREATED_CLUSTER=0
cleanup() {
  local status=$?
  [[ -n "${FORWARD_PID:-}" ]] && kill "$FORWARD_PID" 2>/dev/null || true
  if [[ $status -ne 0 ]]; then  # leave something to debug with
    printf '\n--- failed: pods, events and gateway logs\n' >&2
    kubectl -n "$NS" get pods -o wide >&2 || true
    kubectl -n "$NS" get events --sort-by=.lastTimestamp >&2 | tail -30 || true
    kubectl -n "$NS" logs -l "$GATEWAY_PODS" --tail=50 >&2 || true
  fi
  # Only a cluster this run created: never one that was there before.
  if [[ $CREATED_CLUSTER == 1 && "${KEEP_CLUSTER:-0}" != 1 ]]; then
    kind delete cluster --name "$CLUSTER"
  fi
}
trap cleanup EXIT

step() { printf '\n--- %s\n' "$*"; }

step "cluster"
if ! kind get clusters | grep -qx "$CLUSTER"; then
  kind create cluster --name "$CLUSTER" --wait 120s
  CREATED_CLUSTER=1
fi
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
kubectl -n "$NS" rollout status "deploy/$FULL-usage-worker" --timeout=120s

step "tenant and key"
TENANT="smoke-$(date +%s)"  # a new one each run, so a kept cluster can run it again
kubectl -n "$NS" exec "deploy/$FULL" -- llm-gateway admin create-tenant "$TENANT" >/dev/null
KEY=$(kubectl -n "$NS" exec "deploy/$FULL" -- llm-gateway admin create-key "$TENANT")
[[ "$KEY" == gw_* ]] || { echo "no API key printed" >&2; exit 1; }

forward() {
  [[ -n "${FORWARD_PID:-}" ]] && { kill "$FORWARD_PID" 2>/dev/null || true; wait "$FORWARD_PID" 2>/dev/null || true; }
  kubectl -n "$NS" port-forward "svc/$FULL" "$LOCAL_PORT:80" >"$FORWARD_LOG" 2>&1 &
  FORWARD_PID=$!
  for _ in $(seq 30); do curl -fs "localhost:$LOCAL_PORT/healthz" >/dev/null && return; sleep 1; done
  echo "port-forward never answered:" >&2; cat "$FORWARD_LOG" >&2; exit 1
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
OLD_HASH=$(kubectl -n "$NS" get pods -l "$GATEWAY_PODS" -o jsonpath='{.items[0].metadata.labels.pod-template-hash}')
helm upgrade "$RELEASE" "$CHART" -n "$NS" -f "$VALUES" --set gateway.podAnnotations.smoke=upgraded --wait --timeout 5m
# --wait returns once the new pods are ready; old ones may still be serving their grace
# period, and port-forward would pin to one of them.
OLD_PODS="$GATEWAY_PODS,pod-template-hash=$OLD_HASH"
kubectl -n "$NS" wait --for=delete pod -l "$OLD_PODS" --timeout=120s 2>/dev/null || true
[[ -z $(kubectl -n "$NS" get pods -l "$OLD_PODS" -o name) ]] \
  || { echo "old gateway pods still running after the upgrade" >&2; exit 1; }
forward
chat

step "security context"
POD=$(kubectl -n "$NS" get pods -l "$GATEWAY_PODS" -o jsonpath='{.items[0].metadata.name}')
# From the pod spec: the image's own USER would pass an `id -u` check without the chart.
[[ $(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.spec.securityContext.runAsUser}') == 10001 ]] \
  || { echo "the chart doesn't set runAsUser 10001" >&2; exit 1; }
[[ $(kubectl -n "$NS" exec "$POD" -- id -u) == 10001 ]] || { echo "not running as 10001" >&2; exit 1; }
# $HOME, not /app: uid 10001 can write its home on a writable filesystem, never /app.
# shellcheck disable=SC2016 # $HOME expands inside the pod, on purpose
if kubectl -n "$NS" exec "$POD" -- sh -c 'touch "$HOME/probe"' 2>/dev/null; then
  echo "root filesystem is writable" >&2; exit 1
fi
echo "uid 10001, read-only root filesystem: ok"

step "usage rows reached Postgres"
rows=0
for _ in $(seq 20); do
  rows=$(kubectl -n "$NS" exec deploy/postgres -- psql -U gateway -tAc \
    "SELECT count(*) FROM usage_events u JOIN tenants t ON t.id = u.tenant_id WHERE t.name = '$TENANT'")
  [[ "$rows" -ge 2 ]] && break; sleep 1
done
[[ "$rows" -ge 2 ]] || { echo "expected 2 usage rows, found $rows" >&2; exit 1; }
echo "usage rows: $rows"

printf '\nkind smoke test passed\n'
