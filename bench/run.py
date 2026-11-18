"""Run trials and store their traces.

  python -m bench run --config base --trials 1 --no-batch       standard API: latency is measured
  python -m bench run --config base --trials 2-3                 Message Batches: half price, no latency

A run is a folder eval/results/<config>/ with run.json (settings, offered tools, prompt hashes, batches,
spend) and traces.jsonl (one line per trial). A trial already stored without an error is not run again,
so an interrupted run carries on where it stopped.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import yaml

from . import config, llm, trace
from .adapters import ad_review, biz, creator

ADAPTERS = {"ad_review": ad_review, "biz": biz, "creator": creator}


def load_configs() -> dict:
    return yaml.safe_load((config.EVAL / "configs.yaml").read_text(encoding="utf-8"))["configs"]


def trial_range(spec: str) -> list[int]:
    if "-" in spec:
        a, b = spec.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in spec.split(",")]


def run(config_name: str, trials: str = "1-3", families: str = "", tasks: str = "", batch: bool = True,
        budget: float = 2.0, out_name: str = "", log=print) -> Path:
    cfgs = load_configs()
    cfg = dict(cfgs[config_name], name=config_name)
    fams = [f for f in (families.split(",") if families else cfg["families"]) if f]
    out = config.RESULTS / (out_name or config_name)
    out.mkdir(parents=True, exist_ok=True)
    traces_path = out / "traces.jsonl"
    llm.MODEL = cfg.get("model", config.MODEL)
    meta_path = out / "run.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {"config": cfg, "sessions": []}
    meta["config"] = cfg
    meta.setdefault("families", {})
    jobs = []
    done = {(t["family"], t["task"], t["trial"]) for t in trace.load(traces_path) if not t.get("error")}
    spent = sum(t["usage"]["cost_usd"] for t in trace.load(traces_path))
    want = set(tasks.split(",")) if tasks else None
    for fam in fams:
        ad = ADAPTERS[fam]
        meta["families"][fam] = ad.configure(cfg)
        for task in ad.load_tasks():
            if want and task["id"] not in want:
                continue
            for k in trial_range(trials):
                if (fam, task["id"], k) not in done:
                    jobs.append((fam, task, k))
    log(f"{len(jobs)} trials to run for {config_name} (model {llm.MODEL}, {'batch' if batch else 'standard'} API); "
        f"US${spent:.4f} already spent in this run")
    if not jobs:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8", newline="\n")
        return out
    lock = threading.Lock()
    pool = None
    if batch:
        pool = llm.POOL = llm.BatchPool(log=log, journal=out / "journal.jsonl")
        for _ in jobs:
            pool.enter()
        pool.start()
    state = {"spent": spent, "n": 0}

    def work(fam, task, k):
        try:
            if not batch:
                with lock:
                    if state["spent"] >= budget:
                        return
            ad = ADAPTERS[fam]
            name = f"{config_name}/{fam}/{task['id']}/t{k}"
            calls = llm.start_trial(name)
            t0 = time.perf_counter()
            try:
                if fam == "biz":
                    res = ad.run_trial(task, cfg, out / "biz" / f"{task['id']}-t{k}")
                else:
                    res = ad.run_trial(task, cfg)
            except Exception as e:
                res = {"turns": [], "gate": {"auto_release": False}, "error": f"{type(e).__name__}: {str(e)[:300]}", "extras": {}}
            wall = time.perf_counter() - t0
            extras = res.get("extras", {})
            rec = {"run": out.name, "family": fam, "task": task["id"], "tier": task["tier"], "lang": task["lang"],
                   "config": {"name": config_name, "model": llm.MODEL}, "trial": k, "mode": "batch" if batch else "standard",
                   "turns": res["turns"], "gate": res["gate"], "error": res.get("error"), "usage": trace.usage_of(calls),
                   "latency_s": None if batch else round(wall + extras.get("ocr_seconds", 0.0), 2), "extras": extras,
                   "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            with lock:
                trace.append(traces_path, rec)
                state["spent"] += rec["usage"]["cost_usd"]
                state["n"] += 1
                log(f"[{state['n']}/{len(jobs)}] {fam} {task['id']} t{k} "
                    f"{'ERROR ' + rec['error'][:60] if rec['error'] else 'ok'} calls {rec['usage']['calls']} "
                    f"US${rec['usage']['cost_usd']:.4f} (run total US${state['spent']:.4f})")
        finally:
            if pool:
                pool.leave()

    threads = [threading.Thread(target=work, args=j) for j in jobs]
    workers = len(threads) if batch else 4
    started = []
    for t in threads:
        if not batch:
            while sum(x.is_alive() for x in started) >= workers:
                time.sleep(0.2)
        t.start()
        started.append(t)
    for t in threads:
        t.join()
    if pool:
        pool.close()
        llm.POOL = None
    meta["sessions"].append({"trials": trials, "families": fams, "batch": batch, "jobs": len(jobs),
                             "batches": pool.batches if pool else [], "answers_reused": pool.reused if pool else 0,
                             "spent_after_usd": round(state["spent"], 6), "ended": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8", newline="\n")
    log(f"done: US${state['spent']:.4f} spent in {out.name}")
    return out
