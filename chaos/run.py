"""Chaos runs against the compose stack: break one dependency, check the stated hypothesis.

    uv run python chaos/run.py [scenario ...]      (all of them by default)

Each scenario injects a failure (docker compose stop / pause), probes the gateway, restores
the dependency and checks that the gateway recovers. It prints one Markdown table row per
scenario: the hypothesis, what was observed, and whether it held. Exit status 1 if any
hypothesis failed. See docs/design/14-chaos.md.
"""

import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
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
BREAKER_THRESHOLD, BREAKER_RESET_S = 3, 5  # as in chaos/compose.chaos.yaml
# Well formed, so it passes the shape check and is looked up: unknown to every store.
UNKNOWN_KEY = "gw_00000000_" + "x" * 43


def compose(*args: str) -> str:
    result = subprocess.run([*COMPOSE, *args], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"compose {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def psql(sql: str) -> str:
    return compose("exec", "-T", "postgres", "psql", "-U", "gateway", "-d", "gateway", "-tAc", sql)


def usage_rows() -> int:
    return int(psql("SELECT count(*) FROM usage_events") or 0)


def worker_restarts() -> int:
    container = compose("ps", "-q", "usage-worker")
    inspected = subprocess.run(
        ["docker", "inspect", "-f", "{{.RestartCount}}", container],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return int(inspected.stdout.strip())


def metric(name: str, **labels: str) -> float:
    return sample(httpx.get(f"{GATEWAY}/metrics", timeout=5).text, name, **labels)


def sample(exposition: str, name: str, **labels: str) -> float:
    """The value of one sample in Prometheus text format: exactly these labels, in any
    order. 0 when absent, as a counter that hasn't been incremented yet."""
    wanted = sorted(f'{key}="{value}"' for key, value in labels.items())
    for line in exposition.splitlines():
        match = re.fullmatch(rf"{re.escape(name)}(?:\{{(.*)\}})? (\S+)", line)
        if match and sorted(filter(None, (match.group(1) or "").split(","))) == wanted:
            return float(match.group(2))
    return 0.0


def wait_until(check: Callable[[], bool], timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (httpx.HTTPError, RuntimeError):
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
        error = "" if response.is_success else response.json().get("error", {}).get("message", "")
        return Answer(response.status_code, seconds, error)

    def asks(self, n: int, question: str = "hi", **extra: object) -> list[Answer]:
        return [self.ask(question, **extra) for _ in range(n)]


def new_key(tenant: str) -> str:
    compose("exec", "-T", "gateway", "llm-gateway", "admin", "create-tenant", tenant)
    return compose("exec", "-T", "gateway", "llm-gateway", "admin", "create-key", tenant)


@dataclass
class Outcome:
    name: str
    hypothesis: str
    observed: str
    held: bool


def circuit(provider: str) -> float:
    return metric("gateway_circuit_state", provider=provider)


def served_by(provider: str) -> float:
    return metric("gateway_chat_requests_total", provider=provider, outcome="completed")


def provider_killed(client: Client) -> Outcome:
    hypothesis = (
        "Fake Claude stopped: every `smart` request is still answered (by the local model); "
        f"after {BREAKER_THRESHOLD} failures its circuit opens; /readyz stays ready; "
        "once it's back, the circuit closes and it serves again."
    )
    local_before = served_by("local")
    compose("stop", "fake-anthropic")
    answers = client.asks(10)
    opened, ready = circuit("anthropic") == 2, httpx.get(f"{GATEWAY}/readyz").status_code
    local = served_by("local") - local_before
    compose("start", "fake-anthropic")
    time.sleep(BREAKER_RESET_S + 0.5)
    claude_before = served_by("anthropic")
    wait_until(lambda: client.ask().status == 200 and served_by("anthropic") > claude_before,
               15, "fake Claude to serve again")  # fmt: skip
    closed = circuit("anthropic") == 0
    statuses = {a.status for a in answers}
    observed = (
        f"statuses {sorted(statuses)}, {local:.0f}/10 served by local, circuit open: {opened}, "
        f"/readyz {ready}; after restart: serving again, circuit closed: {closed}"
    )
    held = statuses == {200} and local == 10 and opened and ready == 200 and closed
    return Outcome("provider killed", hypothesis, observed, held)


def provider_slow(client: Client) -> Outcome:
    hypothesis = (
        "Fake Claude frozen (accepts connections, never answers): requests are still answered; "
        f"only the first {BREAKER_THRESHOLD} wait out the 2 s read timeout, then the circuit "
        "opens and the rest go straight to the local model."
    )
    compose("pause", "fake-anthropic")
    try:
        answers = client.asks(8)
    finally:
        compose("unpause", "fake-anthropic")
    slow = sum(a.seconds > 1.5 for a in answers)
    statuses = {a.status for a in answers}
    fast_tail = max(a.seconds for a in answers[BREAKER_THRESHOLD:])
    time.sleep(BREAKER_RESET_S + 0.5)
    wait_until(lambda: client.ask().status == 200 and circuit("anthropic") == 0, 15, "recovery")
    observed = (
        f"statuses {sorted(statuses)}, {slow} of 8 took over 1.5 s, the rest at most "
        f"{fast_tail:.2f} s; circuit closed again after unpausing"
    )
    held = statuses == {200} and slow <= BREAKER_THRESHOLD and fast_tail < 1
    return Outcome("provider slow", hypothesis, observed, held)


def redis_down(client: Client) -> Outcome:
    hypothesis = (
        "Redis stopped: auth falls back to Postgres (a valid key still authenticates, a bad "
        "one is still refused); the budget fails closed with 503; nothing hangs (each "
        "response within 1 s); with Redis back, requests are answered again."
    )
    compose("stop", "redis")
    try:
        answers = client.asks(5)
        bad = Client(UNKNOWN_KEY).ask()
    finally:
        compose("start", "redis")
    wait_until(lambda: client.ask().status == 200, 20, "requests to succeed again")
    statuses = {a.status for a in answers}
    errors = {a.error for a in answers}
    slowest = max(a.seconds for a in answers)
    observed = (
        f"valid key: statuses {sorted(statuses)} ({' / '.join(sorted(errors))}), slowest "
        f"{slowest:.2f} s; unknown key: {bad.status}; answered again once Redis was back"
    )
    held = (
        statuses == {503}
        and errors == {"Usage limits are temporarily unavailable."}
        and bad.status == 401
        and slowest < 1
    )
    return Outcome("Redis down", hypothesis, observed, held)


def postgres_down(client: Client, fresh_key: str) -> Outcome:
    hypothesis = (
        "Postgres stopped: a key used before is still served from the Redis key cache; a key "
        "never used fails closed (503, not 401); the usage worker waits, and once Postgres is "
        "back every usage row arrives, none lost."
    )
    rows_before = usage_rows()
    client.ask()  # caches the key in Redis
    wait_until(lambda: usage_rows() > rows_before, 30, "the warm-up's usage row")
    rows_before = usage_rows()
    restarts_before = worker_restarts()
    compose("stop", "postgres")
    try:
        answers = client.asks(5)
        fresh = Client(fresh_key).ask()
    finally:
        compose("start", "postgres")
    statuses = {a.status for a in answers}
    wait_until(lambda: usage_rows() >= rows_before + 5, 60, "usage rows to arrive")
    time.sleep(2)  # and no duplicates after them
    arrived = usage_rows() - rows_before
    restarts = worker_restarts() - restarts_before
    observed = (
        f"cached key: statuses {sorted(statuses)}; never-used key: {fresh.status} "
        f"({fresh.error}); {arrived} usage rows arrived after Postgres came back (5 expected); "
        f"usage worker restarts: {restarts}"
    )
    held = statuses == {200} and fresh.status == 503 and arrived == 5
    return Outcome("Postgres down", hypothesis, observed, held)


def qdrant_down(client: Client) -> Outcome:
    hypothesis = (
        "Qdrant stopped: the semantic cache is bypassed and every request is still answered "
        "by a model; lookups count as `unavailable`; once Qdrant is back, answers are cached "
        "and hit again."
    )
    question = "What is the capital of France?"
    unavailable_before = metric("gateway_cache_lookups_total", result="unavailable")
    compose("stop", "qdrant")
    try:
        answers = client.asks(6, question, temperature=0)
    finally:
        compose("start", "qdrant")
    unavailable = metric("gateway_cache_lookups_total", result="unavailable") - unavailable_before
    hits_before = metric("gateway_cache_lookups_total", result="hit")

    def hits_again() -> bool:
        client.ask(question, temperature=0)
        time.sleep(0.3)  # the answer is stored in the background
        return metric("gateway_cache_lookups_total", result="hit") > hits_before

    wait_until(hits_again, 30, "cache hits")
    statuses = {a.status for a in answers}
    observed = (
        f"statuses {sorted(statuses)}, {unavailable:.0f} lookups unavailable; "
        "hits again once Qdrant was back"
    )
    held = statuses == {200} and unavailable == 6
    return Outcome("Qdrant down", hypothesis, observed, held)


def main() -> int:
    compose(
        "up", "-d", "--build", "--wait", "gateway", "usage-worker", "fake-llm", "fake-anthropic"
    )
    stamp = int(time.time())
    client = Client(new_key(f"chaos-{stamp}"))
    fresh_key = new_key(f"chaos-fresh-{stamp}")
    wait_until(lambda: client.ask().status == 200, 30, "the stack to answer")
    scenarios: dict[str, Callable[[], Outcome]] = {
        "provider-killed": lambda: provider_killed(client),
        "provider-slow": lambda: provider_slow(client),
        "redis-down": lambda: redis_down(client),
        "postgres-down": lambda: postgres_down(client, fresh_key),
        "qdrant-down": lambda: qdrant_down(client),
    }
    chosen = sys.argv[1:] or list(scenarios)
    outcomes = []
    for name in chosen:
        print(f"--- {name}", file=sys.stderr)
        outcomes.append(scenarios[name]())
    print("| Scenario | Hypothesis | Observed | Held |\n|---|---|---|---|")
    for o in outcomes:
        print(f"| {o.name} | {o.hypothesis} | {o.observed} | {'yes' if o.held else '**no**'} |")
    return 0 if all(o.held for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
