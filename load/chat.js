// Load test: chat completions at a constant arrival rate, streamed like a real client.
// Run once against the fake LLM directly and once through the gateway; the difference
// in latency is what the gateway adds. See docs/proof/load.md.
import http from "k6/http";
import { check } from "k6";

const TARGET = __ENV.TARGET_URL; // .../v1/chat/completions
const API_KEY = __ENV.API_KEY || "";
const RPS = Number(__ENV.RPS || 50);
const DURATION = __ENV.DURATION || "60s";

export const options = {
  discardResponseBodies: false,
  scenarios: {
    chat: {
      executor: "constant-arrival-rate",
      rate: RPS,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: RPS * 2,
      maxVUs: RPS * 10,
    },
  },
  summaryTrendStats: ["p(50)", "p(90)", "p(99)", "max"],
};

const BODY = JSON.stringify({
  model: __ENV.MODEL || "fake-model",
  stream: true,
  messages: [{ role: "user", content: "Say hello." }],
});

export default function () {
  const headers = { "Content-Type": "application/json" };
  if (API_KEY) headers.Authorization = `Bearer ${API_KEY}`;
  const res = http.post(TARGET, BODY, { headers, timeout: "30s" });
  check(res, {
    "status 200": (r) => r.status === 200,
    "stream finished": (r) => typeof r.body === "string" && r.body.includes("[DONE]"),
  });
}

// One JSON line on stdout instead of k6's text summary; load/summary.py prints it.
export function handleSummary(data) {
  const m = data.metrics;
  const duration = m.http_req_duration.values;
  const firstByte = m.http_req_waiting.values;
  return {
    stdout:
      JSON.stringify({
        requests: m.http_reqs.values.count,
        failed_rate: m.http_req_failed.values.rate,
        checks_rate: m.checks.values.rate,
        dropped: m.dropped_iterations ? m.dropped_iterations.values.count : 0,
        p50_ms: duration["p(50)"],
        p90_ms: duration["p(90)"],
        p99_ms: duration["p(99)"],
        max_ms: duration.max,
        ttfb_p50_ms: firstByte["p(50)"],
        ttfb_p99_ms: firstByte["p(99)"],
      }) + "\n",
  };
}
