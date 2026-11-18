"""Connects the ad review agent (grounded-review, agent v1.3) to the bench.

The agent, its tools and its prompts are that project's own code, imported from its folder. This file
only: (1) routes its model calls through bench.llm so every call is recorded, (2) runs one, or for an L3
task three, turns of its Session on a bench task, (3) converts the conversation it kept into a trace,
and (4) scores each turn against the task's answer key with that project's matching rules.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import time
from functools import lru_cache
from pathlib import Path

import yaml

from .. import config, llm, trace

FAMILY = "ad_review"
TASK_DIR = config.TASKS / "ad_review"
VERSION = "v1.3"


def load_tasks() -> list[dict]:
    items = yaml.safe_load((TASK_DIR / "items.yaml").read_text(encoding="utf-8"))
    ocr = json.loads((TASK_DIR / "ocr.json").read_text(encoding="utf-8")) if (TASK_DIR / "ocr.json").exists() else {}
    tasks = []
    for it in items:
        it.setdefault("labels", [])
        it["split"] = "bench"
        t = {"id": it["id"], "family": FAMILY, "tier": it["tier"], "lang": it["lang"], "item": it,
             "image": None, "ocr": None, "image_rev": None, "ocr_rev": None}
        if it["kind"] == "image":
            t["image"], t["ocr"] = TASK_DIR / "images" / f"{it['id']}.png", ocr.get(it["id"])
            if it.get("revision", {}).get("image_lines"):
                t["image_rev"], t["ocr_rev"] = TASK_DIR / "images" / f"{it['id']}-rev.png", ocr.get(it["id"] + "-rev")
        tasks.append(t)
    return tasks


@lru_cache(maxsize=1)
def agent():
    """The ad review project's modules, with its model calls routed through bench.llm."""
    root = config.AD_REVIEW_DIR
    if not (root / "agent" / "review_agent.py").exists():
        raise SystemExit(f"ad review agent not found at {root}; set APE_AD_REVIEW_DIR")
    for sub in ("agent", "policies", "testset"):
        p = str(root / sub)
        if p not in sys.path:
            sys.path.insert(0, p)
    import common
    import library
    import review_agent
    from load import ad_text

    spec = importlib.util.spec_from_file_location("ad_review_score", root / "eval" / "score.py")
    score = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(score)

    def create(usage, **kwargs):
        t0 = time.perf_counter()
        oc = {"effort": config.EFFORT, **kwargs.pop("output_config", {})}
        resp = llm.create(output_config=oc, **kwargs)
        usage.add(resp, time.perf_counter() - t0)
        return resp

    review_agent.create = create
    return {"common": common, "library": library, "review_agent": review_agent, "ad_text": ad_text, "score": score,
            "base_system": review_agent.SYSTEM}


def configure(cfg: dict) -> dict:
    """Apply a run configuration (an optional addition to the system prompt); returns run metadata."""
    a = agent()
    ra = a["review_agent"]
    ra.SYSTEM = a["base_system"] + (("\n\n" + cfg["ad_review_system_addition"]) if cfg.get("ad_review_system_addition") else "")
    return {"agent_version": VERSION,
            "tools": {"image": ra.tools_for(True, VERSION), "text": ra.tools_for(False, VERSION)},
            "system_template_sha": hashlib.sha256(ra.SYSTEM.encode("utf-8")).hexdigest()[:12],
            "system_addition": cfg.get("ad_review_system_addition", "")}


def _review_outcome(item: dict, labels: list[dict], out: dict | None, ad_source: str) -> dict:
    """One review turn against its answer key, by the ad review project's scoring rules."""
    a = agent()
    sc, common = a["score"], a["common"]
    out = out or {"decision": "error", "violations": []}
    allv = out.get("violations", [])
    viols = [v for v in allv if v.get("category") in common.CATEGORIES]
    gold_cats, pred_cats = sorted({l["cat"] for l in labels}), sorted({v["category"] for v in viols})
    labs = []
    for lab in labels:
        kind = next(k for k in ("span", "visual", "absence") if k in lab)
        match = [v for v in viols if v["category"] == lab["cat"]]
        labs.append({"cat": lab["cat"], "kind": kind, "accepted_clauses": lab["clauses"], "found": bool(match),
                     "clause_right": any(v.get("clause_id") in lab["clauses"] for v in match) if match else None,
                     "located": (any(sc.overlaps(v.get("ad_quote", ""), lab["span"], item["lang"]) for v in match)
                                 if match and kind == "span" else None)})
    quotes = []
    if allv:
        res = common.check_quotes(ad_source, allv)
        for v, q in zip(allv, res):
            if q["ad"] is not None:
                quotes.append({"side": "ad", "quote": v.get("ad_quote", ""), "status": q["ad"]})
            quotes.append({"side": "policy", "clause": v.get("clause_id", ""), "quote": v.get("policy_quote", ""), "status": q["policy"]})
    return {"gold_reject": bool(labels), "pred_reject": bool(allv), "decision_field": out.get("decision"),
            "decision_right": bool(allv) == bool(labels), "gold_cats": gold_cats, "pred_cats": pred_cats,
            "labels": labs, "extra_cats": sorted(set(pred_cats) - set(gold_cats)),
            "other_violations": sum(1 for v in allv if v.get("category") not in common.CATEGORIES),
            "quotes": quotes, "is_trap": "trap" in item and labels is item["labels"]}


def _advice_outcome(item: dict, adv: dict | None, raw: dict | None, cleanup: list[str]) -> dict:
    a = agent()
    common, norm = a["common"], a["score"].norm
    adv = adv or {}
    cap, lines = adv.get("revised_caption", ""), adv.get("revised_image_lines", []) or []
    words = norm(cap + " " + " ".join(lines))
    return {"spans_removed": all(norm(l["span"]) not in words for l in item["labels"] if "span" in l),
            "facts_kept": all(norm(f) in words for f in item.get("must_keep", [])),
            "placeholders": common.placeholders(cap + " " + " ".join(lines)),
            "field_problems": common.advice_problems(adv),
            "raw_field_problems": common.advice_problems(raw if raw is not None else adv),
            "cleanup": cleanup, "has_image_lines": bool(lines)}


def run_trial(task: dict, cfg: dict) -> dict:
    """Run one trial. The caller has started call recording (llm.start_trial) in this thread."""
    a = agent()
    item = task["item"]
    s = a["review_agent"].Session(item, VERSION)
    starts, kinds, results = [], [], []

    def turn(kind, fn):
        starts.append(len(s.messages))
        kinds.append(kind)
        try:
            results.append(fn())
        except Exception as e:  # stored with the trace; the scorer counts the turn as failed
            results.append({"output": None, "error": f"{type(e).__name__}: {str(e)[:300]}"})

    turn("review", lambda: s.review(caption=item["copy"], image_path=task["image"], ocr_text=(task["ocr"] or {}).get("text")))
    if item["tier"] == "L3" and not results[0]["error"]:
        turn("advice", s.advise)
        if not results[-1]["error"]:
            rev = item["revision"]
            turn("rereview", lambda: s.review(revised=True, caption=rev.get("copy", item["copy"]),
                                              image_path=task["image_rev"] or task["image"],
                                              ocr_text=(task["ocr_rev"] or task["ocr"] or {}).get("text")))
    turns = trace.turns_from_messages(s.messages, llm._local.calls, starts, kinds)
    ad_text = a["ad_text"]
    sources = {"review": ad_text(item), "rereview": ad_text(item, revised=True) if "revision" in item else ""}
    for t, r in zip(turns, results):
        t["final"], t["error"] = r["output"], r["error"]
        if t["kind"] == "advice":
            t["outcome"] = _advice_outcome(item, r["output"], s.advice_raw, s.advice_cleanup)
        else:
            labels = item["labels"] if t["kind"] == "review" else item["revision"]["labels"]
            t["outcome"] = _review_outcome(item, labels, r["output"], sources[t["kind"]])
            t["ad_text"] = sources[t["kind"]]
    expected = 3 if item["tier"] == "L3" else 1
    gate = {"complete": len(turns) == expected and all(t["final"] is not None and not t["error"] for t in turns)}
    reviews = [t for t in turns if t["kind"] != "advice" and t["final"]]
    gate["quotes_verified"] = all(q["status"] == "supported" for t in reviews for q in t["outcome"]["quotes"])
    gate["decision_consistent"] = all((t["final"].get("decision") == "reject") == bool(t["final"].get("violations")) for t in reviews)
    advice = [t for t in turns if t["kind"] == "advice" and t["final"]]
    gate["advice_ready"] = all(not t["outcome"]["placeholders"] and not t["outcome"]["field_problems"] for t in advice)
    gate["auto_release"] = all(gate.values())
    ocr_seconds = (task["ocr"] or {}).get("seconds", 0.0) if any(st["name"] == "read_image_text" for t in turns for st in trace.tool_steps(t)) else 0.0
    return {"turns": turns, "gate": gate, "error": next((t["error"] for t in turns if t["error"]), None),
            "extras": {"jurisdiction": s.jur, "kind": item["kind"], "expected_turns": expected, "ocr_seconds": ocr_seconds,
                       "system_sha": hashlib.sha256(s.system.encode("utf-8")).hexdigest()[:12]}}
