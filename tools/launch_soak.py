#!/usr/bin/env python3
"""Poll a running simulator for a soak window and fail on health regressions."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _get(url: str, timeout: float) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed = {"error": body}
            return response.status, parsed if isinstance(parsed, dict) else {"error": parsed}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"error": body}
        return exc.code, parsed if isinstance(parsed, dict) else {"error": parsed}
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 0, {"error": str(exc)}


def run_soak(*, base_url: str, seconds: int, interval: float, json_out: str = "") -> int:
    base = base_url.rstrip("/")
    samples: list[dict] = []
    deadline = time.monotonic() + seconds
    try:
        while True:
            status, payload = _get(f"{base}/api/ready", timeout=5.0)
            summary_status, summary = _get(f"{base}/api/fleet/summary", timeout=5.0)
            tick = None
            if isinstance(summary, dict):
                metrics = summary.get("metrics") or {}
                tick = metrics.get("avg_tick_ms")
            row = {
                "ts": time.time(),
                "ready_status": status,
                "ready": payload.get("ready") if isinstance(payload, dict) else False,
                "summary_status": summary_status,
                "avg_tick_ms": tick,
                "generator_count": (summary.get("summary") or {}).get("generator_count") if isinstance(summary, dict) else None,
                "error": payload.get("error") if isinstance(payload, dict) else None,
            }
            samples.append(row)
            print(f"[soak] ready={row['ready_status']} tick={tick} gens={row['generator_count']}")
            if status != 200 or not row["ready"]:
                print("[fail] readiness dropped", file=sys.stderr)
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(interval)
    finally:
        failed = [row for row in samples if row["ready_status"] != 200 or not row["ready"]]
        result = {"samples": samples, "failed": len(failed), "seconds": seconds}
        if json_out:
            out = Path(json_out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5301")
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    return run_soak(
        base_url=args.base_url,
        seconds=args.seconds,
        interval=args.interval,
        json_out=args.json_out,
    )


if __name__ == "__main__":
    raise SystemExit(main())
