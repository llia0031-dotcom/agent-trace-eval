"""Connects the business review agent (grounded-business-review, prompt v4) to the bench.

The agent loop, its four tools, its number check and its scorer are that project's own code. This
file only: (1) routes its model calls through bench.llm, (2) hands it the request of a bench task
(the weekly review it was built for, or a scoped question), (3) converts the conversation it saved
into a trace, and (4) stores what its scorer and its number check said.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from functools import lru_cache
from pathlib import Path

import yaml

from .. import config, llm, trace

FAMILY = "biz"
TASK_DIR = config.TASKS / "biz"


def load_tasks() -> list[dict]:
    cases = yaml.safe_load((TASK_DIR / "cases.yaml").read_text(encoding="utf-8"))
    return [{"id": c["id"], "family": FAMILY, "tier": c["tier"], "lang": "zh", "case": c} for c in cases]


@lru_cache(maxsize=1)
def agent():
    root = config.BIZ_DIR
    if not (root / "bizagent" / "arms.py").exists():
        raise SystemExit(f"business review agent not found at {root}; set APE_BIZ_DIR")
    os.environ["BIZ_DATA_DIR"] = str(config.BIZ_DATA_DIR)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from bizagent import arms, db, prompts, scenarios, score
    from bizagent import llm as bl

    def create(usage, key=None, **kwargs):
        t0 = time.perf_counter()
        oc = {"effort": config.EFFORT, **kwargs.pop("output_config", {})}
        resp = llm.create(max_tokens=bl.MAX_TOKENS, output_config=oc, **kwargs)
        usage.batch = llm.POOL is not None
        usage.add(resp, time.perf_counter() - t0)
        return resp

    bl.create = create
    return {"arms": arms, "db": db, "prompts": prompts, "scenarios": scenarios, "score": score,
            "base_system": prompts.system_prompt}


def configure(cfg: dict) -> dict:
    a = agent()
    add = cfg.get("biz_system_addition", "")
    base = a["base_system"]
    a["prompts"].system_prompt = (lambda with_tools: base(with_tools) + "\n\n" + add) if add else base
    return {"prompt_version": a["prompts"].PROMPT_VERSION, "tools": a["prompts"].tools(),
            "system_sha": hashlib.sha256(a["prompts"].system_prompt(True).encode("utf-8")).hexdigest()[:12],
            "system_addition": add, "max_calls": a["arms"].MAX_CALLS}


EMPTY_USAGE = {"calls": 0, "input_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
               "seconds": 0.0, "batch": False, "stop_reasons": []}


def run_trial(task: dict, cfg: dict, work: Path) -> dict:
    """Run one trial in the folder `work` (evidence files and the saved conversation go there)."""
    a = agent()
    case = task["case"]
    shutil.rmtree(work, ignore_errors=True)
    (work / "evidence").mkdir(parents=True)
    # The agent resumes from a saved conversation when one exists: that is how a task's own request is handed to it.
    (work / "state.json").write_text(json.dumps({"messages": [{"role": "user", "content": case["request"]}],
                                                 "usage": EMPTY_USAGE, "evidence": []}, ensure_ascii=False), encoding="utf-8")
    # Each trial reads its own copy of the case database: DuckDB shares one instance per file within a
    # process, and the agent's read-only connection locks its configuration, so two trials of the same
    # case (batch mode runs them at once) cannot both open the original file.
    copy = config.BIZ_DATA_DIR / "trials" / f"{work.parent.parent.name}-{work.name}.duckdb"
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(a["scenarios"].build(case), copy)
    con = a["db"].connect(copy)
    try:
        error = None
        try:
            result = a["arms"].run_agent(con, case["week"], work / "evidence")
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)[:300]}"
            result = {"has_anomaly": False, "causes": [], "blocks": [], "check": None, "first_check": None, "revisions": 0,
                      "error": error, "trace": []}
        asked = a["score"].score(con, {**case, "truth": case["asked"]}, result)
        full = a["score"].score(con, case, result) if case["request_kind"] == "scoped" else None
    finally:
        con.close()
        copy.unlink(missing_ok=True)
    saved = json.loads((work / "state.json").read_text(encoding="utf-8"))
    turns = trace.turns_from_messages(saved.get("messages", []), llm._local.calls, [0], [case["request_kind"]])
    t = turns[0]
    answered = bool(result.get("blocks")) and not result.get("error")
    t["final"] = {k: result.get(k) for k in ("has_anomaly", "causes", "blocks")} if answered else None
    t["error"] = error or result.get("error")
    check, first = result.get("check"), result.get("first_check")
    slim = lambda c: None if not c else {k: c[k] for k in ("released", "checked", "supported", "failed", "report_problems")} | {
        "failed_blocks": [{"index": b["index"], "status": b["status"], "missing_numbers": b.get("missing_numbers", []),
                           "quotes_not_found": len(b.get("quotes_not_found", []))}
                          for b in c["blocks"] if b["status"] not in ("supported", "no_number")]}
    t["outcome"] = {"asked": asked, "full": full, "check": slim(check), "first_check": slim(first),
                    "revisions": result.get("revisions", 0)}
    gate = {"complete": answered, "numbers_verified": bool(check and check["released"])}
    gate["auto_release"] = all(gate.values())
    return {"turns": turns, "gate": gate, "error": t["error"],
            "extras": {"request_kind": case["request_kind"], "family_of_case": case["family"], "week": case["week"],
                       "planted": [{"lever": e["lever"], "segment": e["segment"]} for e in case["events"]],
                       "asked": case["asked"]["causes"], "alerts": case.get("alerts"), "expected_turns": 1}}
