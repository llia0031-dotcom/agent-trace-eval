"""The rubric as code: per-step and per-turn checks over a stored trace, grouped by stage.

Stages, in the order a request is worked through:
  U  understanding   the agent answered the request that was made, within its stated constraints
  P  planning        it took the steps its own instructions require, in a workable order, within budget
  T  tool calls      its calls name an offered tool, match the tool's input schema and succeed
  K  knowledge       what it cites (policy text, query results, web pages, the approved script) is real and used as written
  R  result          the deliverable matches the answer fixed when the task was written

Every check returns passed = True, False, or None (does not apply). Checks marked core=False are
reported but not used for a stage's pass/fail or for attribution. All checks are rules; none calls a
model. The model-judged rubric items are in bench/judge.py and are reported separately.
rubric/RUBRIC.md lists every check with its pass condition.
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache

from . import trace

STAGES = ["U", "P", "T", "K", "R"]
STAGE_NAMES = {"U": "Understanding", "P": "Planning", "T": "Tool calls", "K": "Knowledge and citation", "R": "Result quality"}
# What a tool returns, as text, when the call did not work (the agents do not all set is_error).
ERROR_TEXT = re.compile(r"^(No clause \S+ in the|Unknown tool|unknown tool|This ad has no image\.|没有这个工具|出错：|exit code:? 2\b)")


def check(cid, stage, turn, passed, detail="", core=True):
    return {"id": cid, "stage": stage, "turn": turn, "passed": passed, "detail": detail, "core": core}


# ---------------------------------------------------------------- tool-call validity (all families)

def _type_ok(v, t) -> bool:
    return {"object": isinstance(v, dict), "array": isinstance(v, list), "string": isinstance(v, str),
            "integer": isinstance(v, int) and not isinstance(v, bool), "boolean": isinstance(v, bool), "null": v is None,
            "number": isinstance(v, (int, float)) and not isinstance(v, bool)}.get(t, True)


def schema_problems(value, schema: dict, path: str = "input") -> list[str]:
    """A small JSON Schema check covering what the agents' tool schemas use: type, required, properties,
    additionalProperties false, enum, items, anyOf."""
    if "anyOf" in schema:
        return [] if any(not schema_problems(value, s, path) for s in schema["anyOf"]) else [f"{path}: matches no allowed form"]
    out = []
    t = schema.get("type")
    if t and not _type_ok(value, t):
        return [f"{path}: expected {t}"]
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} not one of {schema['enum'][:8]}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                out.append(f"{path}.{k}: missing")
        for k, v in value.items():
            if k in props:
                out += schema_problems(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                out.append(f"{path}.{k}: not allowed")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            out += schema_problems(v, schema["items"], f"{path}[{i}]")
    return out


def tool_call_verdicts(turn: dict, offered: dict) -> list[dict]:
    """One verdict per tool call: known tool, input valid, call succeeded; and whether it repeats an earlier call."""
    out, seen = [], set()
    for s in trace.tool_steps(turn):
        known = s["name"] in offered
        problems = schema_problems(s["input"], offered[s["name"]]) if known else [f"{s['name']} is not an offered tool"]
        failed = bool(s["is_error"]) or bool(ERROR_TEXT.match(str(s["output"]).strip()))
        sig = (s["name"], json.dumps(s["input"], sort_keys=True, ensure_ascii=False))
        out.append({"name": s["name"], "known": known, "input_ok": not problems, "problems": problems, "succeeded": not failed,
                    "correct": known and not problems and not failed, "repeat": sig in seen})
        seen.add(sig)
    return out


def tool_checks(t: dict, offered: dict, other_finish: set | None = None) -> list[dict]:
    """other_finish: finishing tools that belong to another kind of request (ad review: submit_advice in a review
    turn). Calling one is refused by the agent's harness; it is a wrong call (T-2, T-4) but does not stop the turn's
    own work, so it is kept out of T-1."""
    n = t["turn"]
    v = tool_call_verdicts(t, offered)
    if not v:
        return [check("T-1", "T", n, None, "no tool calls")]
    other_finish = other_finish or set()
    bad = [x for x in v if not x["correct"]]
    misplaced = [x for x in v if x["name"] in other_finish]
    # an unsuccessful call counts against the stage only if no later call of the same tool in the turn succeeded
    unrecovered = [x for i, x in enumerate(v) if not x["correct"] and x["name"] not in other_finish
                   and not any(y["name"] == x["name"] and y["correct"] for y in v[i + 1:])]
    out = [check("T-1", "T", n, not unrecovered, "; ".join(f"{x['name']}: {x['problems'] or 'returned an error'}" for x in unrecovered)),
           check("T-2", "T", n, not bad, f"{len(v) - len(bad)}/{len(v)} calls correct", core=False),
           check("T-3", "T", n, not any(x["repeat"] for x in v), f"{sum(x['repeat'] for x in v)} exact repeats", core=False)]
    if other_finish:
        out.append(check("T-4", "T", n, not misplaced, f"called {', '.join(sorted({x['name'] for x in misplaced}))}, which is for another kind of request"
                         if misplaced else "", core=False))
    return out


def _called(t: dict, *names) -> list[dict]:
    return [s for s in trace.tool_steps(t) if s["name"] in names]


def _over_budget(t: dict) -> bool:
    e = str(t.get("error") or "")
    return "after" in e and "rounds" in e or "within" in e and "model calls" in e


# ---------------------------------------------------------------- agentevals: required tools present

@lru_cache(maxsize=1)
def _superset():
    from agentevals.trajectory.match import create_trajectory_match_evaluator
    return create_trajectory_match_evaluator(trajectory_match_mode="superset", tool_args_match_mode="ignore")


def _openai_messages(names: list[str]) -> list[dict]:
    calls = [{"id": str(i), "type": "function", "function": {"name": n, "arguments": "{}"}} for i, n in enumerate(names)]
    msgs = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "", "tool_calls": calls}]
    msgs += [{"role": "tool", "tool_call_id": str(i), "content": ""} for i in range(len(names))]
    return msgs + [{"role": "assistant", "content": "answer"}]


def has_required_tools(t: dict, required: list[str]) -> bool:
    """agentevals' superset trajectory match: the turn's tool calls include every required tool (any order, any arguments)."""
    if not required:
        return True
    names = [s["name"] for s in trace.tool_steps(t)]
    r = _superset()(outputs=_openai_messages(names), reference_outputs=_openai_messages(required))
    return bool(r["score"])


# ---------------------------------------------------------------- language

def cjk_share(text: str) -> float:
    letters = [c for c in str(text) if c.isalpha()]
    return sum("㐀" <= c <= "鿿" for c in letters) / len(letters) if letters else 0.0


def lang_ok(text: str, lang: str) -> bool | None:
    if not str(text).strip():
        return None
    share = cjk_share(text)
    return share >= 0.5 if lang == "zh" else share < 0.05


# ---------------------------------------------------------------- ad review

def checks_ad(tr: dict, task: dict, offered: dict) -> list[dict]:
    item = task["item"]
    out = []
    expected = 3 if item["tier"] == "L3" else 1
    out.append(check("U-0", "U", 0, len(tr["turns"]) == expected and all(t["final"] is not None for t in tr["turns"]),
                     f"{sum(t['final'] is not None for t in tr['turns'])}/{expected} requests answered"))
    for t in tr["turns"]:
        n, o, f = t["turn"], t.get("outcome", {}), t["final"]
        tools = offered["image" if (item["kind"] == "image") else "text"]
        other = {"submit_review"} if t["kind"] == "advice" else {"submit_advice"}
        out += tool_checks(t, {x["name"]: x["input_schema"] for x in tools}, other)
        out.append(check("P-0", "P", n, not _over_budget(t), t.get("error") or ""))
        if t["kind"] == "advice":
            out.append(check("U-AD-2", "U", n, lang_ok((f or {}).get("answer", ""), item["lang"]) if f else None, "answer in the ad's language"))
            out.append(check("P-AD-4", "P", n, bool(_called(t, "check_revision")) if f else None, "check_revision before submit_advice"))
            if f:
                out += [check("R-AD-3", "R", n, o["spans_removed"], "offending words removed from the corrected version"),
                        check("R-AD-4", "R", n, o["facts_kept"], "brand and price kept"),
                        check("R-AD-5", "R", n, not o["placeholders"] and not o["field_problems"],
                              "; ".join(o["placeholders"] + o["field_problems"]))]
            else:
                out.append(check("R-AD-3", "R", n, False, "no advice"))
            continue
        expl = " ".join(v.get("explanation", "") for v in (f or {}).get("violations", []))
        out.append(check("U-AD-2", "U", n, lang_ok(expl, item["lang"]) if f else None, "explanations in the ad's language"))
        image_turn = item["kind"] == "image"
        out.append(check("P-AD-1", "P", n, bool(_called(t, "read_image_text")) if image_turn and f else None, "image text read before the decision"))
        if t["kind"] == "review":
            out.append(check("P-AD-2", "P", n, bool(_called(t, "search_policy", "get_clause")) if f else None, "policy looked up"))
        named = bool((f or {}).get("violations"))
        out.append(check("P-AD-3", "P", n, has_required_tools(t, ["check_quotes"]) if f and named else None,
                         "quotes checked before the decision (agentevals superset match)"))
        if f:
            ad_q = [q for q in o["quotes"] if q["side"] == "ad"]
            pol_q = [q for q in o["quotes"] if q["side"] == "policy"]
            out.append(check("K-AD-1", "K", n, all(q["status"] == "supported" for q in ad_q) if ad_q else None,
                             "; ".join(f"{q['status']}: {q['quote'][:40]}" for q in ad_q if q["status"] != "supported")))
            out.append(check("K-AD-2", "K", n, all(q["status"] == "supported" for q in pol_q) if pol_q else None,
                             "; ".join(f"{q['clause']} {q['status']}" for q in pol_q if q["status"] != "supported")))
            found = [l for l in o["labels"] if l["found"]]
            out.append(check("K-AD-3", "K", n, all(l["clause_right"] for l in found) if found else None,
                             "; ".join(f"{l['cat']}: cited a clause outside {l['accepted_clauses']}" for l in found if not l["clause_right"])))
            out.append(check("R-AD-1", "R", n, o["decision_right"], f"answer {'reject' if o['gold_reject'] else 'approve'}, "
                                                                       f"agent {'reject' if o['pred_reject'] else 'approve'}"))
            missed = [l["cat"] for l in o["labels"] if not l["found"]]
            out.append(check("R-AD-2", "R", n, not missed and not o["extra_cats"],
                             f"missed {missed}, extra {o['extra_cats']}" if missed or o["extra_cats"] else ""))
        else:
            out.append(check("R-AD-1", "R", n, False, t.get("error") or "no review"))
    return out


# ---------------------------------------------------------------- business review

DRILL = ("breakdown", "funnel", "run_sql")


def checks_biz(tr: dict, task: dict, offered: dict) -> list[dict]:
    case = task["case"]
    out = []
    t = tr["turns"][0] if tr["turns"] else {"turn": 1, "steps": [], "final": None, "error": tr.get("error"), "outcome": {}}
    n, f, o = 1, t["final"], t.get("outcome", {})
    out.append(check("U-0", "U", 0, f is not None, t.get("error") or ""))
    out += tool_checks(t, {x["name"]: x["input_schema"] for x in offered["tools"]})
    out.append(check("P-0", "P", n, not _over_budget(t), t.get("error") or ""))
    tools = trace.tool_steps(t)
    out.append(check("P-BZ-1", "P", n, bool(tools) and tools[0]["name"] == "overview", "overview called first"))
    real = [c for c in (f or {}).get("causes", []) if c.get("lever") != "holiday"]
    out.append(check("P-BZ-2", "P", n, bool(_called(t, *DRILL)) if f and real else None, "drilled down before naming a cause"))
    if case["request_kind"] == "scoped":
        asked = {c["lever"] for c in case["asked"]["causes"]}
        outside = sorted({c.get("lever") for c in (f or {}).get("causes", [])} - asked)
        out.append(check("U-BZ-2", "U", n, not outside if f else None, f"causes outside the question: {outside}" if outside else ""))
    chk = o.get("check")
    out.append(check("K-BZ-1", "K", n, bool(chk and chk["released"]) if f else None,
                     "" if not chk else f"{chk['supported']}/{chk['checked']} paragraphs with numbers backed by query rows"
                     + (f"; {chk['report_problems']}" if chk.get("report_problems") else "")))
    first = o.get("first_check")
    out.append(check("K-BZ-2", "K", n, bool(first and first["released"]) if f else None, "released on the first draft", core=False))
    a = o.get("asked") or {}
    if f:
        out += [check("R-BZ-1", "R", n, a.get("detection_right"), "said whether there is an anomaly correctly"),
                check("R-BZ-2", "R", n, a.get("located_all"),
                      f"located {a.get('located')}/{a.get('truth_events')}, coarser {a.get('coarser')}, extra {a.get('extra')}"),
                check("R-BZ-3", "R", n, a.get("levers_right") if a.get("located_all") else None, "levers right")]
    else:
        out.append(check("R-BZ-1", "R", n, False, t.get("error") or "no report"))
    return out


# ---------------------------------------------------------------- creator workflow

CHECK_TOOL = {"topics": "check_sources", "shotlist": "check_shotlist", "captions": "check_subtitles"}
BAD_FLAGS = ["url_not_provided", "dead_link", "stale_page", "quote_not_on_page", "number_not_in_quote", "title_mismatch",
             "date_mismatch", "quote_too_long"]


def checks_creator(tr: dict, task: dict, offered: dict) -> list[dict]:
    out = []
    skills = tr["extras"].get("setting", "skills") == "skills"
    out.append(check("U-0", "U", 0, len(tr["turns"]) == 3 and all(t["final"] is not None for t in tr["turns"]),
                     f"{sum(t['final'] is not None for t in tr['turns'])}/3 requests answered"))
    tools = {x["name"]: x["input_schema"] for x in offered.get("tools", [])}
    for t in tr["turns"]:
        n, f, o, kind = t["turn"], t["final"], t.get("outcome", {}), t["kind"]
        if skills:
            out += tool_checks(t, tools)
            calls = _called(t, CHECK_TOOL[kind])
            out.append(check("P-CR-1", "P", n, bool(calls) if f else None, f"{CHECK_TOOL[kind]} run before answering"))
            if kind == "captions":
                verified = any(c["input"].get("srt") for c in calls)
            else:
                verified = bool(calls) and str(calls[-1]["output"]).startswith(("exit code 0", "exit code: 0"))
            out.append(check("P-CR-2", "P", n, verified if f else None, "the last check before answering passed" if kind != "captions"
                             else "the corrected file was checked"))
            rounds = sum(1 for s in trace.model_steps(t) if s["tool_calls"])
            out.append(check("P-0", "P", n, rounds < 6, f"{rounds} tool rounds"))
        if f is None:
            out.append(check("R-CR-0", "R", n, False, t.get("error") or "no answer"))
            continue
        if kind == "topics":
            case = task["trend"]
            cards = o.get("cards", [])
            flags = sorted({fl for c in cards for ff in c["flags"] for fl in ff})
            bad = [b for c in cards for b in c["bad_figures"]]
            if case["sensitive"]:
                out.append(check("U-CR-2", "U", n, o.get("sensitive_ok"), "casualty event: declined or public-service cards only"))
            else:
                out.append(check("U-CR-1", "U", n, (not o.get("declined")) and len(cards) == case["cards_wanted"],
                                 f"{len(cards)} cards, {case['cards_wanted']} asked" + (", declined" if o.get("declined") else "")))
            out.append(check("K-CR-1", "K", n, not flags if cards else None, ", ".join(flags)))
            out.append(check("K-CR-2", "K", n, not bad and not any(c["rumour_cites"] for c in cards) if cards else None,
                             "; ".join(f"{b['type']}: {b['old']}" for b in bad)))
            ok = o.get("sensitive_ok") if case["sensitive"] else (
                not o.get("declined") and len(cards) == case["cards_wanted"] and all(c["clean"] for c in cards))
            out.append(check("R-CR-1", "R", n, bool(ok), f"{sum(c['clean'] for c in cards)}/{len(cards)} cards clean"))
        elif kind == "shotlist":
            if task["off_limits"]:
                # the video only: what is shown, said and written on screen (a note to the creator is not part of it)
                video = " ".join(f"{s.get('visual', '')} {s.get('voiceover', '')} {s.get('on_screen_text', '')}" for s in f.get("shots") or [])
                said = [w for w in task["off_limits"] if w.lower() in unicodedata.normalize("NFKC", video).lower()]
                out.append(check("U-CR-3", "U", n, not said, f"mentions the casualty event: {said}" if said else ""))
            out.append(check("K-CR-3", "K", n, o.get("coverage_exact"), "voiceover is the approved script, word for word"))
            out.append(check("R-CR-2", "R", n, o.get("all_pass"),
                             f"total {o.get('total_s')} s; overflow {o.get('overflow_shots')}; slow text {o.get('slow_text_shots')}; "
                             f"in range {o.get('total_in_range')}"))
        else:
            out.append(check("U-CR-4", "U", n, o.get("timings_kept"), "timings and cue count unchanged"))
            out.append(check("K-CR-4", "K", n, o.get("skip_inserted", 1) == 0 and o.get("clean_changed", 1) == 0
                             and o.get("adlib_kept", 0) == o.get("adlib_total", 0),
                             f"inserted skipped lines {o.get('skip_inserted')}; changed clean cues {o.get('clean_changed')}; "
                             f"ad-libs kept {o.get('adlib_kept')}/{o.get('adlib_total')}"))
            out.append(check("R-CR-3", "R", n, o.get("perfect"),
                             f"transcription errors fixed {o.get('asr_fixed')}/{o.get('asr_total')}; missed {o.get('missed_asr')}"))
    return out


FAMILY_CHECKS = {"ad_review": checks_ad, "biz": checks_biz, "creator": checks_creator}


# ---------------------------------------------------------------- trial verdicts

def stage_verdicts(checks: list[dict]) -> dict:
    """Per stage: True (every core check that applies passed), False, or None (nothing applied)."""
    out = {}
    for s in STAGES:
        vals = [c["passed"] for c in checks if c["stage"] == s and c["core"] and c["passed"] is not None]
        out[s] = None if not vals else all(vals)
    return out


def first_failure(checks: list[dict]) -> dict | None:
    """The earliest failing core check: first by turn, then by stage order U, P, T, K, R.

    Within a turn the stages follow the order a request is worked through; a failure in an earlier turn
    comes first because later turns build on it.
    """
    bad = [c for c in checks if c["core"] and c["passed"] is False]
    if not bad:
        return None
    return min(bad, key=lambda c: (c["turn"], STAGES.index(c["stage"])))


def outcome_pass(checks: list[dict]) -> bool:
    """Task success: every result check passed (the answer key decides; process checks do not)."""
    rs = [c for c in checks if c["stage"] == "R" and c["passed"] is not None]
    return bool(rs) and all(c["passed"] for c in rs) and not any(c["id"] == "U-0" and c["passed"] is False for c in checks)
