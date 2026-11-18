"""Hashes of the files a pre-registered run depends on.

`python -m bench freeze` writes eval/frozen.json; `python -m bench check-frozen` compares. The list covers
the tasks and answer keys, the rubric code, the run configurations, and the agent code imported from the
three agent projects (which this repository does not contain; a missing file is reported, not failed, so
scoring stored traces works without them).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import config

OWN = ["tasks/ad_review/items.yaml", "tasks/ad_review/ocr.json", "tasks/biz/cases.yaml", "tasks/creator/specs.py",
       "tasks/creator/cases.json", "bench/checks.py", "bench/metrics.py", "bench/adapters/ad_review.py", "bench/adapters/biz.py",
       "bench/adapters/creator.py", "bench/adapters/creator_lib.py", "bench/llm.py", "bench/trace.py", "bench/run.py", "eval/configs.yaml"]
AGENT_FILES = {
    "grounded-review": (lambda: config.AD_REVIEW_DIR, ["agent/review_agent.py", "agent/common.py", "policies/clauses.yaml",
                                                         "policies/library.py", "eval/score.py", "testset/load.py"]),
    "grounded-business-review": (lambda: config.BIZ_DIR, ["bizagent/arms.py", "bizagent/prompts.py", "bizagent/tools.py",
                                                           "bizagent/evidence.py", "bizagent/score.py", "bizagent/scenarios.py",
                                                           "bizagent/world.py", "vendor/evidence-check/check_evidence.py"]),
}


def sha12(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]


def current() -> dict:
    out = {}
    for f in OWN:
        p = config.ROOT / f
        out[f] = sha12(p) if p.exists() else None
    for name, (root, files) in AGENT_FILES.items():
        for f in files:
            p = root() / f
            out[f"{name}/{f}"] = sha12(p) if p.exists() else None
    from .adapters.creator_lib import frozen_files
    try:
        for k, p in frozen_files().items():
            out[k] = sha12(p)
    except SystemExit:
        pass
    shots = sorted((config.TASKS / "creator").glob("*/cases/*/**/*")) + sorted((config.TASKS / "creator" / "sub").glob("*/*"))
    h = hashlib.sha256()
    for p in shots:
        if p.is_file():
            h.update(p.relative_to(config.ROOT).as_posix().encode() + b" " + sha12(p).encode() + b"\n")
    out["tasks/creator/* (pages, scripts, captions)"] = h.hexdigest()[:12]
    return out


def freeze() -> None:
    (config.EVAL / "frozen.json").write_text(json.dumps(current(), indent=1) + "\n", encoding="utf-8", newline="\n")
    print("wrote", config.EVAL / "frozen.json")


CHANGES = "frozen-changes.json"  # files changed after the freeze: their new hash and the change-log entry that explains it


def record_changes(note: str) -> None:
    """Record the current hash of every frozen file that differs from eval/frozen.json, with a pointer to the
    PREREGISTRATION change-log entry. eval/frozen.json itself is never rewritten after the freeze."""
    want, now = json.loads((config.EVAL / "frozen.json").read_text(encoding="utf-8")), current()
    p = config.EVAL / CHANGES
    log = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    for k in want:
        if now.get(k) is not None and now[k] != want[k] and log.get(k, {}).get("sha") != now[k]:
            log[k] = {"sha": now[k], "was": want[k], "change_log": note}
    p.write_text(json.dumps(log, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print("wrote", p)


def check() -> bool:
    p = config.EVAL / "frozen.json"
    if not p.exists():
        print("no eval/frozen.json")
        return False
    want, now = json.loads(p.read_text(encoding="utf-8")), current()
    logged = json.loads((config.EVAL / CHANGES).read_text(encoding="utf-8")) if (config.EVAL / CHANGES).exists() else {}
    changed = [k for k in want if now.get(k) is not None and now.get(k) != want[k]]
    explained = [k for k in changed if logged.get(k, {}).get("sha") == now[k]]
    unexplained = [k for k in changed if k not in explained]
    missing = [k for k in want if now.get(k) is None]
    print(f"frozen files: {len(want) - len(changed) - len(missing)} unchanged, {len(explained)} changed after the freeze "
          f"with a change-log entry, {len(unexplained)} changed without one, {len(missing)} not present here")
    for k in explained:
        print(f"  changed (logged: {logged[k]['change_log']}) {k}")
    for k in unexplained:
        print("  CHANGED WITHOUT A CHANGE-LOG ENTRY", k)
    return not unexplained
