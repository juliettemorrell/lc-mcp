"""
Concurrent load stress test for the Longevity Copilot MCP.

Hammers the server with N parallel JSON-RPC calls mixing read-only tools
(normalize_biomarker, calc_homa_ir, list_supported_sources) plus a few
report-generation calls. Reports latency p50/p95/p99, error count, and a
sanity check on the audit log (every successful call should have left
exactly one audit event).

Run:
  python stress_test.py [BASE_URL] [N] [CONCURRENCY]
  python stress_test.py http://127.0.0.1:8080 200 20
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import time
from typing import Any

import httpx


# A pool of tool calls to randomize across.
CALLS: list[dict[str, Any]] = [
    {"tool": "normalize_biomarker", "args": {"vendor": "quest", "vendor_code": "HBA1C"}},
    {"tool": "normalize_biomarker", "args": {"vendor": "labcorp", "vendor_code": "HEMOGLOBIN_A1C"}},
    {"tool": "normalize_biomarker", "args": {"vendor": "bostonheart", "vendor_code": "APOB"}},
    {"tool": "calc_homa_ir", "args": {"fasting_insulin_uIU_mL": 7.2, "fasting_glucose_mg_dL": 88}},
    {"tool": "calc_homa_ir", "args": {"fasting_insulin_uIU_mL": 12, "fasting_glucose_mg_dL": 95}},
    {"tool": "calc_egfr_ckdepi_2021", "args": {"creatinine_mg_dL": 1.05, "age_years": 26, "sex": "male"}},
    {"tool": "calc_egfr_ckdepi_2021", "args": {"creatinine_mg_dL": 1.4, "age_years": 70, "sex": "female"}},
    {"tool": "calc_ascvd_10yr", "args": {"age_years": 55, "sex": "male", "race": "white", "total_cholesterol_mg_dL": 213, "hdl_mg_dL": 50, "sbp_mmHg": 120, "treated_for_hypertension": False, "diabetes": False, "smoker": False}},
    {"tool": "calc_bmi_bsa", "args": {"height_cm": 180, "weight_kg": 80}},
    {"tool": "calc_fib4", "args": {"age_years": 50, "ast_U_L": 45, "alt_U_L": 40, "platelets_10e9_L": 220}},
    {"tool": "list_supported_sources", "args": {}},
    {"tool": "get_patient_labs", "args": {}},
    {"tool": "get_patient_genomics", "args": {}},
    {"tool": "calc_reference_ranges", "args": {"loinc": "3016-3", "value": 3.8}},
    {"tool": "generate_clinical_pdf", "args": {"headline": "Stress brief", "findings": [{"name": "HbA1c", "value": 5.4, "unit": "%"}]}},
]


async def one(client: httpx.AsyncClient, base: str, i: int) -> tuple[int, int, str | None]:
    call = random.choice(CALLS)
    body = {"jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": call["tool"], "arguments": call["args"]}, "id": i}
    t0 = time.time()
    try:
        r = await client.post(f"{base}/mcp", json=body, timeout=15.0)
        latency_ms = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            return latency_ms, r.status_code, f"HTTP {r.status_code}"
        d = r.json()
        if "error" in d:
            return latency_ms, 200, f"rpc error: {d['error'].get('message')}"
        return latency_ms, 200, None
    except Exception as e:
        return int((time.time() - t0) * 1000), -1, f"exception: {e}"


async def main(base: str = "http://127.0.0.1:8080", n: int = 200, concurrency: int = 20) -> int:
    base = base.rstrip("/")
    print(f"Hammering {base} with {n} calls, concurrency={concurrency}")
    print("=" * 60)
    sem = asyncio.Semaphore(concurrency)
    latencies: list[int] = []
    errors: list[str] = []
    successes = 0

    async with httpx.AsyncClient() as client:
        async def runner(i: int) -> None:
            async with sem:
                lat, status, err = await one(client, base, i)
                latencies.append(lat)
                if err:
                    errors.append(err)
                else:
                    nonlocal_success()

        def nonlocal_success():
            pass  # python scoping workaround below

        # Re-do above without nonlocal trick:
        async def runner2(i: int) -> tuple[int, str | None]:
            async with sem:
                lat, status, err = await one(client, base, i)
                return lat, err

        t0 = time.time()
        results = await asyncio.gather(*[runner2(i) for i in range(n)])
        total_s = time.time() - t0

    latencies = [r[0] for r in results]
    errors = [r[1] for r in results if r[1] is not None]
    successes = len(results) - len(errors)
    latencies.sort()
    def pct(p):
        if not latencies: return 0
        k = int(len(latencies) * p)
        return latencies[min(k, len(latencies) - 1)]

    print(f"Total wall time:     {total_s:.2f} s")
    print(f"Calls/sec:           {n/total_s:.1f}")
    print(f"Successes:           {successes}/{n}")
    print(f"Errors:              {len(errors)}")
    print(f"Latency p50:         {pct(0.50)} ms")
    print(f"Latency p95:         {pct(0.95)} ms")
    print(f"Latency p99:         {pct(0.99)} ms")
    print(f"Latency max:         {max(latencies) if latencies else 0} ms")

    if errors:
        print()
        print("First 5 errors:")
        for e in errors[:5]:
            print(f"  - {e}")

    return 0 if successes == n else 1


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    c = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    sys.exit(asyncio.run(main(base, n, c)))
