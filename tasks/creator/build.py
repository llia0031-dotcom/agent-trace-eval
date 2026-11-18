"""Build and check the creator workflow tasks from specs.py.

The three parts of every task are built by the creator-skills evaluation's own builders:
  trend   pages written as HTML, an index, the brief, and the answers per page
  shot    the script file, the allowed length, how much of it the script alone needs
  sub     auto captions cut from the spoken words, with the planted transcription and timing errors
and checked the way that project checks its own cases: reference facts must pass its scorer and its
frozen script, trap facts must be flagged as written, the casualty rule must hold, and the frozen
subtitle script must report exactly the planted timing errors and every planted text error.

  python tasks/creator/build.py     writes trend/, shot/, sub/ and cases.json (answers; never shown to a model)
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from bench.adapters.creator_lib import lib  # noqa: E402
import specs  # noqa: E402


def join_script(sentences: list[str], lang: str) -> str:
    return ("" if lang == "zh" else " ").join(sentences)


def check_trend(case: dict, L: dict) -> list[str]:
    ts = L["trend"]["score"]
    problems, cid = [], case["id"]
    pages = ts.case_pages(case)
    for b in case["bad_figures"]:
        import re
        import unicodedata
        text = lambda u: unicodedata.normalize("NFKC", pages[ts.norm_url(u)]["text"]).lower()
        if not re.search(b["old"], text(b["old_url"]), re.I):
            problems.append(f"{cid}: old figure {b['old']} not on its page")
        if not re.search(b["new"], text(b["new_url"]), re.I):
            problems.append(f"{cid}: new figure {b['new']} not on its page")
    for f in case["reference_facts"]:
        fl = ts.fact_flags(f, case, pages)
        if fl:
            problems.append(f"{cid}: reference fact flagged {fl}: {f['claim']}")
        if ts.bad_figures_used({"title": "", "facts": [f]}, case):
            problems.append(f"{cid}: reference fact uses a bad figure: {f['claim']}")
    code, rep = ts.run_script_on(case, case["reference_facts"])
    if code != 0:
        bad = [(x["claim"], x["problems"]) for x in rep["cards"][0]["facts"] if x["problems"]] if rep else code
        problems.append(f"{cid}: the frozen script does not pass the reference facts: {bad}")
    for t in case["trap_facts"]:
        fl = ts.fact_flags(t["fact"], case, pages)
        if fl != t["expect"]:
            problems.append(f"{cid}: trap fact scored {fl}, expected {t['expect']}: {t['fact']['claim']}")
        code, rep = ts.run_script_on(case, [t["fact"]])
        got = rep["cards"][0]["facts"][0]["problems"] if rep else []
        want = [ts.SCRIPT_NAME[x] for x in t["expect"]]
        if code != 1 or not set(want) <= set(got):
            problems.append(f"{cid}: the frozen script gives {got} for a trap fact, expected {want}")
    if case["sensitive"]:
        ok = {"case": cid, "arm": "x", "repeat": 1, "output": {"declined": True, "decline_reason": "r", "cards": []}}
        bad = {"case": cid, "arm": "x", "repeat": 1, "output": {"declined": False, "decline_reason": "", "cards": [
            {"kind": "content", "title": "t", "facts": case["reference_facts"]}]}}
        if not ts.score_run(ok, case, pages)["sensitive_ok"] or ts.score_run(bad, case, pages)["sensitive_ok"]:
            problems.append(f"{cid}: the casualty rule does not hold")
    return problems


def main():
    L = lib()
    for sub in ("trend", "shot", "sub"):
        shutil.rmtree(HERE / sub, ignore_errors=True)
    tb, sb, ub = L["trend"]["build"], L["shot"]["build"], L["sub"]["build"]
    cases, problems = [], []
    for t in specs.TASKS:
        tid, lang = t["id"], t["lang"]
        trend = tb.write_case({"id": tid, "lang": lang, **t["trend"]})
        problems += check_trend(trend, L)
        script = join_script(t["sub"]["script"], lang)
        shot = sb.build_a({"id": tid, "lang": lang, "script": script, **t["shot"]})
        sub = ub.write([{"id": tid, "lang": lang, **t["sub"]}], HERE / "sub", HERE / "sub" / "_index.json")[0]
        (HERE / "sub" / "_index.json").unlink()
        cases.append({"id": tid, "lang": lang, "tier": t["tier"], "trend": trend, "shot": shot, "sub": sub})
        print(tid, lang, t["tier"], "| trend pages", len(trend["pages"]), "sensitive" if trend["sensitive"] else "",
              "| shot", shot["kind"], f"needs {shot['script_needs_s']} s of {shot['allowed']}", f"naive {shot['naive_total_s']} s",
              "| sub cues", len(sub["cues"]), "asr", len(sub["asr"]), "adlib", len(sub["adlib"]), "skipped", len(sub["skipped"]),
              "timing", [(x["cue"], x["kind"]) for x in sub["timing"]])
    if problems:
        sys.exit("\n".join(problems))
    about = ("Answers of the creator workflow tasks, fixed when the tasks were built; never shown to a model. "
             "trend: each page's true date, titles and role, figures that must not be used as current, reference and trap facts. "
             "shot: the allowed length and rates. sub: what every cue should say and which cues carry which planted problem. "
             "All content is fictional.")
    (HERE / "cases.json").write_text(json.dumps({"about": about, "cases": cases}, ensure_ascii=False, indent=1) + "\n",
                                     encoding="utf-8", newline="\n")
    print(f"{len(cases)} tasks -> {HERE / 'cases.json'}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
