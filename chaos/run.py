"""Chaos runs against the compose stack: break one dependency, check the stated hypothesis.

    uv run python chaos/run.py [scenario ...]      (all of them by default)

Each scenario injects a failure (docker compose stop, pause, or replacing a container),
probes the gateway, restores the dependency in a `finally` and checks that the gateway
recovers. It prints one Markdown table row per scenario: the hypothesis, what was observed,
and whether it held. A scenario that errors out is a row that didn't hold, and the others
still run. Exit status 1 if any hypothesis failed. See docs/design/14-chaos.md.
"""

import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "gwload"  # the proof stack, shared with load/run.sh
COMPOSE = [
    "docker",
    "compose",
    "-p",
    PROJECT,
    "-f",
    "compose.yaml",
    "-f",
    "chaos/compose.chaos.yaml",
]
GATEWAY = "http://localhost:8080"
# As in chaos/compose.chaos.yaml.
BREAKER_THRESHOLD, BREAKER_RESET_S = 3, 5
READ_TIMEOUT_S = 2
KEY_FRESH_S = 5
CACHE_DEADLINE_S = 1  # GATEWAY_CACHE_TIMEOUT_S: embedding and search together
# Well formed, so it passes the shape check and is looked up: unknown to every store.
UNKNOWN_KEY = "gw_00000000_" + "x" * 43
OPEN, CLOSED = 2, 0  # gateway_circuit_state


def compose(*args: str) -> str:
    result = subprocess.run([*COMPOSE, *args], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"compose {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


@contextmanager
def broken(*how: str) -> Iterator[None]:
    """`stop <service>` or `pause <service>` for the duration of the block, undone even if
    the block fails."""
    action, service = how
    compose(action, service)
    try:
        yield
    finally:
        compose({"stop": "start", "pause": "unpause"}[action], service)


def psql(sql: str) -> str:
    return compose("exec", "-T", "postgres", "psql", "-U", "gateway", "-d", "gateway", "-tAc", sql)


def usage_rows(tenant: str) -> int:
    """Usage rows of one tenant, so another scenario's late rows can't be counted."""
    if not re.fullmatch(r"[a-z0-9-]+", tenant):  # our own generated names, quoted below
        raise ValueError(f"unexpected tenant name {tenant!r}")
    joined = "usage_events u JOIN tenants t ON t.id = u.tenant_id"
    query = f"SELECT count(*) FROM {joined} WHERE t.name = '{tenant}'"  # noqa: S608 - validated
    return int(psql(query) or 0)


def worker_restarts() -> int:
    container = compose("ps", "-q", "usage-worker")
    inspected = subprocess.run(
        ["docker", "inspect", "-f", "{{.RestartCount}}", container],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return int(inspected.stdout.strip())


def metric(name: str, required: bool = False, **labels: str) -> float:
    exposition = httpx.get(f"{GATEWAY}/metrics", timeout=5).text
    return sample(exposition, name, required=required, **labels)


class MissingSampleError(Exception):
    pass


def sample(exposition: str, name: str, required: bool = False, **labels: str) -> float:
    """The value of one sample in Prometheus text format: exactly these labels, in any
    order. Absent, it reads 0 (a counter that hasn't been incremented yet), unless it is
    required: a gauge that's missing must not read as "closed"."""
    wanted = sorted(f'{key}="{value}"' for key, value in labels.items())
    for line in exposition.splitlines():
        match = re.fullmatch(rf"{re.escape(name)}(?:\{{(.*)\}})? (\S+)", line)
        if match and sorted(filter(None, (match.group(1) or "").split(","))) == wanted:
            return float(match.group(2))
    if required:
        raise MissingSampleError(f"no sample {name}{labels}")
    return 0.0


def circuit(provider: str) -> float:
    return metric("gateway_circuit_state", required=True, provider=provider)


def served_by(provider: str) -> float:
    return metric("gateway_chat_requests_total", provider=provider, outcome="completed")


def wait_until(check: Callable[[], bool], timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (httpx.HTTPError, RuntimeError, ValueError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {what}")


@dataclass
class Answer:
    status: int
    seconds: float
    error: str = ""  # the error message, for a refusal


class Client:
    def __init__(self, key: str) -> None:
        self._http = httpx.Client(
            base_url=GATEWAY, headers={"Authorization": f"Bearer {key}"}, timeout=30
        )

    def ask(self, question: str = "hi", **extra: object) -> Answer:
        body = {"model": "smart", "messages": [{"role": "user", "content": question}]} | extra
        started = time.monotonic()
        response = self._http.post("/v1/chat/completions", json=body)
        seconds = time.monotonic() - started
        return Answer(response.status_code, seconds, _error_message(response))

    def asks(self, n: int, question: str = "hi", **extra: object) -> list[Answer]:
        return [self.ask(question, **extra) for _ in range(n)]


def _error_message(response: httpx.Response) -> str:
    if response.is_success:
        return ""
    try:
        return str(response.json().get("error", {}).get("message", ""))
    except ValueError:
        return response.text[:80]


def new_key(tenant: str) -> str:
    compose("exec", "-T", "gateway", "llm-gateway", "admin", "create-tenant", tenant)
    return compose("exec", "-T", "gateway", "llm-gateway", "admin", "create-key", tenant)


@dataclass
class Tenant:
    name: str
    client: Client

    @classmethod
    def new(cls, name: str) -> "Tenant":
        return cls(name, Client(new_key(name)))


@dataclass
class Verdict:
    observed: str
    held: bool


@dataclass
class Scenario:
    name: str
    hypothesis: str
    run: Callable[[], Verdict]


def statuses(answers: list[Answer]) -> list[int]:
    return sorted({a.status for a in answers})


def recovered(client: Client) -> None:
    wait_until(lambda: client.ask().status == 200, 30, "requests to be answered again")


def provider_killed(client: Client) -> Verdict:
    local_before = served_by("local")
    with broken("stop", "fake-anthropic"):
        first = client.asks(BREAKER_THRESHOLD - 1)
        closed_before_threshold = circuit("anthropic") == CLOSED
        first += client.asks(1)
        opened_at_threshold = circuit("anthropic") == OPEN
        rest = client.asks(10 - BREAKER_THRESHOLD)
        ready = httpx.get(f"{GATEWAY}/readyz", timeout=5).status_code
    local = served_by("local") - local_before
    time.sleep(BREAKER_RESET_S + 0.5)
    claude_before = served_by("anthropic")
    wait_until(lambda: client.ask().status == 200 and served_by("anthropic") > claude_before,
               15, "fake Claude to serve again")  # fmt: skip
    answers = first + rest
    return Verdict(
        f"statuses {statuses(answers)}, {local:.0f}/10 served by local; circuit closed after "
        f"{BREAKER_THRESHOLD - 1} failures: {closed_before_threshold}, open after "
        f"{BREAKER_THRESHOLD}: {opened_at_threshold}; /readyz {ready}; after restart: "
        "serving again",
        statuses(answers) == [200]
        and local == 10
        and closed_before_threshold
        and opened_at_threshold
        and ready == 200,
    )


def provider_slow(client: Client) -> Verdict:
    local_before = served_by("local")
    with broken("pause", "fake-anthropic"):
        answers = client.asks(8)
        opened = circuit("anthropic") == OPEN  # read while still frozen
    local = served_by("local") - local_before
    slow = sum(a.seconds > READ_TIMEOUT_S * 0.75 for a in answers)
    fast_tail = max(a.seconds for a in answers[BREAKER_THRESHOLD:])
    slowest = max(a.seconds for a in answers)
    time.sleep(BREAKER_RESET_S + 0.5)
    wait_until(lambda: client.ask().status == 200 and circuit("anthropic") == CLOSED,
               15, "recovery")  # fmt: skip
    return Verdict(
        f"statuses {statuses(answers)}, {local:.0f}/8 served by local, {slow} of 8 took over "
        f"{READ_TIMEOUT_S * 0.75:.1f} s (slowest {slowest:.2f} s), the rest at most "
        f"{fast_tail:.2f} s; circuit open while frozen: {opened}; closed again after "
        "unpausing",
        statuses(answers) == [200]
        and local == 8
        and slow == BREAKER_THRESHOLD  # 0 would mean the fault never took effect
        and slowest < READ_TIMEOUT_S + 1  # one timeout each: no retries, no longer wait
        and opened
        and fast_tail < 1,
    )


def provider_slow_under_load(key: str) -> Verdict:
    concurrent = 30

    def ask(_: int) -> Answer:
        return Client(key).ask()

    with broken("pause", "fake-anthropic"), ThreadPoolExecutor(concurrent) as pool:
        answers = list(pool.map(ask, range(concurrent)))
        opened = circuit("anthropic") == OPEN
        after = Client(key).asks(5)
    slowest = max(a.seconds for a in answers)
    slowest_after = max(a.seconds for a in after)
    time.sleep(BREAKER_RESET_S + 0.5)
    recovered(Client(key))
    return Verdict(
        f"{concurrent} at once: statuses {statuses(answers)}, "
        f"{sum(a.seconds > READ_TIMEOUT_S * 0.75 for a in answers)} waited out the timeout, "
        f"slowest {slowest:.2f} s; circuit open: {opened}; the next 5: statuses "
        f"{statuses(after)}, slowest {slowest_after:.2f} s",
        statuses(answers) == [200]
        and slowest < READ_TIMEOUT_S + 1.5
        and opened
        and statuses(after) == [200]
        and slowest_after < 1,
    )


def redis_down(client: Client) -> Verdict:
    with broken("stop", "redis"):
        answers = client.asks(5)
        unknown = Client(UNKNOWN_KEY).ask()
    recovered(client)
    return _redis_verdict(answers, unknown)


def redis_frozen(client: Client) -> Verdict:
    with broken("pause", "redis"):
        answers = client.asks(5)
        unknown = Client(UNKNOWN_KEY).ask()
    recovered(client)
    return _redis_verdict(answers, unknown)


def _redis_verdict(answers: list[Answer], unknown: Answer) -> Verdict:
    errors = sorted({a.error for a in answers})
    slowest = max(a.seconds for a in [*answers, unknown])
    return Verdict(
        f"valid key: statuses {statuses(answers)} ({' / '.join(errors)}); unknown key: "
        f"{unknown.status}; slowest {slowest:.2f} s; answered again once Redis was back",
        statuses(answers) == [503]
        and errors == ["Usage limits are temporarily unavailable."]
        and unknown.status == 401
        and slowest < 1,
    )


def postgres_down(tenant: Tenant, never_used_key: str) -> Verdict:
    before = usage_rows(tenant.name)
    tenant.client.ask()  # confirms the key, so the cache holds it
    wait_until(lambda: usage_rows(tenant.name) > before, 30, "the warm-up's usage row")
    before = usage_rows(tenant.name)
    restarts_before = worker_restarts()
    with broken("stop", "postgres"):
        answers = tenant.client.asks(2)
        time.sleep(KEY_FRESH_S + 1)  # past the key cache's fresh window
        answers += tenant.client.asks(3)
        never_used = Client(never_used_key).ask()
    answered = sum(a.status == 200 for a in answers)  # each answer is one usage row
    wait_until(lambda: usage_rows(tenant.name) >= before + answered, 60, "usage rows")
    arrived = usage_rows(tenant.name) - before
    restarts = worker_restarts() - restarts_before
    return Verdict(
        f"confirmed key: statuses {[a.status for a in answers]} (the last 3 after its cache "
        "entry went stale); "
        f"never-used key: {never_used.status} ({never_used.error}); {arrived} usage rows "
        f"arrived after Postgres came back ({answered} answered); usage worker restarts: "
        f"{restarts}",
        statuses(answers) == [200] and never_used.status == 503 and arrived == 5 and restarts == 0,
    )


def postgres_frozen(client: Client, never_used_key: str) -> Verdict:
    client.ask()  # confirms the key
    with broken("pause", "postgres"):
        answers = client.asks(3)
        never_used = Client(never_used_key).ask()
    recovered(client)
    slowest = max(a.seconds for a in answers)
    return Verdict(
        f"confirmed key: statuses {statuses(answers)}, slowest {slowest:.2f} s; never-used "
        f"key: {never_used.status} after {never_used.seconds:.1f} s",
        statuses(answers) == [200]
        and slowest < 1
        and never_used.status == 503
        and never_used.seconds < 8,
    )


CAPITAL = "What is the capital of France?"


def cache_lookups(result: str) -> float:
    return metric("gateway_cache_lookups_total", result=result)


def hits_again(client: Client, timeout_s: float) -> None:
    hits_before = cache_lookups("hit")

    def hit() -> bool:
        client.ask(CAPITAL, temperature=0)
        time.sleep(0.3)  # the answer is stored in the background
        return cache_lookups("hit") > hits_before

    wait_until(hit, timeout_s, "cache hits")


def qdrant_down(client: Client) -> Verdict:
    unavailable_before = cache_lookups("unavailable")
    with broken("stop", "qdrant"):
        answers = client.asks(6, CAPITAL, temperature=0)
    unavailable = cache_lookups("unavailable") - unavailable_before
    hits_again(client, 30)
    return Verdict(
        f"statuses {statuses(answers)}, {unavailable:.0f} lookups unavailable; hits again "
        "once Qdrant was back",
        statuses(answers) == [200] and unavailable == 6,
    )


def qdrant_frozen(client: Client) -> Verdict:
    unavailable_before = cache_lookups("unavailable")
    with broken("pause", "qdrant"):
        answers = client.asks(6, CAPITAL, temperature=0)
    unavailable = cache_lookups("unavailable") - unavailable_before
    slowest = max(a.seconds for a in answers)
    fast_tail = max(a.seconds for a in answers[BREAKER_THRESHOLD:])
    hits_again(client, 60)  # the cache's breaker may be open: it tries again after 5 s
    return Verdict(
        f"statuses {statuses(answers)}, {unavailable:.0f} lookups unavailable; slowest "
        f"{slowest:.2f} s, after the first {BREAKER_THRESHOLD} at most {fast_tail:.2f} s; "
        "hits again once Qdrant was back",
        statuses(answers) == [200]
        and unavailable == 6
        and slowest < CACHE_DEADLINE_S + 0.5
        and fast_tail < 0.5,
    )


def qdrant_replaced(client: Client) -> Verdict:
    compose("rm", "-sf", "qdrant")  # Qdrant has no volume: it comes back empty
    try:
        answers = client.asks(3, CAPITAL, temperature=0)
    finally:
        compose("up", "-d", "--wait", "qdrant")
    answers += client.asks(3, CAPITAL, temperature=0)
    collections = httpx.get("http://localhost:6333/collections", timeout=5, headers=_qdrant())
    empty = collections.json()["result"]["collections"] == []
    hits_again(client, 60)  # the cache's own breaker may be open: reset 30 s by default
    return Verdict(
        f"statuses {statuses(answers)}; Qdrant came back empty: {empty}; the collection was "
        "created again and hits returned, without restarting the gateway",
        statuses(answers) == [200] and empty,
    )


def _qdrant() -> dict[str, str]:
    key = compose("exec", "-T", "gateway", "printenv", "GATEWAY_QDRANT_API_KEY")
    return {"api-key": key}


class Tenants:
    """The run's tenants and keys, created on first use: listing the scenarios (to check
    the names asked for) touches nothing."""

    def __init__(self) -> None:
        self._stamp = int(time.time())

    @cached_property
    def main(self) -> Tenant:
        tenant = Tenant.new(f"chaos-{self._stamp}")
        recovered(tenant.client)
        return tenant

    @cached_property
    def outage(self) -> Tenant:
        return Tenant.new(f"chaos-pg-{self._stamp}")

    def never_used_key(self, label: str) -> str:
        return new_key(f"chaos-never-{label}-{self._stamp}")

    @cached_property
    def load_key(self) -> str:
        return new_key(f"chaos-load-{self._stamp}")


def scenarios(tenants: Tenants) -> list[Scenario]:
    t, r = BREAKER_THRESHOLD, READ_TIMEOUT_S
    return [
        Scenario(
            "provider killed",
            "Fake Claude stopped: every `smart` request is still answered (by the local model); "
            f"its circuit opens at failure {t}, not before; /readyz stays ready; once "
            "it's back, the circuit closes and it serves again.",
            lambda: provider_killed(tenants.main.client),
        ),
        Scenario(
            "provider slow",
            "Fake Claude frozen (accepts connections, never answers), requests one at a time: "
            f"all are answered by the local model; exactly the first {t} wait out the {r} s "
            "read timeout; the circuit opens and the rest go straight to the local model.",
            lambda: provider_slow(tenants.main.client),
        ),
        Scenario(
            "provider slow, under load",
            "Fake Claude frozen, 30 requests at once: all are answered; every request already "
            f"in flight waits out one read timeout ({r} s), none longer; then the circuit is "
            "open and new requests are fast.",
            lambda: provider_slow_under_load(tenants.load_key),
        ),
        Scenario(
            "Redis down",
            "Redis stopped: the budget fails closed with 503; an unknown key is still refused "
            "(auth falls back to Postgres); each response within 1 s; answered again once "
            "Redis is back.",
            lambda: redis_down(tenants.main.client),
        ),
        Scenario(
            "Redis frozen",
            "Redis paused (connections hang): the same as stopped, within 1 s per request: "
            "each Redis call waits at most its 0.25 s timeout.",
            lambda: redis_frozen(tenants.main.client),
        ),
        Scenario(
            "Postgres down",
            "Postgres stopped, for longer than the key cache's fresh window: a key confirmed "
            "before is still served; a key never used fails closed (503, not 401); the usage "
            "worker waits without restarting, and once Postgres is back every usage row "
            "arrives, none lost.",
            lambda: postgres_down(tenants.outage, tenants.never_used_key("pg-down")),
        ),
        Scenario(
            "Postgres frozen",
            "Postgres paused (connections hang): a confirmed key is served without waiting on "
            "it; a never-used key gets 503 within 8 s (statement timeout, then connect "
            "timeout).",
            lambda: postgres_frozen(tenants.main.client, tenants.never_used_key("pg-frozen")),
        ),
        Scenario(
            "Qdrant down",
            "Qdrant stopped: the semantic cache is bypassed and every request is still "
            "answered by a model; lookups count as `unavailable`; once Qdrant is back, "
            "answers are cached and hit again.",
            lambda: qdrant_down(tenants.main.client),
        ),
        Scenario(
            "Qdrant frozen",
            "Qdrant paused (connections hang): every request is still answered, none waits "
            "more than the cache's 1 s deadline; after 3 failures the cache's breaker skips "
            "Qdrant at once; once it's back, answers hit again.",
            lambda: qdrant_frozen(tenants.main.client),
        ),
        Scenario(
            "Qdrant replaced",
            "Qdrant's container replaced (it has no volume, so it comes back empty): requests "
            "are answered throughout, and the cache hits again without a gateway restart.",
            lambda: qdrant_replaced(tenants.main.client),
        ),
    ]


def slug(name: str) -> str:
    return re.sub(r"[^a-z]+", "-", name.lower()).strip("-")


def main() -> int:
    # SIGTERM becomes SystemExit, so the `finally` blocks restore what they broke.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    every = scenarios(Tenants())
    chosen = set(sys.argv[1:]) or {slug(scenario.name) for scenario in every}
    unknown = chosen - {slug(scenario.name) for scenario in every}
    if unknown:
        print(f"unknown scenarios: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    compose("up", "-d", "--build", "--wait", "gateway", "usage-worker")
    rows = []
    for scenario in every:
        if slug(scenario.name) not in chosen:
            continue
        print(f"--- {scenario.name}", file=sys.stderr)
        try:
            verdict = scenario.run()
        except Exception as error:  # a failed recovery is a result too
            verdict = Verdict(f"**error:** {error}", held=False)
        rows.append((scenario, verdict))
    print("| Scenario | Hypothesis | Observed | Held |\n|---|---|---|---|")
    for scenario, verdict in rows:
        held = "yes" if verdict.held else "**no**"
        print(f"| {scenario.name} | {scenario.hypothesis} | {verdict.observed} | {held} |")
    return 0 if rows and all(v.held for _, v in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
