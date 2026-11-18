"""The rubric items that rules cannot check, judged against a written scoring guide.

Three items (rubric/RUBRIC.md, section "Judged items"):
  J-AD  ad review, advice turn: does the answer explain each problem and its fix, without making up facts
        the advertiser has to supply, and does the corrected version avoid new claims that need proof?
  J-BZ  business review: does every advice paragraph follow from a cause the report names and say what to do?
  J-CR  creator topics: does each card fit the account (niche, audience, tone), with a usable first line?

Each answer is judged twice, in two independent requests with the same prompt; agreement between the two
passes is reported per item. Before judging the runs, the judge is run on a calibration set of answers
written with a known verdict (rubric/judge_calibration.yaml); its accuracy there is reported too.
A criterion counts as passed only when both passes say pass; a split verdict counts as "uncertain".
The calls use claude-haiku-5-5 (the model of the agents), through the Message Batches API.
Judged items are reported next to the rule-based results and never change task success.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import yaml

from . import config, llm, trace

PASSES = 2
VERDICT = {"type": "object", "additionalProperties": False, "required": ["criteria"],
           "properties": {"criteria": {"type": "array", "items": {
               "type": "object", "additionalProperties": False, "required": ["id", "verdict", "reason"],
               "properties": {"id": {"type": "string"}, "verdict": {"type": "string", "enum": ["pass", "fail"]},
                              "reason": {"type": "string"}}}}}}

GUIDES = {
    "J-AD": {
        "intro": "You are checking a reply an ad review agent gave to an advertiser who asked what is wrong with their ad and how to fix it.",
        "criteria": [
            ("J-AD-1", "The answer names every problem the review found and, for each, the rule it breaks."),
            ("J-AD-2", "The answer says how to fix each problem. Where a fix needs a fact only the advertiser has (a data source, an "
                       "alcohol strength, a licence), it asks the advertiser to add it and does not make the fact up."),
            ("J-AD-3", "The corrected version adds no new claim that would itself need proof: no new superlative, ranking, result, "
                       "health effect, guarantee or number that is not in the original ad."),
        ]},
    "J-BZ": {
        "intro": "You are checking the advice in a weekly business review written for a local-services platform's operations team.",
        "criteria": [
            ("J-BZ-1", "Every advice paragraph follows from a cause or finding stated earlier in the same report (it is about the "
                       "same segment and the same problem)."),
            ("J-BZ-2", "Every advice paragraph says what to do, for whom or where; a paragraph that only says to keep watching or "
                       "to look into it further fails, unless the report says the cause is not yet clear."),
        ]},
    "J-CR": {
        "intro": "You are checking topic cards written for a short-video creator's account.",
        "criteria": [
            ("J-CR-1", "Every card's angle suits the account's niche and audience as described."),
            ("J-CR-2", "Every card's hook would work as the first line or first three seconds of the video: it is short, concrete "
                       "and in the account's tone."),
        ]},
}


def prompt(item: str, material: str) -> str:
    g = GUIDES[item]
    lines = [g["intro"], "", "Judge each criterion separately, pass or fail, with a one-sentence reason. Judge only what is "
             "written; do not reward length.", "", "Criteria:"]
    lines += [f"- {cid}: {text}" for cid, text in g["criteria"]]
    lines += ["", "Material:", material, "", "Reply in JSON: criteria, one entry per criterion id above."]
    return "\n".join(lines)


# ---------------------------------------------------------------- material from traces

def material_ad(tr: dict, task: dict) -> str | None:
    adv = next((t for t in tr["turns"] if t["kind"] == "advice"), None)
    rev = next((t for t in tr["turns"] if t["kind"] == "review"), None)
    if not adv or not adv["final"] or not rev or not rev["final"]:
        return None
    item = task["item"]
    words = item["copy"] + ("\nWords in the image: " + " / ".join(item["image"]["lines"]) if item["kind"] == "image" else "")
    found = [f"- {v['category']} ({v['clause_id']}): {v.get('ad_quote') or v.get('visual_evidence')}" for v in rev["final"]["violations"]]
    a = adv["final"]
    return "\n".join(["The ad:", words, "", "Problems the review found:", *(found or ["(none)"]), "", "The agent's reply:",
                      a.get("answer", ""), "", "Corrected caption: " + a.get("revised_caption", ""),
                      "Corrected image words: " + " / ".join(a.get("revised_image_lines", []) or [])])


def material_biz(tr: dict, task: dict) -> str | None:
    t = tr["turns"][0] if tr["turns"] else None
    if not t or not t["final"] or not any(b.get("kind") == "advice" for b in t["final"].get("blocks", [])):
        return None
    labels = {"summary": "Summary", "finding": "Finding", "cause": "Cause", "advice": "Advice", "note": "Note"}
    return "The report, paragraph by paragraph:\n" + "\n".join(f"[{labels.get(b['kind'], b['kind'])}] {b['text']}" for b in t["final"]["blocks"])


def material_cr(tr: dict, task: dict) -> str | None:
    t = next((t for t in tr["turns"] if t["kind"] == "topics"), None)
    if not t or not t["final"] or not t["final"].get("cards") or task["trend"]["sensitive"]:
        return None
    brief = json.loads((config.TASKS / "creator" / "trend" / task["trend"]["dir"] / "brief.json").read_text(encoding="utf-8"))
    p = brief["profile"]
    cards = [f"Card {i}: title: {c['title']} | hook: {c['hook']} | angle: {c['angle']}" for i, c in enumerate(t["final"]["cards"], 1)]
    return "\n".join([f"Account: platform {p['platform']}; niche {p['niche']}; audience {p['audience']}; tone {p['tone']}",
                      f"Trend: {brief['trend']}", "", *cards])


MATERIAL = {"ad_review": ("J-AD", material_ad), "biz": ("J-BZ", material_biz), "creator": ("J-CR", material_cr)}


# ---------------------------------------------------------------- running

def _ask(text: str, key: str) -> tuple[dict | None, dict]:
    calls = llm.start_trial(key)
    try:
        resp = llm.create(messages=[{"role": "user", "content": text}], max_tokens=4000,
                          output_config={"effort": "low", "format": {"type": "json_schema", "schema": VERDICT}})
        out = json.loads("".join(b.text for b in resp.content if b.type == "text"))
    except Exception as e:  # stored as a failed judgement
        out = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return out, trace.usage_of(calls)


def _run_jobs(jobs: list[tuple[str, str, str]], batch: bool, journal: Path) -> dict:
    """jobs: (key, item, prompt text). Returns key -> {"verdict", "usage"}."""
    results, lock = {}, threading.Lock()
    pool = None
    if batch and jobs:
        pool = llm.POOL = llm.BatchPool(journal=journal)
        for _ in jobs:
            pool.enter()
        pool.start()

    def work(key, text):
        try:
            out, usage = _ask(text, key)
            with lock:
                results[key] = {"verdict": out, "usage": usage}
        finally:
            if pool:
                pool.leave()

    threads = [threading.Thread(target=work, args=(k, t)) for k, _, t in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if pool:
        pool.close()
        llm.POOL = None
    return results


def _verdicts(res: dict, item: str) -> dict:
    v = res.get("verdict") or {}
    want = [c for c, _ in GUIDES[item]["criteria"]]
    got = {c.get("id"): c.get("verdict") for c in v.get("criteria", []) if isinstance(c, dict)}
    return {c: got.get(c) for c in want}


def judge_run(run: str, batch: bool = True, calibration: bool = False) -> None:
    from .report import tasks_of
    llm.MODEL = config.MODEL
    folder = config.RESULTS / run if not calibration else config.RESULTS / "judge-calibration"
    folder.mkdir(parents=True, exist_ok=True)
    out_path = folder / "judgements.jsonl"
    done = {}
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if not (r["verdict"] or {}).get("error"):
                    done[r["key"]] = r
    jobs, meta = [], {}
    if calibration:
        cal = yaml.safe_load((config.ROOT / "rubric" / "judge_calibration.yaml").read_text(encoding="utf-8"))["examples"]
        for ex in cal:
            for p in range(1, PASSES + 1):
                key = f"cal/{ex['id']}/p{p}"
                meta[key] = {"item": ex["item"], "example": ex["id"], "pass": p, "expected": ex["expected"]}
                if key not in done:
                    jobs.append((key, ex["item"], prompt(ex["item"], ex["material"])))
    else:
        for tr in trace.load(config.RESULTS / run / "traces.jsonl"):
            item, fn = MATERIAL[tr["family"]]
            mat = fn(tr, tasks_of(tr["family"])[tr["task"]])
            if mat is None:
                continue
            for p in range(1, PASSES + 1):
                key = f"{run}/{tr['family']}/{tr['task']}/t{tr['trial']}/p{p}"
                meta[key] = {"item": item, "family": tr["family"], "task": tr["task"], "trial": tr["trial"], "pass": p}
                if key not in done:
                    jobs.append((key, item, prompt(item, mat)))
    print(f"{len(jobs)} judgements to make ({len(done)} stored)")
    res = _run_jobs(jobs, batch, folder / "judge-journal.jsonl")
    with out_path.open("a", encoding="utf-8", newline="\n") as f:
        for key, r in res.items():
            f.write(json.dumps({"key": key, **meta[key], **r}, ensure_ascii=False) + "\n")
    summarise(out_path, calibration)


def summarise(path: Path, calibration: bool = False) -> dict:
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            rows[r["key"]] = r
    groups = {}
    for r in rows.values():
        base = r["key"].rsplit("/p", 1)[0]
        groups.setdefault(base, {})[r["pass"]] = r
    per_item = {}
    for base, passes in groups.items():
        if len(passes) < PASSES:
            continue
        item = passes[1]["item"]
        v1, v2 = _verdicts(passes[1], item), _verdicts(passes[2], item)
        for cid in v1:
            e = per_item.setdefault(cid, {"judged": 0, "agree": 0, "pass_both": 0, "fail_both": 0, "split": 0, "missing": 0,
                                          "right": 0, "expected": 0})
            if v1[cid] is None or v2[cid] is None:
                e["missing"] += 1
                continue
            e["judged"] += 1
            e["agree"] += v1[cid] == v2[cid]
            e["pass_both"] += v1[cid] == v2[cid] == "pass"
            e["fail_both"] += v1[cid] == v2[cid] == "fail"
            e["split"] += v1[cid] != v2[cid]
            if calibration and cid in passes[1].get("expected", {}):
                for v in (v1[cid], v2[cid]):
                    e["expected"] += 1
                    e["right"] += v == passes[1]["expected"][cid]
    cost = sum(r["usage"]["cost_usd"] for r in rows.values())
    summary = {"passes": PASSES, "answers_judged": len(groups), "per_criterion": per_item, "cost_usd": round(cost, 6)}
    path.with_name("judgements.summary.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8", newline="\n")
    for cid, e in sorted(per_item.items()):
        line = f"{cid}: judged {e['judged']}, both passes agree {e['agree']}, pass {e['pass_both']}, fail {e['fail_both']}, split {e['split']}"
        if calibration:
            line += f"; right on the calibration set {e['right']}/{e['expected']}"
        print(line)
    print(f"cost US${cost:.4f}")
    return summary
