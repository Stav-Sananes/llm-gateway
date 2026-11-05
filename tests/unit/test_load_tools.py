"""The load-test tooling: the fake LLM's entry point and the k6 summary printer."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from harness import fake_llm

ROOT = Path(__file__).resolve().parents[2]


def test_the_fake_llm_reads_its_latency_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: dict[str, Any] = {}
    monkeypatch.setenv("FAKE_LLM_TOKENS", "3")
    monkeypatch.setenv("FAKE_LLM_TOKEN_DELAY_S", "0.25")
    monkeypatch.setenv("FAKE_LLM_PORT", "9123")
    monkeypatch.setattr(fake_llm, "create_fake_llm", lambda script: script)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.update(app=app, **kw))

    fake_llm.main()

    script = served["app"]
    assert (len(script.reply), script.token_delay_s, served["port"]) == (3, 0.25, 9123)
    assert (
        script.headers_with_first_token
    )  # so first byte means first token, as through the gateway


def test_the_summary_prints_the_served_percentiles() -> None:
    line = json.dumps(
        {"requests": 10, "failed_rate": 0.1, "checks_rate": 0.9, "dropped": 2,
         "p50_ms": 200.0, "p90_ms": 210.0, "p99_ms": 250.5, "max_ms": 300.0,
         "ttfb_p50_ms": 12.0, "ttfb_p99_ms": 20.0}
    )  # fmt: skip
    printed = subprocess.run(  # noqa: S603 - our own script, fixed arguments
        [sys.executable, str(ROOT / "load/summary.py"), "direct"],
        input="k6 noise\n" + line + "\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert printed.startswith("direct: 10 requests, 10.00% failed, 90.00% checks passed, 2 dropped")
    assert "p99 250.5 ms" in printed and "first byte:   p50 12.0 ms" in printed
