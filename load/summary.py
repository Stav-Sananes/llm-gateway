"""Print a k6 run's summary line (from chat.js's handleSummary) for humans: `name: …`."""

import json
import sys


def main() -> None:
    name = sys.argv[1]
    line = next(line for line in sys.stdin if line.startswith("{"))
    s = json.loads(line)
    print(
        f"{name}: {s['requests']} requests, {s['failed_rate']:.2%} failed, "
        f"{s['checks_rate']:.2%} checks passed, {s['dropped']} dropped\n"
        f"  served answers only, whole answer: p50 {s['p50_ms']:.1f} ms, p90 {s['p90_ms']:.1f} ms, "
        f"p99 {s['p99_ms']:.1f} ms, max {s['max_ms']:.1f} ms\n"
        f"  first byte:   p50 {s['ttfb_p50_ms']:.1f} ms, p99 {s['ttfb_p99_ms']:.1f} ms"
    )


if __name__ == "__main__":
    main()
