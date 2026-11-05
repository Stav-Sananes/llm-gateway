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
TAG="kind-$(date +%s)"  # new each run: the same tag with pullPolicy Never would not roll
FORWARD_LOG=$(mktemp)
# A kubeconfig of its own: the user's current context is left as it was.
KUBECONFIG=$(mktemp)
export KUBECONFIG

for tool in docker kind kubectl helm curl; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

CREATED_CLUSTER=0
cleanup() {
  local status=$?
  [[ -n "${FORWARD_PID:-}" ]] && kill "$FORWARD_PID" 2>/dev/null || true
  if [[ $status -ne 0 ]]; then  # leave something to debug with
    printf '\n--- failed: pods, events, migration and gateway logs\n' >&2
    kubectl -n "$NS" get pods -o wide >&2 || true
    kubectl -n "$NS" get events --sort-by=.lastTimestamp 2>&1 | tail -30 >&2 || true
    kubectl -n "$NS" logs "job/$FULL-migrate" --tail=50 >&2 || true
    kubectl -n "$NS" logs -l "$GATEWAY_PODS" --tail=50 >&2 || true
  fi
  rm -f "$FORWARD_LOG"
  # Only a cluster this run created: never one that was there before.
  if [[ $CREATED_CLUSTER == 1 && "${KEEP_CLUSTER:-0}" != 1 ]]; then
    kind delete cluster --name "$CLUSTER" || true
  fi
  rm -f "$KUBECONFIG"
}
trap cleanup EXIT

step() { printf '\n--- %s\n' "$*"; }

step "cluster"
clusters=$(kind get clusters)
if grep -qx "$CLUSTER" <<<"$clusters"; then
  kind get kubeconfig --name "$CLUSTER" >"$KUBECONFIG"
else
  CREATED_CLUSTER=1  # before creating: a half-created cluster is deleted too
  kind create cluster --name "$CLUSTER" --kubeconfig "$KUBECONFIG" --wait 120s
fi

step "images"
docker build -q -t "llm-gateway:$TAG" .
docker build -q -f harness/Dockerfile -t llm-gateway-fake-llm:dev .
kind load docker-image --name "$CLUSTER" "llm-gateway:$TAG" llm-gateway-fake-llm:dev

step "dependencies (Postgres, Redis, fake LLM)"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" apply -f deploy/kind/dependencies.yaml
for d in postgres redis fake-llm; do kubectl -n "$NS" rollout status "deploy/$d" --timeout=180s; done

step "install (the migration hook runs first)"
helm upgrade --install "$RELEASE" "$CHART" -n "$NS" -f "$VALUES" --set image.tag="$TAG" --wait --timeout 5m
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
  local answer  # captured first: grep -q exiting early would make curl fail on SIGPIPE
  answer=$(curl -fsS -N "localhost:$LOCAL_PORT/v1/chat/completions" \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d '{"model": "fake", "stream": true, "messages": [{"role": "user", "content": "hi"}]}')
  grep -q '^data: \[DONE\]' <<<"$answer" || { echo "streamed chat did not finish with [DONE]" >&2; exit 1; }
  echo "streamed chat: ok"
}

step "streamed chat"
forward
chat

step "upgrade (the migration hook runs again, on an up-to-date schema)"
# Every pod-template hash running now, not just one: a kept cluster can still have pods
# of an even older ReplicaSet terminating.
OLD_HASHES=$(kubectl -n "$NS" get pods -l "$GATEWAY_PODS" -o jsonpath='{range .items[*]}{.metadata.labels.pod-template-hash}{"\n"}{end}' | sort -u)
helm upgrade "$RELEASE" "$CHART" -n "$NS" -f "$VALUES" --set image.tag="$TAG" \
  --set gateway.podAnnotations.smoke=upgraded --wait --timeout 5m
# --wait returns once the new pods are ready; old ones may still be serving their grace
# period, and port-forward would pin to one of them.
for hash in $OLD_HASHES; do
  old="$GATEWAY_PODS,pod-template-hash=$hash"
  kubectl -n "$NS" wait --for=delete pod -l "$old" --timeout=120s >/dev/null 2>&1 || true
  left=$(kubectl -n "$NS" get pods -l "$old" -o name)  # a kubectl error fails the run
  [[ -z "$left" ]] || { echo "old gateway pods still running after the upgrade: $left" >&2; exit 1; }
done
forward
chat

step "security context"
POD=$(kubectl -n "$NS" get pods -l "$GATEWAY_PODS" -o jsonpath='{.items[0].metadata.name}')
# From the pod spec: the image's own USER would pass an `id -u` check without the chart.
[[ $(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.spec.securityContext.runAsUser}') == 10001 ]] \
  || { echo "the chart doesn't set runAsUser 10001" >&2; exit 1; }
[[ $(kubectl -n "$NS" exec "$POD" -- id -u) == 10001 ]] || { echo "not running as 10001" >&2; exit 1; }
read_only() {  # pod: /tmp is writable (the control), $HOME is refused as read-only
  local pod=$1 refused
  kubectl -n "$NS" exec "$pod" -- touch /tmp/probe \
    || { echo "$pod: cannot write /tmp, so the check below would prove nothing" >&2; exit 1; }
  # $HOME, not /app: uid 10001 can write its home on a writable filesystem, never /app.
  # shellcheck disable=SC2016 # $HOME expands inside the pod, on purpose
  refused=$(kubectl -n "$NS" exec "$pod" -- sh -c 'touch "$HOME/probe"' 2>&1) && {
    echo "$pod: root filesystem is writable" >&2; exit 1; }
  grep -q "Read-only file system" <<<"$refused" \
    || { echo "$pod: write refused for another reason: $refused" >&2; exit 1; }
}
read_only "$POD"
read_only "$(kubectl -n "$NS" get pods -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=usage-worker" -o jsonpath='{.items[0].metadata.name}')"
echo "uid 10001, read-only root filesystem (gateway and worker): ok"

step "usage rows reached Postgres"
rows=0
for _ in $(seq 20); do
  rows=$(kubectl -n "$NS" exec deploy/postgres -- psql -U gateway -tAc \
    "SELECT count(*) FROM usage_events u JOIN tenants t ON t.id = u.tenant_id WHERE t.name = '$TENANT'") \
    || rows=0  # a transient failure is retried, not fatal
  [[ "$rows" -ge 2 ]] && break; sleep 1
done
[[ "$rows" -ge 2 ]] || { echo "expected 2 usage rows, found $rows" >&2; exit 1; }
echo "usage rows: $rows"

printf '\nkind smoke test passed\n'
