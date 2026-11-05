# 15 — The Helm chart

**Goal (SPEC):** a Helm chart, tested on kind. There's no always-on cloud deployment.

This is build step 10c, the last one before the demo video.

## What it deploys

```
deploy/helm/llm-gateway
  pre-install / pre-upgrade hooks:
     Secret <release>-migrate (weight -5, removed after success)  → Job llm-gateway migrate (weight 0)
  Deployment <release>                gateway, 2 replicas, rolling (maxUnavailable 0)
     ConfigMap <release>-config       GATEWAY_* settings from values.config
     Secret    <release>-secrets      connection strings and keys (or existingSecret)
     ConfigMap <release>-policy       the Cedar policy, when values.policy is set
  Deployment <release>-usage-worker   1 replica (each pod is a consumer in one group)
  Service <release>                   port 80 → 8080
  PodDisruptionBudget                 maxUnavailable 1
  Ingress (optional)                  /v1 and /mcp only
```

Postgres, Redis, Qdrant and the model providers are not in the chart. It points at them through the URLs in the secret. The kind smoke test brings throwaway ones of its own.

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **Migrations run as a Helm hook before every install and upgrade**, as one Job. | New code never runs against an old schema, and one Job means no two migrations race (Alembic takes no lock). Migrations must therefore be backward compatible with the running version: expand, then contract. | An init container in each gateway pod: replicas would migrate concurrently, and every pod would need a role allowed to change the schema. A post-install hook: new pods would start against the old schema. |
| **The migration Job gets its own hook Secret**, created just before it (weight −5) and removed once it succeeds. It can use a different database role (`secrets.migrationDatabaseUrl`). It runs under the namespace's default service account, with no token. | Hooks run before the release's own resources exist, so the Job can't use the release's Secret or service account on a first install. A separate role lets the running gateway do without DDL rights. | Making the main Secret a hook: Helm doesn't track hook resources, so the gateway's own Secret would outlive `helm uninstall` and be recreated on every upgrade. |
| **Readiness is `/healthz`, not `/readyz`.** `/readyz` stays, for dashboards and alerts. | `/readyz` fails when every provider's circuit is open, which happens on every pod at once. Kubernetes would then take every pod out of the Service together, and MCP tool calls would go down with the model providers, though they need no model. The chaos runs (14) showed a pod answering correctly with every provider down: 503s with a reason, fast. | Readiness on `/readyz`: an LLM outage becomes a gateway outage. |
| **Liveness is generous** (every 10 s, 5 s timeout, 6 failures). | Under overload the event loop runs late and `/healthz` answers slowly. Killing an overloaded pod moves its load onto the others, and they fall over in turn: a chain reaction. | Kubernetes' defaults (1 s timeout, 3 failures). |
| **On shutdown: a `preStop` sleep of 5 s, then uvicorn's 20 s for running requests, inside a grace period computed from both** (35 s). | The sleep lets the Service drop the pod before it stops accepting connections. The 20 s lets held approvals be closed as abandoned (12) before the kubelet kills the process. | The default 30 s grace with no `preStop`: requests routed in the gap get connection errors, and holds can be killed mid-close. |
| **The ingress exposes only `/v1` and `/mcp`.** | `/metrics`, `/healthz` and `/readyz` are unauthenticated. They stay inside the cluster, for Prometheus and the kubelet. | Exposing everything: tenant names and traffic in the metrics would be public. |
| **Non-root (uid 10001), read-only root filesystem, every capability dropped, no privilege escalation, seccomp `RuntimeDefault`, no service-account token.** `/tmp` is a 64 MiB `emptyDir`; `PYTHONDONTWRITEBYTECODE` is set. | The gateway holds every tenant's traffic and the provider keys. It needs no Kubernetes API, no root and no writes outside `/tmp`. The image already compiles bytecode at build time. | Defaults: root-capable and writable. |
| **No CPU limit; a memory limit.** | A CPU limit throttles the process, and throttling shows up as event-loop lag: the gateway would shed load (13) while the node has CPU to spare. Memory has no such side effect, and a leak should end in a restart, not a full node. | A CPU limit equal to the request. |
| **A disruption budget of `maxUnavailable: 1`**, not `minAvailable: 1`. | With one replica, `minAvailable: 1` blocks every node drain. | — |
| **Settings come from `values.config` as `GATEWAY_<KEY>`**, and the pods roll when the config or the Secret changes (checksum annotations). | Any setting works without touching the chart, and a changed setting takes effect. | One chart value per setting: the chart would trail every new setting. |
| **Two replicas spread across nodes** (topology spread, best effort). | One node going down shouldn't take the gateway with it. Admission is per process (13), and rate limits and budgets are shared through Redis, so replicas need no coordination. | — |

## How it's tested

- **`tests/deploy/test_helm_chart.py`** renders the chart with the real helm binary (its container image, `alpine/helm:3.19.0`) and checks what the chart promises:
  - what the ingress exposes;
  - the security context of every workload;
  - the grace period against the shutdown timeout;
  - readiness;
  - the hook order;
  - secrets and an existing secret;
  - settings and the policy reaching every container;
  - pods rolling on a settings change;
  - the disruption budget;
  - `helm lint --strict`.

  Six mutations of the templates (readiness on `/readyz`, root allowed, a grace period without the margin, `/` on the ingress, the hook order swapped, `minAvailable`) each fail a test.
- **kubeconform, strict, against Kubernetes 1.34 schemas:** the rendered chart (11 resources) and the smoke test's dependencies (6) are valid. Run by hand; it downloads the schemas, so it isn't part of the gate.
- **`deploy/kind/smoke-test.sh`** installs the chart on a kind cluster with throwaway Postgres and Redis and the fake LLM, then checks:
  - a streamed chat ends in `[DONE]`;
  - an upgrade re-runs the migration hook on an up-to-date schema and serves again;
  - the pod runs as uid 10001 and can't write its root filesystem;
  - usage rows reach Postgres through the worker.

  It needs `kind` and `helm` (`brew install kind helm`). **It has not been run yet**: downloading kind and helm was blocked in the session that wrote it.

## Known gaps

- **The kind smoke test is unrun.** Until it is, "tested on kind" is not true.
- **No dependencies in the chart.** Production needs a highly available Redis (14: it is a single point of failure for chat), a Postgres with backups, and Qdrant with a volume. Those belong to their own charts or a managed service.
- **No autoscaler.** The signal to scale on would be the event-loop lag (`gateway_event_loop_lag_seconds`) or the shed rate, through a metrics adapter; CPU alone lags behind.
- **No NetworkPolicy.** Inside the cluster, any pod can reach `/metrics`, and the gateway can reach anything. A policy would allow ingress from the ingress controller and Prometheus only, and egress to the stores and providers.
- **Request-body deadlines and per-IP limits** for unauthenticated traffic (13) belong to the ingress controller's annotations, which depend on the controller and aren't set here.
- **No ServiceMonitor.** Pods carry `prometheus.io/*` annotations; a Prometheus Operator install would want a ServiceMonitor instead.
- **One usage worker by default.** More replicas share the stream through the consumer group. Entries a dead pod left unacknowledged are claimed by the others after the reclaim idle time.

## Interview questions to be ready for

1. *Why doesn't your readiness probe check the model providers?*
2. *How do you run database migrations in a rolling deployment without breaking the pods still on the old version?*
3. *Why no CPU limit?*
