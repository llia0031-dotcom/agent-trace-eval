"""Command line.

  python -m bench all                         offline: check the frozen files, score every stored run, write the report tables
  python -m bench run --config base [--trials 1-3] [--no-batch] [--families ad_review] [--tasks zh-L1-01] [--budget 2]
                                              run trials (costs money; EVAL_ENV_FILE points at the key file)
  python -m bench score [--run base]          score stored traces with the rubric checks
  python -m bench judge --run base            the model-judged rubric items (costs money)
  python -m bench inspect-log --run base      write the traces as an Inspect AI log, for `inspect view`
  python -m bench freeze | check-frozen       record / compare the hashes of the frozen files
"""
from __future__ import annotations

import argparse
import sys


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="bench")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--config", required=True)
    p.add_argument("--trials", default="1-3")
    p.add_argument("--families", default="")
    p.add_argument("--tasks", default="")
    p.add_argument("--no-batch", action="store_true")
    p.add_argument("--budget", type=float, default=2.0)
    p.add_argument("--out", default="")
    p = sub.add_parser("score")
    p.add_argument("--run", default="")
    p = sub.add_parser("judge")
    p.add_argument("--run", required=True)
    p.add_argument("--no-batch", action="store_true")
    p.add_argument("--calibration", action="store_true")
    p = sub.add_parser("inspect-log")
    p.add_argument("--run", required=True)
    sub.add_parser("freeze")
    sub.add_parser("check-frozen")
    sub.add_parser("all")
    a = ap.parse_args(argv)

    if a.cmd == "run":
        from .run import run
        run(a.config, a.trials, a.families, a.tasks, batch=not a.no_batch, budget=a.budget, out_name=a.out)
    elif a.cmd == "score":
        from .report import score_runs
        score_runs([a.run] if a.run else None)
    elif a.cmd == "judge":
        from .judge import judge_run
        judge_run(a.run, batch=not a.no_batch, calibration=a.calibration)
    elif a.cmd == "inspect-log":
        from .inspect_log import write_log
        print(write_log(a.run))
    elif a.cmd == "freeze":
        from .freeze import freeze
        freeze()
    elif a.cmd == "check-frozen":
        from .freeze import check
        sys.exit(0 if check() else 1)
    elif a.cmd == "all":
        from .freeze import check
        from .report import build_all
        ok = check()
        build_all()
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
