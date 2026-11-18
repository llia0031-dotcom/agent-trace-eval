"""Loads the creator-skills evaluation code (builders, prompts, tool handlers, scorers) by file path.

Nothing of that project is copied: its modules are imported from where they are, with one change made
in memory only: the folder they read and write cases in (their module-level HERE) is pointed at this
project's tasks/creator/<skill>/ folder. Their frozen SKILL.md files and checking scripts stay theirs.
"""
from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path

from .. import config

SKILLS = {"trend": "trend-topic-check", "shot": "shotlist-check", "sub": "subtitle-check"}
CASE_ROOT = config.TASKS / "creator"
# Modules that read or write case files relative to their own folder when called (not at import).
REDIRECT = {("trend", "build_cases"), ("trend", "run"), ("trend", "score"), ("shot", "build_cases")}


def _load(skill: str, name: str):
    folder = config.CREATOR_DIR / "eval" / SKILLS[skill]
    spec = importlib.util.spec_from_file_location(f"cs_{skill}_{name}", folder / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@lru_cache(maxsize=1)
def lib() -> dict:
    """{'trend': {'build', 'run', 'score'}, 'shot': {...}, 'sub': {...}, 'runner': eval.harness.runner}"""
    if not config.CREATOR_DIR.exists():
        raise SystemExit(f"creator-skills not found at {config.CREATOR_DIR}; set APE_CREATOR_DIR")
    root = str(config.CREATOR_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)
    shot_dir = str(config.CREATOR_DIR / "eval" / SKILLS["shot"])
    if shot_dir not in sys.path:
        sys.path.insert(0, shot_dir)  # shotlist-check's builder does `import score`
    out = {}
    for skill in SKILLS:
        mods = {}
        for name in ("score", "run", "build_cases"):
            m = _load(skill, name)
            m.FROZEN_DIR = m.HERE / "frozen"   # the frozen SKILL.md and checking script of this skill
            if (skill, name) in REDIRECT:
                m.HERE = CASE_ROOT / skill
            mods["build" if name == "build_cases" else name] = m
        out[skill] = mods
    from eval.harness import runner
    out["runner"] = runner
    return out


def frozen_files() -> dict[str, Path]:
    """The frozen skill files this benchmark depends on (hashed in the pre-registration)."""
    out = {}
    for skill, folder in SKILLS.items():
        base = config.CREATOR_DIR / "eval" / folder / "frozen"
        for f in sorted(base.iterdir()):
            if f.is_file():
                out[f"creator-skills/eval/{folder}/frozen/{f.name}"] = f
    return out
