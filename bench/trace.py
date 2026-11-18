"""The record of one trial: every request, model call, tool call and tool result, in order.

A trace is plain JSON, one per line of traces.jsonl:

  {"run", "family", "task", "tier", "lang", "config", "trial", "mode",
   "turns": [{"turn", "kind", "steps": [...], "final": {...} | null, "error": str | null, "outcome": {...}}],
   "error", "usage": {...}, "latency_s", "gate": {...}, "extras": {...}}

Step types
  user         the request of the turn (images are named, not embedded)
  system_note  an operator instruction added mid-conversation
  model        one model call: its text, the tool calls it asked for, stop reason, tokens, seconds
  tool         one tool call with its input, output and whether it returned an error
  harness      a message the harness sent inside a turn (a reminder to finish, feedback from a check)

`outcome` holds what the task's own scorer said about the result of a turn (computed when the trial
ran, with the agent project's code and the answer key). Everything else the bench reports is
computed later from the steps.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


def as_blocks(content) -> list[dict]:
    """Message content as a list of plain dicts (SDK objects, dicts and strings are all accepted)."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    for b in content or []:
        if isinstance(b, dict):
            out.append(b)
        else:
            out.append(b.model_dump(mode="json", exclude_none=True))
    return out


def _text_of(blocks: list[dict]) -> str:
    parts = []
    for b in blocks:
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "image":
            parts.append(f"[image: {b.get('name', 'attached')}]")
    return "\n".join(p for p in parts if p)


def _result_text(block: dict) -> str:
    c = block.get("content", "")
    return c if isinstance(c, str) else _text_of(as_blocks(c))


def turns_from_messages(messages: list[dict], calls: list[dict], turn_starts: list[int], kinds: list[str]) -> list[dict]:
    """Convert an Anthropic-style conversation into turns of steps.

    messages     the conversation as the agent kept it
    calls        one record per model call (see llm.create), in order
    turn_starts  index in `messages` of the user message that opens each turn
    kinds        a label per turn (review, advice, weekly, topic, ...)
    """
    turns, call_i = [], 0
    bounds = list(turn_starts) + [len(messages)]
    for t, (start, end) in enumerate(zip(bounds, bounds[1:]), 1):
        steps, pending = [], {}
        for mi in range(start, end):
            m = messages[mi]
            blocks = as_blocks(m.get("content"))
            if m["role"] == "system":
                steps.append({"type": "system_note", "text": _text_of(blocks)})
            elif m["role"] == "assistant":
                call = calls[call_i] if call_i < len(calls) else {}
                call_i += 1
                tool_calls = [{"id": b["id"], "name": b["name"], "input": b.get("input") or {}} for b in blocks if b.get("type") == "tool_use"]
                for tc in tool_calls:
                    pending[tc["id"]] = tc
                steps.append({"type": "model", "text": _text_of(blocks), "tool_calls": tool_calls,
                              "stop_reason": call.get("stop_reason"), "seconds": call.get("seconds"),
                              "usage": {k: call.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")},
                              "cost_usd": call.get("cost_usd", 0.0)})
            else:
                results = [b for b in blocks if b.get("type") == "tool_result"]
                for b in results:
                    tc = pending.pop(b.get("tool_use_id"), {"name": "?", "input": {}})
                    steps.append({"type": "tool", "id": b.get("tool_use_id"), "name": tc["name"], "input": tc["input"],
                                  "output": _result_text(b), "is_error": bool(b.get("is_error"))})
                rest = [b for b in blocks if b.get("type") != "tool_result"]
                if rest:
                    steps.append({"type": "user" if mi == start else "harness", "text": _text_of(rest)})
        turns.append({"turn": t, "kind": kinds[t - 1], "steps": steps, "final": None, "error": None, "outcome": {}})
    return turns


def usage_of(calls: list[dict]) -> dict:
    keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    u = {"calls": len(calls), **{k: sum(c[k] for c in calls) for k in keys}}
    u["cost_usd"] = round(sum(c["cost_usd"] for c in calls), 6)
    from .llm import cost_of
    u["list_cost_usd"] = round(sum(cost_of(c, batch=False) for c in calls), 6)  # the same tokens at the standard price
    u["model_seconds"] = round(sum(c["seconds"] for c in calls), 2)
    return u


# ---------------------------------------------------------------- reading traces

def tool_steps(turn: dict) -> list[dict]:
    return [s for s in turn["steps"] if s["type"] == "tool"]


def model_steps(turn: dict) -> list[dict]:
    return [s for s in turn["steps"] if s["type"] == "model"]


def all_tool_steps(trace: dict) -> Iterator[tuple[dict, dict]]:
    for turn in trace["turns"]:
        for s in tool_steps(turn):
            yield turn, s


def load(path: Path) -> list[dict]:
    """All traces of a file; a later line for the same (task, config, trial) replaces an earlier one."""
    last = {}
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                t = json.loads(line)
                last[(t["family"], t["task"], t["config"]["name"], t["trial"])] = t
    return list(last.values())


def append(path: Path, trace: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(trace, ensure_ascii=False) + "\n")
