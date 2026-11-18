"""Draft the business-review tasks: a synthetic world, a week, and the events planted in that week.

The case generator is the business-review project's own (bizagent.scenarios): it draws a segment and a
size for each event family, builds the database and keeps a draft only if the weekly monitor raises an
alert on a metric the event moves. The answer (segment, lever, direction) is fixed by construction.
This script only chooses the families, the seeds (new ones: 7000.., draw seed 41) and the tiers, and
adds the request each task puts to the agent.

  APE_BIZ_DATA_DIR=<folder outside the repo> python tasks/biz/make_cases.py

Tiers
  L1  one event in a one-dimension segment
  L2  one event in a two-dimension segment, or two events in one week
  L3  weeks that need a judgement call: nothing planted, a public holiday, the weeks after one,
      and scoped requests (two events planted, the request asks about one of them only)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from bench import config  # noqa: E402

os.environ.setdefault("BIZ_DATA_DIR", str(config.BIZ_DATA_DIR))
sys.path.insert(0, str(config.BIZ_DIR))
import numpy as np  # noqa: E402
from bizagent import config as BC  # noqa: E402
from bizagent import db, monitor, scenarios  # noqa: E402

PLAN = [  # (tier, family, request kind)
    ("L1", "traffic_channel", "weekly"), ("L1", "conversion_city", "weekly"), ("L1", "supply_merchant", "weekly"),
    ("L1", "refund_category", "weekly"), ("L1", "redemption_category", "weekly"), ("L1", "traffic_up_category", "weekly"),
    ("L2", "traffic_city_channel", "weekly"), ("L2", "conversion_city_category", "weekly"), ("L2", "refund_city_category", "weekly"),
    ("L2", "traffic_channel+refund_category", "weekly"), ("L2", "conversion_city+redemption_category", "weekly"),
    ("L2", "supply_merchant+traffic_channel", "weekly"),
    ("L3", "none", "weekly"), ("L3", "none", "weekly"), ("L3", "holiday", "weekly"), ("L3", "after_holiday", "weekly"),
    ("L3", "traffic_channel+refund_category", "scoped"), ("L3", "conversion_city+redemption_category", "scoped"),
]
FIRST_SEED, DRAW_SEED = 7000, 41

SCOPE_TEXT = {  # the lever a scoped request asks about -> how the request names it
    "traffic": "流量（访问次数）", "conversion": "转化（访问支付转化率）", "supply": "供给（门店在线情况）",
    "redemption": "核销", "refund": "退款",
}


def request_of(case: dict, kind: str) -> tuple[str, dict]:
    """The user message of a task and, for a scoped request, the part of the answer it asks for."""
    week = case["week"]
    if kind == "weekly":
        return f"请写 {week} 这一周的经营分析周报。", case["truth"]
    asked = case["truth"]["causes"][-1]  # the second planted event
    text = (f"{week} 这一周，我只想了解{SCOPE_TEXT[asked['lever']]}方面有没有异动、原因是什么。"
            "其他方面的问题这次不用分析，原因清单里也不要列。")
    return text, {"causes": [asked]}


def main():
    rng = np.random.default_rng(DRAW_SEED)
    cases, seed = [], FIRST_SEED
    for i, (tier, family, kind) in enumerate(PLAN, 1):
        for attempt in range(1, 13):
            case = scenarios.make(f"B{i:02d}", family, seed, rng)
            seed += 1
            p = scenarios.build(case, force=True)
            con = db.connect(p)
            al = monitor.alerts(con, case["week"])
            con.close()
            planted = len(case["events"]) == len(family.split("+")) or family in ("none", "holiday", "after_holiday")
            if planted and (not case["events"] or scenarios.visible(case, al)):
                break
        else:
            raise RuntimeError(f"no visible case for {family} after 12 drafts")
        text, asked = request_of(case, kind)
        case.update(tier=tier, request_kind=kind, request=text, asked=asked, drafts=attempt, alerts=int(len(al)))
        cases.append(case)
        print(case["id"], tier, family, kind, "seed", case["seed"], case["week"], "drafts", attempt, "alerts", len(al),
              [(e["lever"], e["segment"], e["size"]) for e in case["events"]], flush=True)
    scenarios.save(cases, HERE / "cases.yaml")
    print(f"{len(cases)} cases -> {HERE / 'cases.yaml'}; databases in {BC.DATA / 'cases'}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
