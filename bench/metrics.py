"""Metrics over scored trials.

pass^k follows tau-bench (Yao et al., 2024, arXiv:2406.12045): for a task with n trials of which c
succeeded, the chance that k trials drawn without replacement all succeed is C(c, k) / C(n, k); pass^k is
its mean over tasks. pass^1 is the plain success rate. Intervals are Wilson 95% intervals; a paired
comparison of two configurations on the same tasks uses an exact two-sided sign test on the tasks whose
per-task success rate differs.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict


def pass_hat_k(per_task: dict[str, list[bool]], k: int) -> float | None:
    vals = []
    for results in per_task.values():
        n, c = len(results), sum(results)
        if n < k:
            continue
        vals.append(math.comb(c, k) / math.comb(n, k))
    return sum(vals) / len(vals) if vals else None


def wilson(c: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if not n:
        return None
    p = c / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def sign_test(pairs: list[tuple[float, float]]) -> dict:
    """Exact two-sided sign test on paired values (ties dropped)."""
    plus = sum(1 for a, b in pairs if b > a)
    minus = sum(1 for a, b in pairs if b < a)
    n = plus + minus
    if not n:
        return {"better": 0, "worse": 0, "p": 1.0}
    tail = sum(math.comb(n, i) for i in range(0, min(plus, minus) + 1)) / 2 ** n
    return {"better": plus, "worse": minus, "p": min(1.0, 2 * tail)}


def quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    i = (len(xs) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def summarise(rows: list[dict]) -> dict:
    """rows: one per scored trial (see report.score_trace)."""
    if not rows:
        return {}
    per_task = defaultdict(list)
    for r in rows:
        per_task[r["task"]].append(r["success"])
    n = len(rows)
    succ = sum(r["success"] for r in rows)
    calls = [c for r in rows for c in r["tool_calls"]]
    lat = [r["latency_s"] for r in rows if r["latency_s"] is not None]
    gated = [r for r in rows if r["gate"].get("auto_release") is not None]
    out = {
        "trials": n, "tasks": len(per_task), "success": succ, "success_rate": succ / n, "success_ci": wilson(succ, n),
        "pass^1": pass_hat_k(per_task, 1), "pass^2": pass_hat_k(per_task, 2), "pass^3": pass_hat_k(per_task, 3),
        "needs_human": sum(not r["gate"].get("auto_release") for r in gated),
        "needs_human_rate": (sum(not r["gate"].get("auto_release") for r in gated) / len(gated)) if gated else None,
        "silent_failures": sum(1 for r in gated if r["gate"].get("auto_release") and not r["success"]),
        "caught_failures": sum(1 for r in gated if not r["gate"].get("auto_release") and not r["success"]),
        "held_successes": sum(1 for r in gated if not r["gate"].get("auto_release") and r["success"]),
        "tool_calls": len(calls), "tool_calls_correct": sum(c["correct"] for c in calls),
        "tool_call_correct_rate": (sum(c["correct"] for c in calls) / len(calls)) if calls else None,
        "tool_calls_repeated": sum(c["repeat"] for c in calls),
        "cost_usd": sum(r["cost_usd"] for r in rows), "cost_per_trial": sum(r["cost_usd"] for r in rows) / n,
        "list_cost_per_trial": sum(r["list_cost_usd"] for r in rows) / n,
        "model_calls_per_trial": sum(r["model_calls"] for r in rows) / n,
        "latency_trials": len(lat), "latency_median_s": statistics.median(lat) if lat else None,
        "latency_p90_s": quantile(lat, 0.9),
        "errors": sum(1 for r in rows if r["error"]),
    }
    stages = {}
    for s in ("U", "P", "T", "K", "R"):
        vals = [r["stages"][s] for r in rows if r["stages"][s] is not None]
        stages[s] = {"applies": len(vals), "passed": sum(vals), "rate": (sum(vals) / len(vals)) if vals else None}
    out["stages"] = stages
    fails = [r for r in rows if not r["success"]]
    attr = defaultdict(int)
    for r in fails:
        attr[r["first_failure"]["stage"] if r["first_failure"] else "?"] += 1
    out["failed_trials"] = len(fails)
    out["first_failing_stage"] = dict(attr)
    return out


def by(rows: list[dict], key) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    return {k: summarise(v) for k, v in sorted(groups.items())}
