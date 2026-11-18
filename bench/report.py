"""Score stored traces and write the result tables.

  score_runs()   eval/results/<run>/scores.jsonl (one line per trial: every check, stage verdicts,
                 success, gate, attribution) and summary.json (metrics by family, tier, language)
  build_all()    the above for every run, then eval/results/summary.md (all tables in the report)
                 and eval/results/attribution.md (the failure attribution table)

No model is called; everything here is computed from the traces, the tasks and the answer keys.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from . import attribution, checks, config, metrics, trace

FAMILY_NAME = {"ad_review": "Ad review", "biz": "Business review", "creator": "Creator workflow"}


@lru_cache(maxsize=None)
def tasks_of(family: str) -> dict:
    if family == "ad_review":
        from .adapters import ad_review as m
    elif family == "biz":
        from .adapters import biz as m
    else:
        from .adapters import creator as m
    return {t["id"]: t for t in m.load_tasks()}


SKIP = {"smoke"}  # the harness check before the first real run; kept on disk, left out of the tables


def runs() -> list[str]:
    return sorted(p.parent.name for p in config.RESULTS.glob("*/traces.jsonl") if p.parent.name not in SKIP)


def score_trace(tr: dict, offered: dict, notes: list[dict]) -> dict:
    task = tasks_of(tr["family"])[tr["task"]]
    cs = checks.FAMILY_CHECKS[tr["family"]](tr, task, offered)
    calls = [v for t in tr["turns"] for v in (checks.tool_call_verdicts(t, _offered_map(tr, task, offered)) if _has_tools(tr) else [])]
    row = {"run": tr["run"], "config": tr["config"]["name"], "family": tr["family"], "task": tr["task"], "tier": tr["tier"],
           "lang": tr["lang"], "trial": tr["trial"], "mode": tr["mode"], "setting": tr["extras"].get("setting"),
           "checks": cs, "stages": checks.stage_verdicts(cs), "success": checks.outcome_pass(cs),
           "first_failure": None, "gate": tr["gate"], "tool_calls": calls, "error": tr.get("error"),
           "cost_usd": tr["usage"]["cost_usd"], "list_cost_usd": tr["usage"]["list_cost_usd"], "model_calls": tr["usage"]["calls"],
           "latency_s": tr["latency_s"]}
    if not row["success"]:
        row["first_failure"] = checks.first_failure(cs)
        row["attribution"] = attribution.attribute(row, notes)
    return row


def _has_tools(tr: dict) -> bool:
    return not (tr["family"] == "creator" and tr["extras"].get("setting") == "plain")


def _offered_map(tr: dict, task: dict, offered: dict) -> dict:
    if tr["family"] == "ad_review":
        tools = offered["image" if task["item"]["kind"] == "image" else "text"]
    else:
        tools = offered.get("tools", [])
    return {x["name"]: x["input_schema"] for x in tools}


def offered_tools(run: str) -> dict:
    meta = json.loads((config.RESULTS / run / "run.json").read_text(encoding="utf-8"))
    out = {}
    for fam, m in meta.get("families", {}).items():
        out[fam] = m["tools"] if fam == "ad_review" else {"tools": m.get("tools", [])}
    if "creator" in out and not out["creator"]["tools"]:
        from .adapters import creator
        out["creator"] = {"tools": creator.tools()}
    return out


def score_runs(names: list[str] | None = None) -> dict:
    notes = attribution.load_notes()
    res = {}
    for run in names or runs():
        folder = config.RESULTS / run
        offered = offered_tools(run)
        rows = [score_trace(t, offered[t["family"]], notes) for t in trace.load(folder / "traces.jsonl")]
        rows.sort(key=lambda r: (r["family"], r["task"], r["trial"]))
        with (folder / "scores.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        summary = {"run": run, "overall": metrics.summarise(rows),
                   "by_family": metrics.by(rows, lambda r: r["family"]),
                   "by_family_tier": metrics.by(rows, lambda r: f"{r['family']}/{r['tier']}"),
                   "by_family_lang": metrics.by(rows, lambda r: f"{r['family']}/{r['lang']}"),
                   "checks": check_table(rows)}
        (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        res[run] = rows
        print(f"{run}: {len(rows)} trials scored")
    return res


def check_table(rows: list[dict]) -> dict:
    """Per (family, check id): how often it applied and passed."""
    t = defaultdict(lambda: {"stage": "", "applies": 0, "passed": 0, "core": True})
    for r in rows:
        for c in r["checks"]:
            if c["passed"] is None:
                continue
            e = t[f"{r['family']}:{c['id']}"]
            e["stage"], e["core"] = c["stage"], c["core"]
            e["applies"] += 1
            e["passed"] += bool(c["passed"])
    return dict(sorted(t.items()))


# ---------------------------------------------------------------- tables

def pct(x) -> str:
    return "–" if x is None else f"{100 * x:.0f}%"


def frac(c, n) -> str:
    return f"{c}/{n} ({100 * c / n:.0f}%)" if n else "–"


def money(x) -> str:
    return "–" if x is None else f"${x:.4f}"


def secs(x) -> str:
    return "–" if x is None else f"{x:.1f} s"


def metric_rows(s: dict) -> list[tuple[str, str]]:
    ci = s["success_ci"]
    return [
        ("Trials (tasks × trials)", f"{s['trials']} ({s['tasks']} × {s['trials'] // max(1, s['tasks'])})"),
        ("Task success (pass^1)", frac(s["success"], s["trials"]) + (f", 95% CI {pct(ci[0])}–{pct(ci[1])}" if ci else "")),
        ("pass^3 (all three trials succeed)", pct(s["pass^3"])),
        ("Needs human confirmation (an automatic gate held it)", frac(s["needs_human"], s["trials"]) if s["needs_human_rate"] is not None else "–"),
        ("Failures the gates caught", str(s["caught_failures"])),
        ("Failures the gates let through", str(s["silent_failures"])),
        ("Successes held for confirmation", str(s["held_successes"])),
        ("Tool calls correct", frac(s["tool_calls_correct"], s["tool_calls"])),
        ("Exact repeats of a tool call", str(s["tool_calls_repeated"])),
        ("Model calls per trial", f"{s['model_calls_per_trial']:.1f}"),
        ("Cost per trial (as paid)", money(s["cost_per_trial"])),
        ("Cost per trial at the standard price", money(s["list_cost_per_trial"])),
        ("Latency per trial, median (standard API trials)", secs(s["latency_median_s"]) + (f" (n = {s['latency_trials']})" if s["latency_trials"] else "")),
        ("Latency per trial, 90th percentile", secs(s["latency_p90_s"])),
    ]


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)] + ["| " + " | ".join(r) + " |" for r in rows]


def build_all() -> None:
    scored = score_runs()
    L = ["# Results", "", "Generated by `python -m bench all` from the stored traces. Do not edit by hand.", ""]
    for run, rows in scored.items():
        if not rows:
            continue
        L += [f"## Run `{run}`", ""]
        fams = sorted({r["family"] for r in rows})
        sums = {f: metrics.summarise([r for r in rows if r["family"] == f]) for f in fams}
        L += table(["Measure"] + [FAMILY_NAME[f] for f in fams],
                   [[name] + [dict(metric_rows(sums[f]))[name] for f in fams] for name, _ in metric_rows(sums[fams[0]])])
        L += ["", "Stage pass rates (trials where the stage's checks applied):", ""]
        L += table(["Stage"] + [FAMILY_NAME[f] for f in fams],
                   [[checks.STAGE_NAMES[s]] + [frac(sums[f]["stages"][s]["passed"], sums[f]["stages"][s]["applies"]) for f in fams]
                    for s in checks.STAGES])
        L += ["", "Task success by tier:", ""]
        tiers = sorted({r["tier"] for r in rows})
        L += table(["Tier"] + [FAMILY_NAME[f] for f in fams],
                   [[t] + [(lambda rs: frac(sum(r["success"] for r in rs), len(rs)))([r for r in rows if r["family"] == f and r["tier"] == t])
                           for f in fams] for t in tiers])
        L += [""]
    L += comparisons(scored)
    (config.RESULTS / "summary.md").write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")
    write_attribution(scored)
    write_checks(scored)
    write_facts(scored)
    print("wrote", config.RESULTS / "summary.md")


def per_task_rate(rows: list[dict]) -> dict:
    d = defaultdict(list)
    for r in rows:
        d[(r["family"], r["task"])].append(r["success"])
    return {k: sum(v) / len(v) for k, v in d.items()}


def comparisons(scored: dict) -> list[str]:
    """Each configuration against base on the tasks both ran, family by family."""
    if "base" not in scored:
        return []
    L = ["## Comparisons with `base`", ""]
    base = [r for r in scored["base"] if r["trial"] in (1, 2, 3)]
    for run, rows in scored.items():
        if run == "base" or run == "smoke" or not rows:
            continue
        for fam in sorted({r["family"] for r in rows}):
            a = [r for r in base if r["family"] == fam]
            b = [r for r in rows if r["family"] == fam]
            tasks = {r["task"] for r in a} & {r["task"] for r in b}
            a, b = [r for r in a if r["task"] in tasks], [r for r in b if r["task"] in tasks]
            if not a or not b:
                continue
            sa, sb = metrics.summarise(a), metrics.summarise(b)
            ra, rb = per_task_rate(a), per_task_rate(b)
            st = metrics.sign_test([(ra[k], rb[k]) for k in ra if k in rb])
            L += [f"### `{run}` against `base`: {FAMILY_NAME[fam]} ({len(tasks)} tasks)", ""]
            L += table(["Measure", "base", run], [[n, v, dict(metric_rows(sb))[n]] for n, v in metric_rows(sa)])
            L += ["", f"Per task, {run} better on {st['better']}, worse on {st['worse']}, same on "
                      f"{len(tasks) - st['better'] - st['worse']}; exact sign test p = {st['p']:.3f}.", ""]
            L += table(["Stage", "base", run], [[checks.STAGE_NAMES[s], frac(sa["stages"][s]["passed"], sa["stages"][s]["applies"]),
                                                  frac(sb["stages"][s]["passed"], sb["stages"][s]["applies"])] for s in checks.STAGES])
            L += [""]
    return L


def write_attribution(scored: dict) -> None:
    L = ["# Failure attribution", "", "Generated by `python -m bench all`. Every failed trial, its first failing check (stage), "
         "and the cause: from the rules in `bench/attribution.py`, or from a note in `eval/attribution_notes.yaml` "
         "written after reading the transcript (source: note).", ""]
    for run, rows in scored.items():
        fails = [r for r in rows if not r["success"]]
        if not fails:
            continue
        L += [f"## Run `{run}`: {len(fails)} failed trials of {len(rows)}", ""]
        cnt = Counter((r["attribution"]["stage"], r["attribution"]["cause"]) for r in fails)
        L += table(["Stage", "Cause", "Failed trials"],
                   [[checks.STAGE_NAMES[s], f"{c} ({attribution.CAUSE_ZH[c]})", str(n)] for (s, c), n in sorted(cnt.items(), key=lambda x: -x[1])])
        L += ["", "Failure patterns (same task, same first failing check):", ""]
        pats = defaultdict(list)
        for r in fails:
            pats[(r["family"], r["task"], r["attribution"]["check"])].append(r)
        rowsout = []
        for (fam, task, cid), rs in sorted(pats.items()):
            a = rs[0]["attribution"]
            rowsout.append([FAMILY_NAME[fam], task, f"{cid} ({checks.STAGE_NAMES[a['stage']]})", f"{len(rs)}",
                            f"{a['cause']} ({attribution.CAUSE_ZH[a['cause']]})", a["why"][:160].replace("|", "/"), a["fix"][:160].replace("|", "/"),
                            a["source"]])
        L += table(["Family", "Task", "First failing check", "Trials", "Cause", "What happened", "Suggested fix", "Source"], rowsout)
        L += [""]
    (config.RESULTS / "attribution.md").write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")


def write_checks(scored: dict) -> None:
    L = ["# Checks", "", "Generated by `python -m bench all`. How often each rubric check applied and passed, per run.", ""]
    for run, rows in scored.items():
        if not rows:
            continue
        t = check_table(rows)
        L += [f"## Run `{run}`", ""]
        L += table(["Family", "Check", "Stage", "Core", "Passed"],
                   [[k.split(":")[0], k.split(":")[1], v["stage"], "yes" if v["core"] else "no", frac(v["passed"], v["applies"])]
                    for k, v in t.items()])
        L += [""]
    (config.RESULTS / "checks.md").write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- derived figures the report cites

def write_facts(scored: dict) -> None:
    """Figures computed from the traces that are not in the metric tables (cited in EVALUATION.md)."""
    from . import trace as T
    L = ["# Derived figures", "", "Generated by `python -m bench all` from the stored traces. Do not edit by hand.", ""]
    total = 0.0
    rows = []
    for run in scored:
        trs = T.load(config.RESULTS / run / "traces.jsonl")
        cost = sum(t["usage"]["cost_usd"] for t in trs)
        total += cost
        rows.append([run, str(len(trs)), f"${cost:.4f}", f"${sum(t['usage']['list_cost_usd'] for t in trs):.4f}"])
    judge = 0.0
    for p in sorted(config.RESULTS.glob("*/judgements.summary.json")):
        c = json.loads(p.read_text(encoding="utf-8"))["cost_usd"]
        judge += c
        rows.append([f"judge: {p.parent.name}", "", f"${c:.4f}", ""])
    smoke = config.RESULTS / "smoke" / "traces.jsonl"
    smoke_cost = sum(t["usage"]["cost_usd"] for t in T.load(smoke)) if smoke.exists() else 0.0
    rows.append(["smoke test (not in the tables)", "", f"${smoke_cost:.4f}", ""])
    L += ["## Spend", ""] + table(["Run", "Trials", "Cost as paid", "At the standard price"], rows)
    L += ["", f"Total spend, all runs, the judge and the smoke test: US${total + judge + smoke_cost:.2f}", ""]

    L += ["## Ad review", ""]
    for run in scored:
        trs = [t for t in T.load(config.RESULTS / run / "traces.jsonl") if t["family"] == "ad_review"]
        if not trs:
            continue
        refused = [s for t in trs for turn in t["turns"] if turn["kind"] != "advice" for s in turn["steps"]
                   if s["type"] == "tool" and s["name"] == "submit_advice"]
        trials_refused = sum(1 for t in trs if any(s["type"] == "tool" and s["name"] == "submit_advice"
                                                   for turn in t["turns"] if turn["kind"] != "advice" for s in turn["steps"]))
        chars = sum(len(json.dumps(s["input"], ensure_ascii=False)) for s in refused) // max(1, len(refused))
        out_tok = sum(t["usage"]["output_tokens"] for t in trs) / len(trs)
        sc = {(r["task"], r["trial"]): r for r in scored[run] if r["family"] == "ad_review"}
        double = 0
        for t in trs:
            r = sc.get((t["task"], t["trial"]))
            if not r or r["success"]:
                continue
            for turn in t["turns"]:
                if turn["kind"] == "advice" or not turn["final"]:
                    continue
                by_quote = defaultdict(set)
                for v in turn["final"]["violations"]:
                    if v.get("ad_quote"):
                        by_quote[v["ad_quote"]].add(v["category"])
                if any(len(c) > 1 for c in by_quote.values()):
                    double += 1
                    break
        L += [f"- `{run}`: trials with submit_advice called in a review turn {trials_refused}/{len(trs)}; "
              f"average length of those calls' input {chars} characters; output tokens per trial {out_tok:,.0f}; "
              f"failed trials with one phrase listed under two categories {double}/{len(trs)}"]
    L += ["", "## Business review", ""]
    for run in scored:
        trs = [t for t in T.load(config.RESULTS / run / "traces.jsonl") if t["family"] == "biz" and t["turns"]]
        if not trs:
            continue
        sup = [e for t in trs for e in t["turns"][0]["outcome"]["asked"]["events"] if e["lever"] == "supply"]
        unflagged = biz_unflagged(trs)
        rs = [r for r in scored[run] if r["family"] == "biz"]
        scoped = [r for r in rs if r["task"] in ("B17", "B18")]
        weekly = [r for r in rs if r["task"] not in ("B17", "B18")]
        flagged_text = ("not computed here (needs grounded-business-review next to this repository)" if unflagged is None else
                        f"{len(unflagged)} ({', '.join(f'{a} trial {b}' for a, b in unflagged) or 'none'})")
        L += [f"- `{run}`: store closures located at the merchant {sum(e['found'] == 'same' for e in sup)}/{len(sup)}; "
              f"trials naming a cause on a slice the monitor did not flag for that lever {flagged_text}; "
              f"weekly requests {sum(r['success'] for r in weekly)}/{len(weekly)}, scoped requests {sum(r['success'] for r in scoped)}/{len(scoped)}"]
    L += ["", "## Composite figures", ""]
    L += composite_lines(scored)
    L += ["", "## All families", ""]
    for run, rs in scored.items():
        ok = [r for r in rs if r["success"]]
        planning = sum(1 for r in rs if r["stages"]["P"] is False)
        L += [f"- `{run}`: trials failing a planning check {planning}, of which in successful trials "
              f"{sum(1 for r in ok if r['stages']['P'] is False)}"]
    (config.RESULTS / "facts.md").write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")


def biz_unflagged(trs: list[dict]) -> list[tuple[str, int]] | None:
    """Trials naming a non-holiday cause whose lever raised no monitor alert in its segment (the case
    generator's own visibility rule, bizagent.scenarios.visible). None when that agent is not available."""
    try:
        from .adapters import biz
        biz.agent()
        from bizagent import db, monitor, scenarios
    except (SystemExit, ImportError):
        return None
    tasks = tasks_of("biz")
    alerts, out = {}, []
    for t in trs:
        case = tasks[t["task"]]["case"]
        if t["task"] not in alerts:
            con = db.connect(scenarios.build(case))
            alerts[t["task"]] = monitor.alerts(con, case["week"])
            con.close()
        for c in (t["turns"][0]["final"] or {}).get("causes", []):
            if c.get("lever") == "holiday":
                continue
            if not scenarios.visible({"id": "x", "events": [{"lever": c["lever"], "segment": dict(c.get("segment") or {})}]}, alerts[t["task"]]):
                out.append((t["task"], t["trial"]))
                break
    return sorted(out)


def composite_lines(scored: dict) -> list[str]:
    """Totals and differences across the tables, computed here so the report can cite them."""
    out = []
    n_tasks = {f: len(tasks_of(f)) for f in ("ad_review", "biz", "creator")}
    out.append(f"- Tasks: {sum(n_tasks.values())} in all; ad review {n_tasks['ad_review']}, business review {n_tasks['biz']}, "
               f"creator workflow {n_tasks['creator']}")
    seen = {}
    for rs in scored.values():
        for r in rs:
            for c in r["checks"]:
                seen[c["id"]] = c["core"]
    out.append(f"- Rubric checks seen in the runs: {len(seen)} in all, {sum(seen.values())} core and {len(seen) - sum(seen.values())} report only")
    base = scored.get("base", [])
    if base:
        fails = [r for r in base if not r["success"]]
        caught = sum(1 for r in fails if not r["gate"].get("auto_release"))
        task_cause = sum(1 for r in fails if r.get("attribution", {}).get("cause") == "task")
        std = [r for r in base if r["mode"] == "standard"]
        out.append(f"- Base failed trials {len(fails)}: caught by a gate {caught}, let through {len(fails) - caught}; "
                   f"cause task (problems of the tasks or the answer key) {task_cause}")
        out.append(f"- Base standard-API trials (latency measured): {len(std)}")
        for f in ("ad_review", "biz", "creator"):
            rs = [r for r in base if r["family"] == f]
            tiers = sorted({r["tier"] for r in rs})
            pct = lambda xs: f"{100 * sum(r['success'] for r in xs) / len(xs):.0f}%"
            out.append(f"- Base {FAMILY_NAME[f]}: success {pct(rs)}; " + ", ".join(f"{t} {pct([r for r in rs if r['tier'] == t])}" for t in tiers))
    if base and "no-skills" in scored:
        a = [r for r in base if r["family"] == "creator"]
        b = scored["no-skills"]
        sa, sb = metrics.summarise(a), metrics.summarise(b)
        out.append(f"- Creator, base against no-skills: success {100 * sa['success_rate']:.0f}% against {100 * sb['success_rate']:.0f}%, "
                   f"a difference of {100 * (sa['success_rate'] - sb['success_rate']):.0f} points; standard-price cost per trial "
                   f"{sa['list_cost_per_trial'] / sb['list_cost_per_trial']:.1f} times as much with the skills")
    for run, fam in (("fix-ad", "ad_review"), ("fix-biz", "biz")):
        if base and run in scored:
            from . import trace as T
            tb = [t for t in T.load(config.RESULTS / "base" / "traces.jsonl") if t["family"] == fam]
            tf = T.load(config.RESULTS / run / "traces.jsonl")
            ob, of = (sum(t["usage"]["output_tokens"] for t in x) / len(x) for x in (tb, tf))
            out.append(f"- {run} against base: output tokens per trial {ob:,.0f} against {of:,.0f} ({100 * (of - ob) / ob:+.0f}%)")
    cal_as, cal = config.RESULTS / "judge-calibration" / "judgements.summary.as-written.json", config.RESULTS / "judge-calibration" / "judgements.summary.json"
    if cal_as.exists() and cal.exists():
        tot = lambda d: (sum(v["right"] for v in d["per_criterion"].values()), sum(v["expected"] for v in d["per_criterion"].values()))
        (r1, n1), (r2, n2) = tot(json.loads(cal_as.read_text(encoding="utf-8"))), tot(json.loads(cal.read_text(encoding="utf-8")))
        n_cal = json.loads(cal.read_text(encoding="utf-8"))["answers_judged"]
        out.append(f"- Judge calibration: {n_cal} answers; agreement with the written verdicts {r1} of {n1} as written, {r2} of {n2} after the correction")
    jb = config.RESULTS / "base" / "judgements.summary.json"
    if jb.exists():
        d = json.loads(jb.read_text(encoding="utf-8"))
        agree = sum(v["agree"] for v in d["per_criterion"].values())
        judged = sum(v["judged"] for v in d["per_criterion"].values())
        out.append(f"- Judge on base: {d['answers_judged']} answers; the two passes agree on {agree} of {judged} criterion judgements")
        rows = [json.loads(l) for l in (config.RESULTS / "base" / "judgements.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        by = defaultdict(dict)
        for r in rows:
            if r["item"] == "J-BZ":
                v = {c.get("id"): c.get("verdict") for c in (r["verdict"] or {}).get("criteria", [])}
                by[(r["task"], r["trial"])][r["pass"]] = v.get("J-BZ-2")
        fails = [k for k, v in by.items() if v.get(1) == v.get(2) == "fail"]
        quiet = [k for k in fails if k[0] in ("B13", "B14", "B15", "B16")]
        out.append(f"- J-BZ-2 failed by both passes: {len(fails)} reports, {len(quiet)} of them in the quiet and holiday weeks B13 to B16")
        names = {"J-AD-1": "every problem named with its rule", "J-AD-2": "fixes stated, missing facts asked for, not made up",
                 "J-AD-3": "the corrected version adds no claim that needs proof", "J-BZ-1": "advice follows from a stated cause",
                 "J-BZ-2": "advice says what to do", "J-CR-1": "the angle suits the account", "J-CR-2": "the hook works as a first line"}
        out += ["", "Judged items on base (two passes per answer):", ""]
        out += table(["Criterion", "Judged", "Both passes agree", "Pass (both)", "Fail (both)", "Split"],
                     [[f"{k} {names[k]}", str(v["judged"]), str(v["agree"]), str(v["pass_both"]), str(v["fail_both"]), str(v["split"])]
                      for k, v in sorted(d["per_criterion"].items())])
        out.append("")
    return out
