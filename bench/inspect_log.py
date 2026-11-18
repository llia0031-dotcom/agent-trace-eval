"""Write a run's traces as an Inspect AI log, so they can be browsed with Inspect's own viewer:

  python -m bench inspect-log --run base        -> eval/results/base/inspect/<file>.eval
  inspect view --log-dir eval/results/base/inspect
  inspect view bundle --log-dir eval/results/base/inspect --output-dir <folder>   (a static site)

Nothing is re-run: a replay solver turns each stored trace into Inspect messages (user requests, model
text and tool calls, tool results), and a scorer copies the stored rubric verdicts into Inspect scores
(one per stage, plus task success). The log names no model (`none`): nothing is generated, and the durations Inspect
shows are those of the replay, not of the original runs (their latency is in the traces and the report).

`tools/make_viewer.py` turns these logs, built from the public records, into a static site for GitHub Pages.
"""
from __future__ import annotations

import json

from . import config, trace


def write_log(run: str) -> str:
    from inspect_ai import Task, eval as inspect_eval
    from inspect_ai.dataset import Sample
    from inspect_ai.model import ChatMessageAssistant, ChatMessageSystem, ChatMessageTool, ChatMessageUser, ModelOutput
    from inspect_ai.scorer import Score, mean, scorer
    from inspect_ai.solver import solver
    from inspect_ai.tool import ToolCall

    folder = config.RESULTS / run
    traces = {f"{t['family']}/{t['task']}/t{t['trial']}": t for t in trace.load(folder / "traces.jsonl")}
    scores = {f"{r['family']}/{r['task']}/t{r['trial']}": r
              for r in map(json.loads, (folder / "scores.jsonl").read_text(encoding="utf-8").splitlines())}
    samples = [Sample(id=k, input=k, metadata={"family": t["family"], "task": t["task"], "tier": t["tier"], "lang": t["lang"],
                                               "trial": t["trial"], "gate": t["gate"], "run": run})
               for k, t in sorted(traces.items())]

    @solver
    def replay():
        async def solve(state, generate):
            tr = traces[state.sample_id]
            msgs = []
            for turn in tr["turns"]:
                for s in turn["steps"]:
                    if s["type"] in ("user", "harness"):
                        msgs.append(ChatMessageUser(content=s["text"] or "(empty)"))
                    elif s["type"] == "system_note":
                        msgs.append(ChatMessageSystem(content=s["text"]))
                    elif s["type"] == "model":
                        msgs.append(ChatMessageAssistant(content=s["text"] or "", tool_calls=[
                            ToolCall(id=c["id"], function=c["name"], arguments=c["input"]) for c in s["tool_calls"]] or None))
                    elif s["type"] == "tool":
                        msgs.append(ChatMessageTool(content=str(s["output"]), tool_call_id=s["id"], function=s["name"]))
                if turn["final"] is not None:
                    msgs.append(ChatMessageAssistant(content="Final answer of turn %d (%s):\n%s" % (
                        turn["turn"], turn["kind"], json.dumps(turn["final"], ensure_ascii=False, indent=1))))
            state.messages = msgs
            state.output = ModelOutput.from_content(model="replay", content=json.dumps(tr["turns"][-1]["final"] if tr["turns"] else None,
                                                                                      ensure_ascii=False))
            return state
        return solve

    @scorer(metrics={k: [mean()] for k in ["success", "U", "P", "T", "K", "R", "needs_human"]})
    def rubric():
        async def score(state, target):
            r = scores[state.sample_id]
            value = {"success": float(r["success"]), "needs_human": float(not r["gate"].get("auto_release"))}
            value.update({s: (float(v) if v is not None else 1.0) for s, v in r["stages"].items()})
            failed = [f"{c['id']} ({c['stage']}, turn {c['turn']}): {c['detail']}" for c in r["checks"] if c["passed"] is False]
            return Score(value=value, explanation="\n".join(failed) or "all checks passed",
                         metadata={"first_failure": r.get("first_failure"), "attribution": r.get("attribution")})
        return score

    logs = inspect_eval(Task(name=f"{run} (recorded traces)", dataset=samples, solver=replay(), scorer=rubric()),
                        model="none", log_dir=str(folder / "inspect"), display="none")
    return logs[0].location
