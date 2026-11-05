"""The Helm chart, rendered by the real helm binary (in its container image).

These check what the chart promises an operator: what's exposed, how pods run and stop,
and when migrations run. The kind smoke test (deploy/kind/smoke-test.sh) checks that the
rendered chart actually works on a cluster."""

import subprocess
from collections.abc import Callable
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


@pytest.fixture(scope="module")
def render() -> Callable[..., list[Manifest]]:
    def rendered(**extra: Any) -> list[Manifest]:
        done = helm("template", "gw", "llm-gateway", values=merged(REQUIRED, extra))
        assert done.returncode == 0, done.stderr
        return [doc for doc in yaml.safe_load_all(done.stdout) if doc]

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


def test_the_gateway_gets_time_to_close_held_calls_before_it_is_killed(
    render: Callable[..., list[Manifest]],
) -> None:
    manifests = render(gateway={"shutdownTimeoutSeconds": 40, "preStopSleepSeconds": 7})
    spec = pod_specs(manifests)["gw-llm-gateway"]
    config = find(manifests, "ConfigMap", "gw-llm-gateway-config")["data"]

    assert config["GATEWAY_SHUTDOWN_TIMEOUT_S"] == "40"
    assert spec["containers"][0]["lifecycle"]["preStop"]["exec"]["command"] == ["sleep", "7"]
    assert spec["terminationGracePeriodSeconds"] > 40 + 7


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


def test_the_migration_secret_is_removed_even_when_the_migration_fails(
    render: Callable[..., list[Manifest]],
) -> None:
    secret = find(render(), "Secret", "gw-llm-gateway-migrate")

    # It holds the credentials of a role allowed to change the schema.
    assert "hook-failed" in secret["metadata"]["annotations"]["helm.sh/hook-delete-policy"]


def test_a_failed_migration_pod_is_never_replaced_while_it_still_runs(
    render: Callable[..., list[Manifest]],
) -> None:
    job = find(render(), "Job", "gw-llm-gateway-migrate")

    assert job["spec"]["podReplacementPolicy"] == "Failed"  # never two migrations at once


def test_no_hash_of_the_secret_is_put_in_the_pod_spec(
    render: Callable[..., list[Manifest]],
) -> None:
    # Anyone who can read pods could test password guesses against it offline.
    for name, deployment in [
        (m["metadata"]["name"], m) for m in render() if m["kind"] == "Deployment"
    ]:
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


def test_a_setting_the_chart_owns_is_refused_in_config() -> None:
    # Two GATEWAY_PORT keys would make the ConfigMap invalid, or silently drop one.
    done = helm("template", "gw", "llm-gateway", values=merged(REQUIRED, {"config": {"PORT": "9"}}))

    assert done.returncode != 0
    assert "config.PORT is set by the chart" in done.stderr
