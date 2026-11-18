"""Export step-by-step replays of a few stored trials, for the portfolio page.

  python tools/make_replay.py [--results eval/results] [--out eval/results/replay/replay.json]

Nothing is re-run and no model is called. The trials are chosen by fixed rules (SELECTION below, also written
into the file), from the stored traces of this repository (in the public copy, policy text is already replaced
by clause ids and links). Every step comes from traces.jsonl; every check, stage result, success flag, gate
result and first failure comes from scores.jsonl, unchanged; the cause and the fix come from the attribution
of that trial. The one-line descriptions of checks are paraphrases of rubric/RUBRIC.md; a failure's English
reason is the attribution's own text, with a Chinese rendering beside it. No cost figures are included.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bench import checks as C  # noqa: E402

REPO = "https://github.com/llia0031-dotcom/agent-trace-eval"
MAX_TEXT = 280
FAMILY_ORDER = ["ad_review", "biz", "creator"]

SELECTION = {
    "gate-missed": {
        "zh": "基线运行里，该语言的试验中任务没成功、自动关卡却放行的；按 Agent（广告审核、经营分析、创作流程）、题号、试验号排序，取第一条。",
        "en": "In the base run, trials in this language whose task failed while every automatic gate let them through; ordered by agent "
              "(ad review, business review, creator workflow), task id and trial number, the first one.",
    },
    "full-pass": {
        "zh": "基线运行里，该语言的试验中任务成功、五个阶段没有一项没过的，先取轮数最多的，再按 Agent、题号、试验号排序，取第一条。",
        "en": "In the base run, trials in this language that succeeded with no stage failed; those with the most turns first, then ordered by "
              "agent, task id and trial number, the first one.",
    },
    "after-fix": {
        "zh": "第一条所在 Agent 有修复运行（fix-ad 或 fix-biz）时，同一道题在修复运行里成功的试验，按试验号取第一条；没有就不放。",
        "en": "If the agent of the first replay has a fix run (fix-ad or fix-biz): the same task's successful trials in that run, the one "
              "with the lowest trial number; otherwise none.",
    },
}
FIX_RUN = {"ad_review": "fix-ad", "biz": "fix-biz"}
LABELS = {
    "gate-missed": {"zh": "关卡放过的一次失败", "en": "A failure the gates let through"},
    "full-pass": {"zh": "全部通过的一次", "en": "A trial that passed every stage"},
    "after-fix": {"zh": "修复之后的同一道题", "en": "The same task after the fix"},
}
AGENTS = {"ad_review": {"zh": "广告审核 Agent", "en": "Ad review agent"},
          "biz": {"zh": "经营分析 Agent", "en": "Business review agent"},
          "creator": {"zh": "创作流程（三个 Skill）", "en": "Creator workflow (three skills)"}}
STAGES = [{"id": "U", "zh": "理解意图", "en": "Understanding"}, {"id": "P", "zh": "规划", "en": "Planning"},
          {"id": "T", "zh": "工具调用", "en": "Tool calls"}, {"id": "K", "zh": "知识引用", "en": "Knowledge and citation"},
          {"id": "R", "zh": "结果质量", "en": "Result quality"}]
CAUSE = {"knowledge": {"zh": "知识缺失", "en": "Missing knowledge"}, "tool": {"zh": "工具调用", "en": "Tool calls"},
         "prompt": {"zh": "提示词", "en": "Prompt"}, "model": {"zh": "模型能力", "en": "Model capability"},
         "interaction": {"zh": "交互设计", "en": "Interaction design"}, "task": {"zh": "评测题本身", "en": "The task itself"}}

# What each check looks at: paraphrases of rubric/RUBRIC.md, one sentence each.
WHAT = {
    "U-0": ("每个请求都按要求的形式给了最终答案", "Every request got a final answer in the required form"),
    "U-AD-2": ("说明和回答用的是广告的语言", "Explanations and the answer are in the ad's language"),
    "U-BZ-2": ("原因没有超出用户问的那一类", "No cause outside the lever the user asked about"),
    "U-CR-1": ("没有拒绝，选题卡的数量和要求的一样", "Not declined, and exactly the number of cards asked for"),
    "U-CR-2": ("伤亡事件：拒绝，或只给公益提示卡", "Casualty event: declined, or public-service cards only"),
    "U-CR-3": ("无关的产品视频里没有提这件事", "The unrelated product video does not mention the event"),
    "U-CR-4": ("字幕条数和每个时间都没改", "Cue count and every timestamp unchanged"),
    "P-0": ("在步数上限内给出答案", "The turn ended with an answer within the step limit"),
    "P-AD-1": ("图片广告先读了图里的文字", "For an image ad, the image text was read"),
    "P-AD-2": ("查过政策条款", "The policy library was searched or a clause fetched"),
    "P-AD-3": ("给出违规前先核对了引文", "Quotes were checked before a violation was named"),
    "P-AD-4": ("给出修改稿前先检查了修改稿", "The corrected version was checked before the advice was given"),
    "P-BZ-1": ("第一次调用的是总览", "The first tool call was the overview"),
    "P-BZ-2": ("说出原因前做过拆解或下钻", "A breakdown, funnel or SQL call came before a cause was named"),
    "P-CR-1": ("回答前跑了这个请求的检查工具", "The request's checking tool was run before answering"),
    "P-CR-2": ("回答前最后一次检查通过了（字幕：检查了改好的文件）", "The last check before answering passed (captions: the corrected file was checked)"),
    "T-1": ("没有失败后没再成功的工具调用", "No tool call failed without a later successful call of the same tool"),
    "T-2": ("这一轮每次工具调用都正确（只报告）", "Every tool call in the turn was correct (report only)"),
    "T-3": ("没有完全重复的工具调用（只报告）", "No call repeats an earlier one with the same input (report only)"),
    "T-4": ("没有调用另一类请求的收尾工具（只报告）", "No call of the other request's finishing tool (report only)"),
    "K-AD-1": ("引的广告原话逐字在广告里", "Every quote of the ad is word for word in the ad"),
    "K-AD-2": ("引的条款原文逐字在所引条款里", "Every policy quote is word for word in the clause it cites"),
    "K-AD-3": ("找到的违规引用的是答案接受的条款", "For each violation found, the cited clause is one the answer key accepts"),
    "K-BZ-1": ("报告通过数字核对：每个数字都在引用的查询行里", "The report passes the number check: every number is in the query rows it quotes"),
    "K-BZ-2": ("第一稿就通过数字核对（只报告）", "Passed the number check on the first draft (report only)"),
    "K-CR-1": ("每条事实的网页、标题、日期、引文都对", "Every fact's page, title, date and quote check out"),
    "K-CR-2": ("没有用被更正或辟谣的数字，没有引用谣言页面", "No superseded or rumoured figure, no rumour page cited"),
    "K-CR-3": ("口播逐字就是定稿的稿子", "The voiceover is the approved script word for word"),
    "K-CR-4": ("没补回没说的句子，没改对的字幕，即兴的话照说的保留", "No skipped line put back, no correct cue changed, ad-libs kept as said"),
    "R-AD-1": ("放行还是拒绝判对了", "Approve or reject is right"),
    "R-AD-2": ("答案里的违规类别都找到了，没有多出的类别", "Every labelled category found, none the key does not have"),
    "R-AD-3": ("修改稿里没有违规原话了", "The offending words are gone from the corrected version"),
    "R-AD-4": ("品牌和价格保留了", "The brand and the price are kept"),
    "R-AD-5": ("修改稿没有占位符、字段没填错", "No unfilled template text, no field filled wrongly"),
    "R-BZ-1": ("有没有异动说对了", "Whether there is an anomaly is said correctly"),
    "R-BZ-2": ("埋的原因都定位到了合适的粒度，没有多列", "Every planted cause located at the right grain, nothing extra"),
    "R-BZ-3": ("定位到的原因环节对了", "Each located cause has the right lever"),
    "R-CR-0": ("这个请求有答案", "The request got an answer"),
    "R-CR-1": ("选题卡全部合格（伤亡事件：拒绝或只给公益卡）", "The cards asked for, every one clean (casualty event: declined or public service only)"),
    "R-CR-2": ("分镜表全部检查通过", "The shot list passes every timing and wording check"),
    "R-CR-3": ("埋的转写错误都改了，时间没动，别的没改", "Every planted transcription error fixed, timings kept, nothing else changed"),
}
# Chinese renderings of the attribution reasons and fixes of the trials that can be chosen (keyed by run, task, check).
WHY_ZH = {
    ("base", "en-L2-01", "R-AD-2"): (
        "减重那句话被列了两次：一次是 WEIGHT（TT-WM-WEIGHT，和答案一样），一次是 TT-MIS-CLAIMS 下的 GUARANTEE。引文都是原话，结论也对。Agent 的说明只写了“一个问题列一次”，没说一句话同时碰到两条规则时该选哪个类别；答案照分类表一句话一个标签。",
        "在说明里写清：一句话同时违反多条规则时，只列一次，放在最具体的类别下（减重说法归 WEIGHT，健康功效归 MEDICAL，数据没有出处归 DATA）。"),
    ("base", "zh-L2-01", "R-AD-2"): (
        "“95% 的用户一周见效”被列成 DATA（和答案一样），又按虚假广告条款 CN-AL-28 列成 GUARANTEE。和 en-L2-01 是同一种问题：一句话，两个类别。",
        "和 en-L2-01 一样：一句话只列一次，用最具体的类别。"),
}


def cut(text, n=MAX_TEXT) -> str:
    t = " ".join(str(text if text is not None else "").split())
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))


def final_summary(family: str, kind: str, final: dict | None) -> str:
    if final is None:
        return "(no answer)"
    if family == "ad_review" and kind != "advice":
        parts = [f"decision: {final.get('decision')}"]
        for v in final.get("violations", []):
            words = v.get("ad_quote") or v.get("visual_evidence") or ""
            parts.append(f"{v.get('category')} {v.get('clause_id')}: {words}")
        return "; ".join(parts)
    if family == "ad_review":
        lines = " / ".join(final.get("revised_image_lines") or [])
        return f"revised caption: {final.get('revised_caption', '')}" + (f"; image words: {lines}" if lines else "") + f"; answer: {final.get('answer', '')}"
    if family == "biz":
        causes = "; ".join(f"{c.get('lever')} {c.get('direction')} {c.get('segment')}" for c in final.get("causes", []))
        first = next((b.get("text", "") for b in final.get("blocks", []) if b.get("kind") == "summary"), "")
        return f"anomaly: {final.get('has_anomaly')}; causes: {causes or 'none'}; summary: {first}"
    if kind == "topics":
        if final.get("declined"):
            return f"declined: {final.get('decline_reason', '')}"
        return "; ".join(f"[{c.get('kind')}] {c.get('title')}" for c in final.get("cards", []))
    if kind == "shotlist":
        shots = final.get("shots") or []
        return f"{len(shots)} shots, total {round(sum(float(s.get('duration_s') or 0) for s in shots), 1)} s; notes: {final.get('notes', '')}"
    return final.get("summary", "")


def build_steps(trace: dict) -> tuple[list[dict], dict]:
    """The steps of a trial in order, and for each turn the indexes of its steps by role (for placing checks)."""
    steps, where = [], {}
    for turn in trace["turns"]:
        t = turn["turn"]
        w = where.setdefault(t, {"calls": [], "final": None})
        for s in turn["steps"]:
            if s["type"] == "user":
                steps.append({"turn": t, "kind": "user", "title": {"zh": "请求", "en": "Request"}, "text": cut(s["text"])})
            elif s["type"] == "system_note":
                steps.append({"turn": t, "kind": "instruction", "title": {"zh": "本轮说明", "en": "Instruction for this turn"}, "text": cut(s["text"])})
            elif s["type"] == "harness":
                steps.append({"turn": t, "kind": "harness", "title": {"zh": "运行框架的提示", "en": "Harness message"}, "text": cut(s["text"])})
            elif s["type"] == "model":
                if s["text"].strip():
                    steps.append({"turn": t, "kind": "model", "title": {"zh": "Agent 的说明", "en": "Agent's note"}, "text": cut(s["text"])})
            elif s["type"] == "tool":
                w["calls"].append((len(steps), s["name"], s))
                steps.append({"turn": t, "kind": "tool_call", "tool": s["name"], "title": {"zh": f"调用 {s['name']}", "en": f"Calls {s['name']}"},
                              "text": cut(compact(s["input"]))})
                steps.append({"turn": t, "kind": "tool_result", "tool": s["name"], "title": {"zh": f"{s['name']} 返回", "en": f"{s['name']} returns"},
                              "text": cut(s["output"]), "error": bool(s["is_error"])})
        w["final"] = len(steps)
        steps.append({"turn": t, "kind": "final", "title": {"zh": "本轮答案", "en": "Answer of this turn"},
                      "text": cut(final_summary(trace["family"], turn["kind"], turn["final"])), "request": turn["kind"]})
    for i, s in enumerate(steps, 1):
        s["n"] = i
    return steps, where


CALL_FOR = {"P-AD-1": ("read_image_text",), "P-AD-2": ("search_policy", "get_clause"), "P-AD-3": ("check_quotes",),
            "P-AD-4": ("check_revision",), "P-BZ-2": ("breakdown", "funnel", "run_sql"),
            "P-CR-1": ("check_sources", "check_shotlist", "check_subtitles"), "P-CR-2": ("check_sources", "check_shotlist", "check_subtitles")}


def place(check: dict, where: dict, last_step: int, offered_finish_other: dict) -> int:
    """Index of the step a check belongs to: tool checks on a call, planning checks on the call they ask for,
    everything else on the turn's answer; a check that cannot be placed goes on the turn's last step."""
    t = check["turn"]
    if t == 0 or t not in where:
        return last_step
    w = where[t]
    calls = w["calls"]
    cid = check["id"]
    if cid.startswith("T-") and calls:
        if cid == "T-4":
            wrong = [i for i, name, _ in calls if name in offered_finish_other.get(t, set())]
            return wrong[0] if wrong else calls[-1][0]
        if cid == "T-1" and check["passed"] is False:
            bad = [i for i, _, s in calls if s["is_error"] or C.ERROR_TEXT.match(str(s["output"]).strip())]
            return bad[-1] if bad else calls[-1][0]
        return calls[-1][0]
    if cid == "P-BZ-1" and calls:
        return calls[0][0]
    if cid in CALL_FOR:
        hit = [i for i, name, _ in calls if name in CALL_FOR[cid]]
        if hit:
            return hit[-1] if cid in ("P-CR-2",) else hit[0]
    return w["final"]


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def record_line(path: Path, family: str, task: str, trial: int) -> int:
    """Line number (1-based) of the stored trace that trace.load keeps for this trial: the last one written."""
    n = None
    for i, l in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if l.strip():
            t = json.loads(l)
            if t["family"] == family and t["task"] == task and t["trial"] == trial:
                n = i
    return n


def choose(results: Path) -> list[dict]:
    base = load_jsonl(results / "base" / "scores.jsonl")
    key = lambda r: (FAMILY_ORDER.index(r["family"]), r["task"], r["trial"])
    picks = []
    for lang in ("zh", "en"):
        rows = [r for r in base if r["lang"] == lang]
        missed = sorted((r for r in rows if not r["success"] and r["gate"].get("auto_release")), key=key)
        traces = {(t["family"], t["task"], t["trial"]): t for t in load_jsonl(results / "base" / "traces.jsonl")}
        full = [r for r in rows if r["success"] and all(v is not False for v in r["stages"].values())]
        full.sort(key=lambda r: (-len(traces[(r["family"], r["task"], r["trial"])]["turns"]),) + key(r))
        if missed:
            picks.append(("gate-missed", lang, "base", missed[0]))
        if full:
            picks.append(("full-pass", lang, "base", full[0]))
        if missed and missed[0]["family"] in FIX_RUN and (results / FIX_RUN[missed[0]["family"]] / "scores.jsonl").exists():
            run = FIX_RUN[missed[0]["family"]]
            fixed = sorted((r for r in load_jsonl(results / run / "scores.jsonl")
                            if r["task"] == missed[0]["task"] and r["success"]), key=lambda r: r["trial"])
            if fixed:
                picks.append(("after-fix", lang, run, fixed[0]))
    return picks


def replay(results: Path, kind: str, lang: str, run: str, row: dict, commit: str) -> dict:
    traces_path = results / run / "traces.jsonl"
    trace = next(t for t in reversed(load_jsonl(traces_path))
                 if t["family"] == row["family"] and t["task"] == row["task"] and t["trial"] == row["trial"])
    steps, where = build_steps(trace)
    other = {}
    for turn in trace["turns"]:
        if row["family"] == "ad_review":
            other[turn["turn"]] = {"submit_review"} if turn["kind"] == "advice" else {"submit_advice"}
    for s in steps:
        s["checks"] = []
    for c in row["checks"]:
        if c["passed"] is None:
            continue
        i = place(c, where, len(steps) - 1, other)
        zh, en = WHAT[c["id"]]
        steps[i]["checks"].append({"id": c["id"], "stage": c["stage"], "turn": c["turn"], "passed": c["passed"], "core": c["core"],
                                   "what": {"zh": zh, "en": en}, "detail": c["detail"]})
    first = None
    if row.get("first_failure"):
        ff, at = row["first_failure"], row.get("attribution") or {}
        step = next(s["n"] for s in steps if any(x["id"] == ff["id"] and x["turn"] == ff["turn"] for x in s["checks"]))
        zh_why, zh_fix = WHY_ZH.get((run, row["task"], ff["id"]), (None, None))
        first = {"step": step, "check": ff["id"], "stage": ff["stage"], "turn": ff["turn"], "detail": ff["detail"],
                 "why": {"zh": zh_why, "en": at.get("why")}, "cause": dict(CAUSE[at["cause"]], id=at["cause"]) if at.get("cause") else None,
                 "fix": {"zh": zh_fix, "en": at.get("fix")}, "source": at.get("source")}
    rel = f"eval/results/{run}/traces.jsonl"
    return {"id": f"{lang}-{kind}", "lang": lang, "label": LABELS[kind], "selection": kind, "agent": AGENTS[row["family"]],
            "run": run, "family": row["family"], "task": row["task"], "tier": row["tier"], "trial": row["trial"],
            "turns": len(trace["turns"]), "steps": steps, "stage_result": row["stages"], "success": row["success"],
            "first_failure": first, "gate": row["gate"],
            "links": {"record": f"{REPO}/blob/{commit}/{rel}", "record_file": rel,
                      "record_line": record_line(traces_path, row["family"], row["task"], row["trial"])}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(ROOT / "eval" / "results"))
    ap.add_argument("--out", default=str(ROOT / "eval" / "results" / "replay" / "replay.json"))
    ap.add_argument("--commit", required=True, help="commit of the public repository that holds the traces")
    ap.add_argument("--viewer", default="", help="address of the full trace viewer, if published")
    a = ap.parse_args(argv)
    results = Path(a.results)
    picks = choose(results)
    data = {
        "schema": 1,
        "schema_notes": ("Step kinds: user, instruction (an operator note added in a turn), harness (a message from the "
                         "harness inside a turn), model (the agent's own text), tool_call, tool_result, final (the turn's "
                         "answer, summarised). Each step has turn and n; checks sit on the step they check, with passed, "
                         "core and detail exactly as in scores.jsonl; report-only checks have core false. first_failure "
                         "comes from scores.jsonl and the attribution; why.en is the attribution's text. No cost fields."),
        "selection_rule": {k: v for k, v in SELECTION.items()},
        "source": {"repo": REPO, "commit": a.commit, "scores": "eval/results/<run>/scores.jsonl",
                   "traces": "eval/results/<run>/traces.jsonl", "attribution": "eval/results/attribution.md"},
        "stages": STAGES,
        "links": {"viewer": a.viewer or None},
        "replays": [replay(results, kind, lang, run, row, a.commit) for kind, lang, run, row in picks],
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    for r in data["replays"]:
        ff = r["first_failure"]
        print(f"{r['id']}: {r['run']} {r['task']} trial {r['trial']}, {len(r['steps'])} steps, success {r['success']}, "
              f"gate {r['gate'].get('auto_release')}" + (f", first failure {ff['check']} at step {ff['step']}" if ff else ""))
    print("wrote", out)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
