"""Paths, model and prices.

The bench does not contain the agents it evaluates. It imports them from their own repositories,
which are expected side by side with this one (override each path with an environment variable):

  ../grounded-review              ad review agent          APE_AD_REVIEW_DIR
  ../grounded-business-review     business review agent    APE_BIZ_DIR
  ../creator-skills               three creator skills     APE_CREATOR_DIR

Scoring stored traces (`python -m bench score`) needs none of them.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"
EVAL = ROOT / "eval"
RESULTS = Path(os.environ.get("APE_RESULTS_DIR") or (EVAL / "results"))  # the offline tests point this at a temporary folder


def _local_paths() -> dict:
    """Optional eval/paths.local.json (not published): the same settings as the environment variables, for a
    machine where the agents live under other folder names. Relative paths are relative to this repository."""
    import json
    p = EVAL / "paths.local.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


_LOCAL = _local_paths()


def _setting(env: str, default: Path) -> Path:
    value = os.environ.get(env) or _LOCAL.get(env)
    return (ROOT / value).resolve() if value else default


AD_REVIEW_DIR = _setting("APE_AD_REVIEW_DIR", ROOT.parent / "grounded-review")
BIZ_DIR = _setting("APE_BIZ_DIR", ROOT.parent / "grounded-business-review")
CREATOR_DIR = _setting("APE_CREATOR_DIR", ROOT.parent / "creator-skills")
# Generated databases of the business-review cases are large and reproducible: kept outside the repository.
BIZ_DATA_DIR = _setting("APE_BIZ_DATA_DIR", ROOT / "data" / "biz")

MODEL = "claude-haiku-5-5"
EFFORT = "medium"
# List prices in USD per token, prompts up to 100K tokens. Cache writes and reads are multiples of the input price.
PRICES = {
    "claude-haiku-5-5": {"input": 0.10 / 1e6, "output": 0.50 / 1e6},
    "claude-sonnet-5-5": {"input": 2.00 / 1e6, "output": 10.00 / 1e6},
}
CACHE_WRITE, CACHE_READ = 1.25, 0.10
BATCH_DISCOUNT = 0.5

FAMILIES = ("ad_review", "biz", "creator")
TRIALS = 3  # trials per task and configuration
