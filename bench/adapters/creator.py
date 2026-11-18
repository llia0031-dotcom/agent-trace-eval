"""Connects the creator skills (creator-skills: trend-topic-check, shotlist-check, subtitle-check) to the bench.

The workflow is one conversation with three requests: topic cards for a trend, a shot list for the
creator's approved script, then a check of the editor's auto captions against that script. With the
"skills" setting the model has all three frozen SKILL.md files loaded and the three checking tools, the
way an agent with the three skills installed works; with "plain" it has each skill's one-line plain
instruction and no tools (the comparison arm of the creator-skills evaluations).

Prompts, output schemas, tool handlers and scorers are those of the creator-skills evaluation code,
loaded by path (bench.adapters.creator_lib). This file only puts the three requests in one conversation,
records the trace and stores what the three scorers said.
"""
from __future__ import annotations

import json
import re
import unicodedata

from .. import config, llm, trace
from .creator_lib import CASE_ROOT, lib

FAMILY = "creator"
MAX_TOOL_ROUNDS = 6  # tool rounds per request; after that the model must answer (tool_choice none)
KINDS = ["topics", "shotlist", "captions"]
# Words of the casualty event that must not appear in the unrelated product video of the same conversation.
OFF_LIMITS = {"C03": ["山洪", "清水溪", "遇难", "失联", "伤亡", "鹿鸣县"],
              "C07": ["Mill Road", "flat fire", "died", "dead", "casualt", "injured"]}


def load_tasks() -> list[dict]:
    data = json.loads((CASE_ROOT / "cases.json").read_text(encoding="utf-8"))["cases"]
    out = []
    for c in data:
        shot = dict(c["shot"])
        shot["script"] = (CASE_ROOT / "shot" / shot["script_file"]).read_text(encoding="utf-8")
        sub = dict(c["sub"])
        sub["srt_text"] = (CASE_ROOT / sub["files"]["subtitles"]).read_text(encoding="utf-8")
        sub["script_text"] = (CASE_ROOT / sub["files"]["script"]).read_text(encoding="utf-8")
        out.append({"id": c["id"], "family": FAMILY, "tier": c["tier"], "lang": c["lang"],
                    "trend": c["trend"], "shot": shot, "sub": sub, "off_limits": OFF_LIMITS.get(c["id"], [])})
    return out


def _parts():
    L = lib()
    return L["trend"]["run"], L["shot"]["run"], L["sub"]["run"], L["runner"]


def tools() -> list[dict]:
    tr, sr, ur, _ = _parts()
    return [tr.TOOL, sr.TOOL, ur.TOOL]


def configure(cfg: dict) -> dict:
    import hashlib
    setting = cfg.get("creator_setting", "skills")
    sample = load_tasks()[0]
    return {"setting": setting, "tools": tools() if setting == "skills" else [],
            "system_sha": hashlib.sha256(system_prompt(sample, setting, cfg.get("creator_system_addition", "")).encode("utf-8")).hexdigest()[:12],
            "system_addition": cfg.get("creator_system_addition", ""), "max_tool_rounds": MAX_TOOL_ROUNDS}


def system_prompt(task: dict, setting: str, addition: str = "") -> str:
    tr, sr, ur, runner = _parts()
    if setting == "plain":
        text = "\n".join([tr.PLAIN_SYSTEM, sr.PLAIN["a"], ur.PLAIN, "Reply in the JSON format each request asks for."])
    else:
        L = lib()
        parts = ["You have the following skills loaded. For each request, follow the skill that fits it.", ""]
        for skill, name in (("trend", "trend-topic-check"), ("shot", "shotlist-check"), ("sub", "subtitle-check")):
            body = runner.skill_body(L[skill]["run"].FROZEN_DIR / "SKILL.md")
            parts += [f'<skill name="{name}">', body, "</skill>", ""]
        t = task["trend"]
        parts += ["Tools:",
                  "- trend-topic-check: " + tr.TOOL_NOTE.format(today=t["today"], lang=t["lang"]),
                  "- shotlist-check: " + sr.TOOL_NOTE + " " + sr.EXTRA["a"],
                  "- subtitle-check: " + ur.TOOL_NOTE + " Put the complete corrected file in corrected_srt; if nothing needs fixing, return the user's file unchanged.",
                  "", "When you are done with a request, reply with the final result in the JSON format it asks for."]
        text = "\n".join(parts)
    return text + (("\n\n" + addition) if addition else "")


def _handle(task: dict, name: str, inp: dict) -> tuple[str, bool]:
    tr, sr, ur, _ = _parts()
    if name == "check_sources":
        return tr.handle(name, inp, task["trend"])
    if name == "check_shotlist":
        return sr.handle(name, inp, task["shot"])
    if name == "check_subtitles":
        return ur.handle(name, inp, task["sub"])
    return f"unknown tool {name}", True


def _final_json(content: list[dict]):
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
    if not text:
        return None, "no text in the final message"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, f"final message is not valid JSON: {e}"


def _request(task, n: int) -> tuple[str, dict]:
    tr, sr, ur, _ = _parts()
    if n == 0:
        return tr.user_message(task["trend"]), tr.SCHEMA
    if n == 1:
        return sr.user_a(task["shot"]), sr.SCHEMA["a"]
    return ur.user(task["sub"]), ur.SCHEMA


def run_trial(task: dict, cfg: dict) -> dict:
    setting = cfg.get("creator_setting", "skills")
    system = system_prompt(task, setting, cfg.get("creator_system_addition", ""))
    use_tools = setting == "skills"
    messages, starts, finals, errors = [], [], [], []
    for n in range(3):
        text, schema = _request(task, n)
        starts.append(len(messages))
        messages.append({"role": "user", "content": text})
        rounds, final, error = 0, None, None
        while True:
            params = dict(system=system, messages=messages, output_config={"effort": config.EFFORT, "format": {"type": "json_schema", "schema": schema}})
            if use_tools:
                params["tools"] = tools()
                if rounds >= MAX_TOOL_ROUNDS:
                    params["tool_choice"] = {"type": "none"}
            try:
                resp = llm.create(**params)
            except Exception as e:
                error = f"{type(e).__name__}: {str(e)[:300]}"
                break
            content = trace.as_blocks(resp.content)
            messages.append({"role": "assistant", "content": content})
            if resp.stop_reason == "tool_use":
                rounds += 1
                results = []
                for b in content:
                    if b.get("type") == "tool_use":
                        out, is_err = _handle(task, b["name"], b.get("input") or {})
                        results.append({"type": "tool_result", "tool_use_id": b["id"], "content": out, "is_error": is_err})
                messages.append({"role": "user", "content": results})
                continue
            if resp.stop_reason != "end_turn":
                error = f"stopped: {resp.stop_reason}"
            final, perr = _final_json(content)
            error = error or perr
            break
        finals.append(final)
        errors.append(error)
        if final is None:
            break
    turns = trace.turns_from_messages(messages, llm._local.calls, starts, KINDS[:len(starts)])
    for t, f, e in zip(turns, finals, errors):
        t["final"], t["error"] = f, e
    _score(task, turns)
    gate = _gate(task, turns)
    return {"turns": turns, "gate": gate, "error": next((e for e in errors if e), None),
            "extras": {"setting": setting, "expected_turns": 3, "sensitive": task["trend"]["sensitive"],
                       "shot_kind": task["shot"]["kind"]}}


# ---------------------------------------------------------------- scoring with the creator-skills scorers

def _mentions(text: str, words: list[str]) -> list[str]:
    t = unicodedata.normalize("NFKC", text).lower()
    return [w for w in words if w.lower() in t]


def _score(task: dict, turns: list[dict]) -> None:
    L = lib()
    ts, ss, us = L["trend"]["score"], L["shot"]["score"], L["sub"]["score"]
    for t in turns:
        out = t["final"]
        if t["kind"] == "topics":
            case = task["trend"]
            r = ts.score_run({"case": task["id"], "arm": "x", "repeat": 1, "output": out}, case, ts.case_pages(case))
            if not r["failed"]:
                passed, total = ts.script_pass({"output": out}, case)
                r["script_pass"], r["script_cards"] = passed, total
            t["outcome"] = r
        elif t["kind"] == "shotlist":
            r = ss.score_a(task["shot"], out)
            shots = (out or {}).get("shots") or []
            words = " ".join(f"{s.get('visual', '')} {s.get('voiceover', '')} {s.get('on_screen_text', '')}" for s in shots)
            # the video only: what is shown, said and written on screen (a note to the creator is not part of it)
            r["off_limits_mentioned"] = _mentions(words, task["off_limits"])
            t["outcome"] = r
        else:
            t["outcome"] = us.score_run(task["sub"], {"output": out, "error": t["error"]}) if out else {"valid": False, "perfect": False}


def _gate(task: dict, turns: list[dict]) -> dict:
    """The checks a deployment would run on the three deliverables before they go out (the skills' own
    frozen scripts), whether or not the model had the skills."""
    L = lib()
    by = {t["kind"]: t for t in turns}
    gate = {"complete": len(turns) == 3 and all(t["final"] is not None and not t["error"] for t in turns)}
    top = by.get("topics", {}).get("outcome") or {}
    if top.get("failed", True):
        gate["topics_sources_pass"] = False
    else:
        declined = bool(top.get("declined"))
        gate["topics_sources_pass"] = (declined and not top.get("cards")) or (
            top.get("script_cards", 0) > 0 and top.get("script_pass") == top.get("script_cards"))
    shot = by.get("shotlist")
    if shot and shot["final"] and shot["final"].get("shots"):
        ss = L["shot"]["score"]
        code, _ = ss.run_frozen(task["shot"], ss.md_from_output(task["shot"], shot["final"]))
        gate["shotlist_check_pass"] = code == 0
    else:
        gate["shotlist_check_pass"] = False
    cap = by.get("captions")
    if cap and cap["final"] and cap["final"].get("corrected_srt"):
        _, _, ur, runner = _parts()
        sub = task["sub"]
        code, out = runner.run_script(ur.SCRIPT, ["fixed.srt", "--script", "script.txt", "--original", "subtitles.srt",
                                                  "--lang", sub["lang"], "--format", "json"],
                                      {"fixed.srt": cap["final"]["corrected_srt"], "script.txt": sub["script_text"],
                                       "subtitles.srt": sub["srt_text"]})
        try:
            rep = json.loads(out.split("\n[stderr]\n")[0])
            changed = [x for x in rep.get("timing", []) if x.get("check") in ("timing_changed", "cue_count_changed")]
            gate["captions_timing_kept"] = not changed
        except (json.JSONDecodeError, AttributeError):
            gate["captions_timing_kept"] = False
    else:
        gate["captions_timing_kept"] = False
    gate["auto_release"] = all(gate.values())
    return gate


def cjk_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum("㐀" <= c <= "鿿" for c in letters) / len(letters) if letters else 0.0


TAG = re.compile(r"\s+")
