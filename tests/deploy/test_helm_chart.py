"""The Helm chart, rendered by the real helm binary (in its container image).

These check what the chart promises an operator: what's exposed, how pods run and stop,
and when migrations run. The kind smoke test (deploy/kind/smoke-test.sh) checks that the
rendered chart actually works on a cluster."""

import json
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELM_IMAGE = "alpine/helm:3.19.0"
REQUIRED = {
    "secrets": {
        "databaseUrl": "postgresql+asyncpg://gateway:pw@postgres/gateway",
        "redisUrl": "redis://redis:6379/0",
    }
}

Manifest = dict[str, Any]


def helm(*args: str, values: dict[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
    """Run helm against the repo's deploy/helm directory, values passed on stdin."""
    charts = f"{ROOT / 'deploy' / 'helm'}:/charts:ro"
    command = ["docker", "run", "--rm", "-i", "-v", charts, "-w", "/charts", HELM_IMAGE]
    return subprocess.run(  # noqa: S603 - a fixed docker command; docker from PATH
        [*command, *args, "-f", "-"],
        input=yaml.safe_dump(values or {}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def merged(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merged(result[key], value)
        else:
            result[key] = value
    return result


GATEWAY, WORKER, MIGRATE = "gw-llm-gateway", "gw-llm-gateway-usage-worker", "gw-llm-gateway-migrate"
_rendered: dict[str, str] = {}


@pytest.fixture(scope="module")
def render() -> Callable[..., list[Manifest]]:
    """The chart rendered with REQUIRED plus `extra`; each set of values renders once."""

    def rendered(**extra: Any) -> list[Manifest]:
        values = merged(REQUIRED, extra)
        key = json.dumps(values, sort_keys=True)
        if key not in _rendered:
            done = helm("template", "gw", "llm-gateway", values=values)
            assert done.returncode == 0, done.stderr
            _rendered[key] = done.stdout
        return [doc for doc in yaml.safe_load_all(_rendered[key]) if doc]

    return rendered


def find(manifests: list[Manifest], kind: str, name: str) -> Manifest:
    matches = [m for m in manifests if m["kind"] == kind and m["metadata"]["name"] == name]
    assert matches, f"no {kind} {name} in {[(m['kind'], m['metadata']['name']) for m in manifests]}"
    return matches[0]


def pod_specs(manifests: list[Manifest]) -> dict[str, Manifest]:
    return {
        m["metadata"]["name"]: m["spec"]["template"]["spec"]
        for m in manifests
        if m["kind"] in ("Deployment", "Job")
    }


def test_the_chart_lints_cleanly() -> None:
    done = helm("lint", "llm-gateway", "--strict", values=REQUIRED)

    assert done.returncode == 0, done.stdout + done.stderr


def test_the_ingress_exposes_only_the_client_apis(render: Callable[..., list[Manifest]]) -> None:
    ingress = find(render(ingress={"enabled": True}), "Ingress", "gw-llm-gateway")

    paths = {
        (p["path"], p["pathType"])
        for rule in ingress["spec"]["rules"]
        for p in rule["http"]["paths"]
    }
    # /metrics, /healthz and /readyz are unauthenticated: they must stay inside the cluster.
    assert paths == {("/v1", "Prefix"), ("/mcp", "Exact")}


def test_there_is_no_ingress_unless_asked_for(render: Callable[..., list[Manifest]]) -> None:
    assert not [m for m in render() if m["kind"] == "Ingress"]


def test_every_workload_runs_as_a_non_root_user_on_a_read_only_filesystem(
    render: Callable[..., list[Manifest]],
) -> None:
    specs = pod_specs(render())

    assert set(specs) == {"gw-llm-gateway", "gw-llm-gateway-usage-worker", "gw-llm-gateway-migrate"}
    for name, spec in specs.items():
        assert spec["securityContext"]["runAsNonRoot"] is True, name
        assert spec["securityContext"]["runAsUser"] == 10001, name
        assert spec["automountServiceAccountToken"] is False, name
        for container in spec["containers"]:
            context = container["securityContext"]
            assert context["readOnlyRootFilesystem"] is True, name
            assert context["allowPrivilegeEscalation"] is False, name
            assert context["capabilities"]["drop"] == ["ALL"], name
            tmp = {"name": "tmp", "mountPath": "/tmp"}  # noqa: S108 - the pod's emptyDir
            assert tmp in container["volumeMounts"], name


@pytest.mark.parametrize(("shutdown_s", "pre_stop_s"), [(40, 7), (5, 0), (120, 15)])
def test_the_gateway_gets_time_to_close_held_calls_before_it_is_killed(
    render: Callable[..., list[Manifest]], shutdown_s: int, pre_stop_s: int
) -> None:
    manifests = render(
        gateway={"shutdownTimeoutSeconds": shutdown_s, "preStopSleepSeconds": pre_stop_s}
    )
    spec = pod_specs(manifests)[GATEWAY]
    config = find(manifests, "ConfigMap", f"{GATEWAY}-config")["data"]

    assert config["GATEWAY_SHUTDOWN_TIMEOUT_S"] == str(shutdown_s)
    assert spec["containers"][0]["lifecycle"]["preStop"]["exec"]["command"] == [
        "sleep",
        str(pre_stop_s),
    ]
    # A margin past both, for the shielded close of held approvals and the lifespan.
    assert spec["terminationGracePeriodSeconds"] - pre_stop_s - shutdown_s >= 5


def test_readiness_does_not_depend_on_the_model_providers(
    render: Callable[..., list[Manifest]],
) -> None:
    gateway = pod_specs(render())["gw-llm-gateway"]["containers"][0]

    # /readyz fails when every provider's circuit is open: on every pod at once.
    assert gateway["readinessProbe"]["httpGet"]["path"] == "/healthz"


def test_migrations_run_as_a_hook_before_install_and_upgrade(
    render: Callable[..., list[Manifest]],
) -> None:
    manifests = render()
    job = find(manifests, "Job", "gw-llm-gateway-migrate")
    job_secret = find(manifests, "Secret", "gw-llm-gateway-migrate")

    assert job["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert job["spec"]["template"]["spec"]["containers"][0]["command"] == ["llm-gateway", "migrate"]
    # The job's secret is created before it, as a hook too: release resources come later.
    assert job_secret["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert int(job_secret["metadata"]["annotations"]["helm.sh/hook-weight"]) < int(
        job["metadata"]["annotations"]["helm.sh/hook-weight"]
    )


def test_the_migration_may_use_its_own_database_role(render: Callable[..., list[Manifest]]) -> None:
    owner = "postgresql+asyncpg://owner:pw@postgres/gateway"
    manifests = render(secrets={"migrationDatabaseUrl": owner})

    assert find(manifests, "Secret", "gw-llm-gateway-migrate")["stringData"] == {
        "GATEWAY_DATABASE_URL": owner
    }
    assert (
        find(manifests, "Secret", "gw-llm-gateway-secrets")["stringData"]["GATEWAY_DATABASE_URL"]
        == REQUIRED["secrets"]["databaseUrl"]
    )


def test_an_existing_secret_replaces_the_generated_ones(
    render: Callable[..., list[Manifest]],
) -> None:
    manifests = render(existingSecret="prod-gateway", secrets={"databaseUrl": "", "redisUrl": ""})

    assert not [m for m in manifests if m["kind"] == "Secret"]
    assert set(pod_specs(manifests)) == {GATEWAY, WORKER, MIGRATE}
    for name, spec in pod_specs(manifests).items():
        container = spec["containers"][0]
        refs = [e["secretRef"]["name"] for e in container.get("envFrom", []) if "secretRef" in e]
        refs += [
            e["valueFrom"]["secretKeyRef"]["name"]
            for e in container.get("env", [])
            if "valueFrom" in e
        ]
        assert refs == ["prod-gateway"], name


def test_installing_without_connection_strings_fails_with_a_clear_message() -> None:
    done = helm("template", "gw", "llm-gateway", values={})

    assert done.returncode != 0
    assert "secrets.databaseUrl is required" in done.stderr


def test_settings_and_the_policy_reach_every_container(
    render: Callable[..., list[Manifest]],
) -> None:
    policy = "permit(principal, action, resource);\n"
    manifests = render(config={"ROUTES": '{"smart": []}'}, policy=policy)

    config = find(manifests, "ConfigMap", "gw-llm-gateway-config")["data"]
    assert config["GATEWAY_ROUTES"] == '{"smart": []}'
    assert find(manifests, "ConfigMap", "gw-llm-gateway-policy")["data"]["policy.cedar"] == policy
    for name in ("gw-llm-gateway", "gw-llm-gateway-usage-worker"):
        container = pod_specs(manifests)[name]["containers"][0]
        assert {"name": "GATEWAY_POLICY_FILE", "value": "/etc/llm-gateway/policy.cedar"} in (
            container["env"]
        ), name
        assert {"configMapRef": {"name": "gw-llm-gateway-config"}} in container["envFrom"], name


def test_a_settings_change_rolls_the_pods(render: Callable[..., list[Manifest]]) -> None:
    def checksum(manifests: list[Manifest]) -> str:
        template = find(manifests, "Deployment", "gw-llm-gateway")["spec"]["template"]
        return str(template["metadata"]["annotations"]["checksum/config"])

    assert checksum(render()) != checksum(render(config={"CACHE_ENABLED": "true"}))


def test_the_disruption_budget_never_blocks_a_drain_of_a_single_replica(
    render: Callable[..., list[Manifest]],
) -> None:
    budget = find(render(gateway={"replicas": 1}), "PodDisruptionBudget", "gw-llm-gateway")

    assert budget["spec"] == {
        "maxUnavailable": 1,
        "selector": {
            "matchLabels": {
                "app.kubernetes.io/name": "llm-gateway",
                "app.kubernetes.io/instance": "gw",
                "app.kubernetes.io/component": "gateway",
            }
        },
    }


def test_the_migration_can_have_its_own_existing_secret(
    render: Callable[..., list[Manifest]],
) -> None:
    manifests = render(
        existingSecret="app-role",
        migrations={"existingSecret": "owner-role"},
        secrets={"databaseUrl": "", "redisUrl": ""},
    )

    job = pod_specs(manifests)["gw-llm-gateway-migrate"]["containers"][0]
    (url,) = [e for e in job["env"] if e["name"] == "GATEWAY_DATABASE_URL"]
    assert url["valueFrom"]["secretKeyRef"]["name"] == "owner-role"


def test_the_migration_secret_and_job_are_cleaned_up_without_blocking_the_next_upgrade(
    render: Callable[..., list[Manifest]],
) -> None:
    manifests = render()
    secret = find(manifests, "Secret", MIGRATE)["metadata"]["annotations"]
    job = find(manifests, "Job", MIGRATE)["metadata"]["annotations"]

    # The Secret holds the schema owner's credentials. Its own hook step succeeds before
    # the Job runs, so Helm applies hook-succeeded to it whether the migration then
    # succeeds or fails: that is what removes it.
    assert {"before-hook-creation", "hook-succeeded"} <= set(
        secret["helm.sh/hook-delete-policy"].split(",")
    )
    # A Job left by a failed run (kept for its logs) must not make the next one fail with
    # "already exists".
    assert "before-hook-creation" in job["helm.sh/hook-delete-policy"].split(",")


def test_a_failed_migration_pod_is_never_replaced_while_it_still_runs(
    render: Callable[..., list[Manifest]],
) -> None:
    job = find(render(), "Job", "gw-llm-gateway-migrate")

    assert job["spec"]["podReplacementPolicy"] == "Failed"  # never two migrations at once


def test_no_hash_of_the_secret_is_put_in_the_pod_spec(
    render: Callable[..., list[Manifest]],
) -> None:
    # Anyone who can read pods could test password guesses against it offline.
    deployments = {m["metadata"]["name"]: m for m in render() if m["kind"] == "Deployment"}
    assert set(deployments) == {GATEWAY, WORKER}
    for name, deployment in deployments.items():
        annotations = deployment["spec"]["template"]["metadata"]["annotations"]
        assert "checksum/secret" not in annotations, name
        assert "checksum/config" in annotations, name


def test_a_digest_pins_the_image_over_the_tag(render: Callable[..., list[Manifest]]) -> None:
    digest = "sha256:" + "a" * 64
    specs = pod_specs(render(image={"tag": "moved", "digest": digest}))

    assert {s["containers"][0]["image"] for s in specs.values()} == {f"llm-gateway@{digest}"}


def test_extra_environment_sources_reach_every_long_running_container(
    render: Callable[..., list[Manifest]],
) -> None:
    extra = {"secretRef": {"name": "mcp-servers"}}
    specs = pod_specs(render(extraEnvFrom=[extra]))

    for name in ("gw-llm-gateway", "gw-llm-gateway-usage-worker"):
        assert extra in specs[name]["containers"][0]["envFrom"], name


@pytest.mark.parametrize(
    "key", ["PORT", "SHUTDOWN_TIMEOUT_S", "USAGE_WORKER_METRICS_PORT", "POLICY_FILE"]
)
def test_a_setting_the_chart_owns_is_refused_in_config(key: str) -> None:
    # Two GATEWAY_PORT keys would make the ConfigMap invalid, or silently drop one.
    done = helm("template", "gw", "llm-gateway", values=merged(REQUIRED, {"config": {key: "9"}}))

    assert done.returncode != 0
    assert f"config.{key} is set by the chart" in done.stderr


def references(spec: Manifest) -> Iterator[tuple[str, str, str]]:
    """(kind, name, key) of every ConfigMap or Secret a pod spec refers to; key "" for a
    whole object."""
    for volume in spec.get("volumes", []):
        if "configMap" in volume:
            yield "ConfigMap", volume["configMap"]["name"], ""
    for container in spec["containers"]:
        for source in container.get("envFrom", []):
            if "configMapRef" in source:
                yield "ConfigMap", source["configMapRef"]["name"], ""
            if "secretRef" in source:
                yield "Secret", source["secretRef"]["name"], ""
        for variable in container.get("env", []):
            if "secretKeyRef" in variable.get("valueFrom", {}):
                ref = variable["valueFrom"]["secretKeyRef"]
                yield "Secret", ref["name"], ref["key"]


def assert_references_resolve(manifests: list[Manifest], existing: set[str]) -> None:
    """Every name one object uses for another points at something that will exist: the
    API server takes dangling names, and the pod just never starts."""
    objects = {(m["kind"], m["metadata"]["name"]): m for m in manifests}
    hook_secrets = {
        m["metadata"]["name"]
        for m in manifests
        if m["kind"] == "Secret" and "helm.sh/hook" in m["metadata"].get("annotations", {})
    }
    for workload, spec in pod_specs(manifests).items():
        is_hook = workload == MIGRATE  # runs before the release's own objects exist
        for container in spec["containers"]:
            mounts = {mount["name"] for mount in container.get("volumeMounts", [])}
            assert mounts <= {v["name"] for v in spec.get("volumes", [])}, workload
            ports = {port["name"] for port in container.get("ports", [])}
            for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
                action = container.get(probe, {})
                port = (action.get("httpGet") or action.get("tcpSocket") or {}).get("port")
                if isinstance(port, str):
                    assert port in ports, (workload, probe, port)
        for kind, name, key in references(spec):
            if kind == "Secret" and name in existing:
                continue
            if is_hook:
                assert kind == "Secret" and name in hook_secrets, (workload, kind, name)
            target = objects.get((kind, name))
            assert target is not None, (workload, kind, name)
            if key:
                keys: dict[str, str] = target.get("stringData") or target.get("data") or {}
                assert key in keys, (name, key)
    service = objects[("Service", GATEWAY)]
    gateway_ports = {p["name"] for p in pod_specs(manifests)[GATEWAY]["containers"][0]["ports"]}
    assert {p["targetPort"] for p in service["spec"]["ports"]} <= gateway_ports
    for ingress in (m for m in manifests if m["kind"] == "Ingress"):
        for rule in ingress["spec"]["rules"]:
            for path in rule["http"]["paths"]:
                backend = path["backend"]["service"]
                assert backend["name"] == GATEWAY
                assert backend["port"]["name"] in {p["name"] for p in service["spec"]["ports"]}


@pytest.mark.parametrize(
    ("extra", "existing"),
    [
        ({}, set()),
        ({"ingress": {"enabled": True}, "policy": "permit(principal, action, resource);"}, set()),
        (
            {"existingSecret": "prod", "secrets": {"databaseUrl": "", "redisUrl": ""}},
            {"prod"},
        ),
        (
            {
                "existingSecret": "prod",
                "migrations": {"existingSecret": "owner"},
                "secrets": {"databaseUrl": "", "redisUrl": ""},
            },
            {"prod", "owner"},
        ),
    ],
    ids=["defaults", "ingress-and-policy", "existing-secret", "existing-migration-secret"],
)
def test_every_reference_between_objects_resolves(
    render: Callable[..., list[Manifest]], extra: dict[str, Any], existing: set[str]
) -> None:
    assert_references_resolve(render(**extra), existing)


def selected(manifests: list[Manifest], selector: dict[str, str]) -> set[str]:
    """The workloads whose pods a label selector picks."""
    return {
        m["metadata"]["name"]
        for m in manifests
        if m["kind"] in ("Deployment", "Job")
        and selector.items() <= m["spec"]["template"]["metadata"]["labels"].items()
    }


def test_the_service_budget_and_spread_select_exactly_the_gateway_pods(
    render: Callable[..., list[Manifest]],
) -> None:
    manifests = render()
    gateway = find(manifests, "Deployment", GATEWAY)
    selectors = {
        "Service": find(manifests, "Service", GATEWAY)["spec"]["selector"],
        "PodDisruptionBudget": find(manifests, "PodDisruptionBudget", GATEWAY)["spec"]["selector"][
            "matchLabels"
        ],
        "topology spread": gateway["spec"]["template"]["spec"]["topologySpreadConstraints"][0][
            "labelSelector"
        ]["matchLabels"],
        "Deployment": gateway["spec"]["selector"]["matchLabels"],
    }

    for what, selector in selectors.items():
        assert selected(manifests, selector) == {GATEWAY}, what
    worker = find(manifests, "Deployment", WORKER)["spec"]["selector"]["matchLabels"]
    assert selected(manifests, worker) == {WORKER}


def test_probes_are_lenient_where_a_restart_would_spread_the_load(
    render: Callable[..., list[Manifest]],
) -> None:
    gateway = pod_specs(render())[GATEWAY]["containers"][0]
    liveness, startup = gateway["livenessProbe"], gateway["startupProbe"]

    # Not /readyz: a provider outage must not restart every pod.
    assert liveness["httpGet"]["path"] == startup["httpGet"]["path"] == "/healthz"
    # An overloaded pod answers late: a minute of failures before it is killed.
    assert liveness["timeoutSeconds"] >= 5
    assert liveness["periodSeconds"] * liveness["failureThreshold"] >= 60


def test_a_rollout_never_takes_a_gateway_pod_away_before_its_replacement_is_ready(
    render: Callable[..., list[Manifest]],
) -> None:
    strategy = find(render(), "Deployment", GATEWAY)["spec"]["strategy"]

    assert strategy["rollingUpdate"]["maxUnavailable"] == 0


def test_every_workload_runs_with_the_default_seccomp_profile_and_its_own_group(
    render: Callable[..., list[Manifest]],
) -> None:
    specs = pod_specs(render())

    assert set(specs) == {GATEWAY, WORKER, MIGRATE}
    for name, spec in specs.items():
        context = spec["securityContext"]
        assert context["seccompProfile"] == {"type": "RuntimeDefault"}, name
        assert (context["runAsGroup"], context["fsGroup"]) == (10001, 10001), name


def test_a_failed_migration_is_not_restarted_in_place_and_cannot_run_forever(
    render: Callable[..., list[Manifest]],
) -> None:
    job = find(render(), "Job", MIGRATE)["spec"]

    assert job["template"]["spec"]["restartPolicy"] == "Never"  # retried as a new pod
    assert 0 < job["activeDeadlineSeconds"] <= 600
