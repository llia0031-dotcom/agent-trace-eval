"""Failure attribution: every failed trial gets a stage (from the checks) and a cause (from the rules below,
or from eval/attribution_notes.yaml when reading the transcript showed something the rules cannot see).

Stage: the earliest failing core check (checks.first_failure).
Cause, one of:
  knowledge     the agent did not have what it needed: the rule, the fact or the method was not in its
                prompt, its skills or anything its tools could return
  tool          a tool was called wrongly, failed, or returned something misleading
  prompt        the instructions were missing, unclear or in conflict for this situation
  model         the instructions and the evidence were there and the model still got it wrong
  interaction   the conversation design: how turns hand over, what a turn is allowed to see or change,
                step limits, output fields
  task          the task or its answer key is at fault (kept in the table, not counted against the agent)

The rules give a default per failing check. A note overrides it for one trial, or for every failed trial
of a task whose first failure is a given check, and carries the reason and the suggested fix.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from . import config

CAUSES = ["knowledge", "tool", "prompt", "model", "interaction", "task"]
CAUSE_ZH = {"knowledge": "知识缺失", "tool": "工具调用", "prompt": "提示词", "model": "模型能力", "interaction": "交互设计",
            "task": "评测题本身"}

# check id -> default cause (with the skills / tools the agent has); the plain creator setting lacks the
# skills, so its knowledge and planning failures default to "knowledge".
DEFAULT = {
    "U-0": "interaction", "P-0": "interaction",
    "U-AD-2": "prompt", "U-BZ-2": "prompt", "U-CR-1": "model", "U-CR-2": "model", "U-CR-3": "interaction", "U-CR-4": "model",
    "P-AD-1": "model", "P-AD-2": "model", "P-AD-3": "model", "P-AD-4": "model", "P-BZ-1": "model", "P-BZ-2": "model",
    "P-CR-1": "model", "P-CR-2": "model",
    "T-1": "tool",
    "K-AD-1": "model", "K-AD-2": "model", "K-AD-3": "knowledge", "K-BZ-1": "model",
    "K-CR-1": "model", "K-CR-2": "model", "K-CR-3": "model", "K-CR-4": "model",
}
DEFAULT_FIX = {
    "prompt": "State the missing rule in the agent's instructions, then re-run the regression set.",
    "model": "Add a check the agent must run and pass before answering, or move this step out of the model into code.",
    "knowledge": "Add the missing rule or fact to the agent's knowledge base or skill, with its source.",
    "tool": "Tighten the tool's input schema and make its error message say how to call it correctly.",
    "interaction": "Change how the turns hand over (what each turn sees and returns), or raise the step limit.",
    "task": "Fix the task or its answer key and record the change.",
}


def load_notes() -> list[dict]:
    p = config.EVAL / "attribution_notes.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")).get("notes", []) if p.exists() else []


def attribute(row: dict, notes: list[dict]) -> dict:
    ff = row["first_failure"] or {"id": "?", "stage": "R", "turn": 0, "detail": ""}
    for n in notes:
        if n.get("run", row["run"]) != row["run"] or n.get("family", row["family"]) != row["family"]:
            continue
        if n.get("task") not in (None, row["task"]) or n.get("check") not in (None, ff["id"]):
            continue
        if n.get("trial") not in (None, row["trial"]):
            continue
        return {"stage": n.get("stage", ff["stage"]), "check": ff["id"], "cause": n["cause"], "why": n["why"],
                "fix": n.get("fix", DEFAULT_FIX[n["cause"]]), "source": "note"}
    cause = DEFAULT.get(ff["id"], "model")
    if row.get("setting") == "plain" and ff["id"].startswith(("K-CR", "P-CR", "U-CR-2")):
        cause = "knowledge"
    return {"stage": ff["stage"], "check": ff["id"], "cause": cause, "why": ff.get("detail", ""), "fix": DEFAULT_FIX[cause], "source": "rule"}
