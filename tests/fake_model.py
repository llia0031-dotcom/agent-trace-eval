"""A stand-in for the Messages API, for the offline tests: no network, no key, no cost.

It answers from the task's answer key, in the shape the real API returns (content blocks, stop reason,
usage), and makes the tool calls each agent's instructions ask for, so that a run through the adapters
exercises the agents' own loops, tools and checks. A trial run this way must score as a success:
that is the "answer key scores full marks" test of the rubric.
"""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

from bench import llm


def block(**kw):
    b = NS(**kw)
    b.model_dump = lambda mode="json", exclude_none=True, _kw=kw: dict(_kw)
    return b


def message(blocks, stop):
    return NS(content=blocks, stop_reason=stop, usage=NS(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                                                       cache_read_input_tokens=0))


def tool_use(name, inp, n):
    return block(type="tool_use", id=f"tu_{n}", name=name, input=inp)


class FakeClient:
    """messages.create(**params) -> a message. `policy(params)` decides what to say."""

    def __init__(self, policy):
        self.policy = policy
        self.messages = NS(create=self._create)

    def _create(self, **params):
        return self.policy(params)


def install(policy):
    """Route bench.llm to a fake client (no key is read, nothing is sent)."""
    fake = FakeClient(policy)
    llm.client = lambda: fake
    return fake


# ---------------------------------------------------------------- answers from the keys

def tools_done(params) -> list[str]:
    """Names of the tools already called in the current request (after the last plain user message)."""
    names, ids = [], {}
    for m in params["messages"]:
        if m["role"] == "user" and isinstance(m["content"], str):
            names, ids = [], {}
        for b in (m["content"] if isinstance(m["content"], list) else []):
            b = b if isinstance(b, dict) else b.model_dump()
            if b.get("type") == "tool_use":
                names.append(b["name"])
            if b.get("type") == "text" and m["role"] == "user" and not any(
                    (x if isinstance(x, dict) else x.model_dump()).get("type") == "tool_result" for x in m["content"]):
                names = []
    return names


def ad_policy(task):
    """Ad review agent: read the image, search the policy, check quotes, submit the answer key's review."""
    import sys
    from bench.adapters import ad_review
    a = ad_review.agent()
    item = task["item"]

    def review_from(labels, revised):
        lib = a["library"].by_id()
        text = a["ad_text"](item, revised)
        vs = []
        for l in labels:
            clause = lib[l["clauses"][0]]
            vs.append({"category": l["cat"], "clause_id": clause.id, "ad_quote": l.get("span", ""),
                       "visual_evidence": l.get("visual", l.get("absence", "")), "policy_quote": max(clause.text.splitlines(), key=len)[:60].strip(),
                       "explanation": "违反规定。" if item["lang"] == "zh" else "This breaks the rule."})
        return {"decision": "reject" if vs else "approve", "violations": vs, "notes": ""}

    state = {"n": 0, "turn": 0}

    def policy(params):
        state["n"] += 1
        n = state["n"]
        names = [t["name"] for t in params.get("tools", [])]
        msgs = params["messages"]
        is_result = lambda m: isinstance(m["content"], list) and any(
            (x if isinstance(x, dict) else x.model_dump()).get("type") == "tool_result" for x in m["content"])
        request = next(m for m in reversed(msgs) if m["role"] == "user" and not is_result(m)
                       and "Please finish" not in json.dumps(m["content"], ensure_ascii=False))
        text = json.dumps(request["content"], ensure_ascii=False)
        if state.get("request") is not request:
            state["request"], state["calls"] = request, []
        done = state["calls"]
        advice = "I'm the advertiser" in text or "我是广告主" in text
        revised = "new version" in text
        if advice:
            if "check_revision" in names and "check_revision" not in done:
                done.append("check_revision")
                return message([tool_use("check_revision", {"caption": item["revision"].get("copy", item["copy"]),
                                                            "image_lines": item["revision"].get("image_lines", [])}, n)], "tool_use")
            spans = [l["span"] for l in item["labels"] if "span" in l]
            cut = lambda t: [t := t.replace(sp, "") for sp in spans][-1] if spans else t
            lines = [cut(x) for x in item["image"]["lines"]] if item["kind"] == "image" else []
            ans = {"revised_caption": cut(item["copy"]), "revised_image_lines": lines,
                   "answer": "按规定修改。" if item["lang"] == "zh" else "Change it as follows."}
            return message([tool_use("submit_advice", ans, n)], "tool_use")
        for step in (["read_image_text"] if "read_image_text" in names else []) + ["search_policy", "check_quotes"]:
            if step not in done:
                done.append(step)
                inp = {} if step == "read_image_text" else {"query": "rule"} if step == "search_policy" else {"quotes": []}
                return message([tool_use(step, inp, n)], "tool_use")
        labels = item["revision"]["labels"] if revised else item["labels"]
        return message([tool_use("submit_review", review_from(labels, revised), n)], "tool_use")

    return policy


def _request_text(params) -> str:
    msgs = params["messages"]
    for m in reversed(msgs):
        if m["role"] != "user":
            continue
        c = m["content"]
        if isinstance(c, str):
            return c
        blocks = [b if isinstance(b, dict) else b.model_dump() for b in c]
        if not any(b.get("type") == "tool_result" for b in blocks):
            return " ".join(b.get("text", "") for b in blocks)
    return ""


def _last_tool_output(params) -> str:
    for m in reversed(params["messages"]):
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                b = b if isinstance(b, dict) else b.model_dump()
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    return c if isinstance(c, str) else " ".join(x.get("text", "") for x in c)
    return ""


def text_message(obj):
    return message([block(type="text", text=json.dumps(obj, ensure_ascii=False))], "end_turn")


def biz_policy(task):
    """Business review agent: call overview, then answer with the asked causes, citing one row of the overview."""
    case = task["case"]

    def policy(params):
        if not any(m["role"] == "assistant" for m in params["messages"]):
            return message([tool_use("overview", {}, 1)], "tool_use")
        out = _last_tool_output(params)
        row = next((l for l in out.splitlines() if l.startswith("|") and not set(l) <= set("|-: ") and "指标" not in l), "")
        causes = [{"segment": [{"dimension": d, "value": v} for d, v in c["segment"].items()], "lever": c["lever"],
                   "direction": c["direction"]} for c in case["asked"]["causes"]]
        blocks = [{"kind": "summary", "text": "本周周报如下。", "evidence": []}]
        if causes:
            blocks.append({"kind": "finding", "text": "本周总览见下面这一行：" + row.strip("| ").split("|")[3].strip() + "。",
                           "evidence": [{"source": "q01", "quote": row}]})
        return text_message({"has_anomaly": bool(causes), "causes": causes, "blocks": blocks})

    return policy


def creator_policy(task):
    """Creator workflow: run each request's check tool once (skills setting), then answer from the answer key."""
    import math
    from bench.adapters.creator_lib import lib
    L = lib()

    def which(text):
        if "Pages collected" in text:
            return "topics"
        if "SRT" in text:
            return "captions"
        return "shotlist"

    def answer(kind):
        if kind == "topics":
            c = task["trend"]
            if c["sensitive"]:
                return {"declined": True, "decline_reason": "casualty event", "cards": []}
            cards = [{"kind": "content", "title": f["claim"][:20], "hook": "hook", "angle": "angle", "fit": "fit", "facts": [f],
                      "risk_notes": ""} for f in c["reference_facts"][:c["cards_wanted"]]]
            return {"declined": False, "decline_reason": "", "cards": cards}
        if kind == "shotlist":
            c, ss = task["shot"], L["shot"]["score"]
            sents = L["shot"]["build"].sentences(c["script"])
            shots = []
            for i, s in enumerate(sents, 1):
                d = math.ceil(10 * ss.count_units(s, c["lang"]) / c["speech_rate"] + 1e-9) / 10
                shots.append({"shot": i, "duration_s": d, "visual": "画面" if c["lang"] == "zh" else "shot", "voiceover": s, "on_screen_text": ""})
            total = sum(s["duration_s"] for s in shots)
            lo = c["allowed"][0]
            if total < lo:
                shots.append({"shot": len(shots) + 1, "duration_s": round(lo - total + 0.2, 1), "visual": "B-roll", "voiceover": "", "on_screen_text": ""})
            return {"shots": shots, "notes": ""}
        return L["sub"]["score"].ideal(task["sub"])

    state = {}

    def policy(params):
        text = _request_text(params)
        kind = which(text)
        tools = {t["name"] for t in params.get("tools", [])}
        tool = {"topics": "check_sources", "shotlist": "check_shotlist", "captions": "check_subtitles"}[kind]
        if tool in tools and state.get(kind) is None and params.get("tool_choice", {}).get("type") != "none":
            state[kind] = True
            ans = answer(kind)
            if kind == "topics":
                inp = ans
            elif kind == "shotlist":
                head = "| 镜号 | 时长 | 画面 | 口播 | 字幕 |" if task["lang"] == "zh" else "| Shot | Duration | Visual | Voiceover | On-screen text |"
                rows = [f"| {s['shot']} | {s['duration_s']} | {s['visual']} | {s['voiceover']} | {s['on_screen_text']} |" for s in ans["shots"]]
                inp = {"shotlist": "\n".join([head, "|---|---|---|---|---|"] + rows) + "\n"}
            else:
                inp = {"srt": ans["corrected_srt"]}
            return message([tool_use(tool, inp, len(state))], "tool_use")
        return text_message(answer(kind))

    return policy
