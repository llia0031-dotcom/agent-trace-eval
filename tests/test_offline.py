"""Offline tests: no model is called, no key is read, nothing costs money.

  python -m unittest discover -s tests -v

1. The tasks are valid (labels are verbatim, clause ids exist, creator cases pass their own builders' checks).
2. The rubric code: schema check, pass^k, sign test, Wilson interval.
3. End to end with a stand-in model that answers from the answer keys (tests/fake_model.py): the
   agents' own loops, tools and checks run, traces are written, and every such trial scores as a
   success. Then single faults are planted and must fail the stage they belong to.
4. The stored runs (eval/results/*/traces.jsonl), if present, score to the same summary that is stored.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

AGENTS_PRESENT = all(p.exists() for p in [])  # set below, after config is imported
from bench import checks, config, llm, metrics, trace  # noqa: E402

AGENTS_PRESENT = (config.AD_REVIEW_DIR / "agent").exists() and (config.BIZ_DIR / "bizagent").exists() and (config.CREATOR_DIR / "eval").exists()
needs_agents = unittest.skipUnless(AGENTS_PRESENT, "the three agent repositories are not next to this one")


class TestRubricCode(unittest.TestCase):
    def test_schema_problems(self):
        schema = {"type": "object", "required": ["q"], "additionalProperties": False,
                  "properties": {"q": {"type": "string"}, "n": {"type": "integer"}, "k": {"type": "string", "enum": ["a", "b"]}}}
        self.assertEqual(checks.schema_problems({"q": "x", "n": 2, "k": "a"}, schema), [])
        self.assertTrue(checks.schema_problems({"n": 2}, schema))
        self.assertTrue(checks.schema_problems({"q": "x", "z": 1}, schema))
        self.assertTrue(checks.schema_problems({"q": "x", "k": "c"}, schema))
        self.assertTrue(checks.schema_problems({"q": 3}, schema))

    def test_pass_hat_k(self):
        # tau-bench: C(c,k)/C(n,k), averaged over tasks
        per_task = {"a": [True, True, True], "b": [True, False, True], "c": [False, False, False]}
        self.assertAlmostEqual(metrics.pass_hat_k(per_task, 1), (1 + 2 / 3 + 0) / 3)
        self.assertAlmostEqual(metrics.pass_hat_k(per_task, 3), (1 + 0 + 0) / 3)
        self.assertAlmostEqual(metrics.pass_hat_k(per_task, 2), (1 + 1 / 3 + 0) / 3)

    def test_sign_test_and_wilson(self):
        self.assertEqual(metrics.sign_test([(0, 1)] * 6)["p"], 2 / 64)
        self.assertEqual(metrics.sign_test([(1, 1)])["p"], 1.0)
        lo, hi = metrics.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=3)
        self.assertAlmostEqual(hi, 0.7634, places=3)

    def test_first_failure_order(self):
        cs = [checks.check("R-x", "R", 1, False), checks.check("K-x", "K", 1, False), checks.check("P-x", "P", 2, False),
              checks.check("T-2", "T", 1, False, core=False)]
        self.assertEqual(checks.first_failure(cs)["id"], "K-x")


@needs_agents
class TestTasks(unittest.TestCase):
    def test_ad_review_items(self):
        sys.path.insert(0, str(ROOT / "tasks" / "ad_review"))
        import build
        items = build.load_items()
        self.assertEqual(build.validate(items), [])
        self.assertEqual(len(items), 20)

    def test_creator_cases(self):
        from bench.adapters import creator
        sys.path.insert(0, str(ROOT / "tasks" / "creator"))
        import importlib
        b = importlib.import_module("build")
        if not hasattr(b, "check_trend"):  # the ad review builder was imported under the same name
            sys.path.insert(0, str(ROOT / "tasks" / "creator"))
            spec = importlib.util.spec_from_file_location("creator_build", ROOT / "tasks" / "creator" / "build.py")
            b = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(b)
        from bench.adapters.creator_lib import lib
        L = lib()
        for t in creator.load_tasks():
            self.assertEqual(b.check_trend(t["trend"], L), [], t["id"])

    def test_biz_cases(self):
        from bench.adapters import biz
        tasks = biz.load_tasks()
        self.assertEqual(len(tasks), 18)
        for t in tasks:
            c = t["case"]
            self.assertIn(c["request_kind"], ("weekly", "scoped"))
            if c["request_kind"] == "scoped":
                self.assertEqual(len(c["asked"]["causes"]), 1)
                self.assertEqual(len(c["truth"]["causes"]), 2)


@needs_agents
class TestEndToEnd(unittest.TestCase):
    """Run the adapters with a stand-in model; write, score and report a run in a temporary folder."""

    @classmethod
    def setUpClass(cls):
        import fake_model
        cls.fake = fake_model
        cls.tmp = Path(tempfile.mkdtemp(prefix="ape-test-"))
        cls.real_client = llm.client
        config.RESULTS = cls.tmp

    @classmethod
    def tearDownClass(cls):
        llm.client = cls.real_client
        config.RESULTS = Path(os.environ.get("APE_RESULTS_DIR") or (config.EVAL / "results"))
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _trial(self, family, task, cfg, policy, workdir=None):
        from bench.adapters import ad_review, biz, creator
        ad = {"ad_review": ad_review, "biz": biz, "creator": creator}[family]
        meta = ad.configure(cfg)
        self.fake.install(policy)
        calls = llm.start_trial("test")
        res = ad.run_trial(task, cfg, workdir) if family == "biz" else ad.run_trial(task, cfg)
        tr = {"run": "test", "family": family, "task": task["id"], "tier": task["tier"], "lang": task["lang"], "trial": 1,
              "config": {"name": "test", "model": config.MODEL}, "mode": "standard", "turns": res["turns"], "gate": res["gate"],
              "error": res["error"], "usage": trace.usage_of(calls), "latency_s": 1.0, "extras": res["extras"]}
        offered = meta["tools"] if family == "ad_review" else {"tools": meta.get("tools") or (creator.tools() if family == "creator" else [])}
        cs = checks.FAMILY_CHECKS[family](tr, task, offered)
        return tr, cs

    def test_answer_key_scores_full_marks(self):
        from bench.adapters import ad_review, biz, creator
        for t in ad_review.load_tasks():
            _, cs = self._trial("ad_review", t, {}, self.fake.ad_policy(t))
            self.assertTrue(checks.outcome_pass(cs), (t["id"], [c for c in cs if c["passed"] is False]))
            self.assertIsNone(checks.first_failure([c for c in cs if c["stage"] != "P"]), t["id"])
        for t in creator.load_tasks():
            for setting in ("skills", "plain"):
                tr, cs = self._trial("creator", t, {"creator_setting": setting}, self.fake.creator_policy(t))
                self.assertTrue(checks.outcome_pass(cs), (t["id"], setting, [c for c in cs if c["passed"] is False]))
                self.assertTrue(tr["gate"]["auto_release"], (t["id"], setting, tr["gate"]))
        tasks = {t["id"]: t for t in biz.load_tasks()}
        for tid in ("B01", "B13", "B15", "B17"):
            _, cs = self._trial("biz", tasks[tid], {}, self.fake.biz_policy(tasks[tid]), self.tmp / "biz" / tid)
            self.assertTrue(checks.outcome_pass(cs), (tid, [c for c in cs if c["passed"] is False]))

    def test_planted_faults_fail_their_stage(self):
        from bench.adapters import ad_review, biz, creator
        ad = {t["id"]: t for t in ad_review.load_tasks()}["zh-L1-01"]
        good = self.fake.ad_policy(ad)

        def bad_quote(params):  # skips check_quotes and quotes words the ad does not contain
            m = good(params)
            b = m.content[0]
            if b.name == "check_quotes":
                return self.fake.message([self.fake.tool_use("submit_review", {"decision": "reject", "violations": [{
                    "category": "ABSOLUTE", "clause_id": "CN-AL-9-3", "ad_quote": "全国销量第一的空气炸锅", "visual_evidence": "",
                    "policy_quote": "（三）使用“国家级”、“最高级”、“最佳”等用语；", "explanation": "绝对化用语。"}], "notes": ""}, 99)], "tool_use")
            return m
        _, cs = self._trial("ad_review", ad, {}, bad_quote)
        self.assertEqual(checks.first_failure(cs)["id"], "P-AD-3")
        self.assertFalse(next(c for c in cs if c["id"] == "K-AD-1")["passed"])

        def unknown_clause(params):  # one failing tool call, never repeated correctly
            m = good(params)
            if m.content[0].name == "search_policy":
                return self.fake.message([self.fake.tool_use("get_clause", {"clause_id": "CN-AL-99"}, 98)], "tool_use")
            return m
        _, cs = self._trial("ad_review", ad, {}, unknown_clause)
        self.assertFalse(next(c for c in cs if c["id"] == "T-1")["passed"])
        self.assertTrue(checks.outcome_pass(cs))  # a process fault does not by itself fail the task

        c3 = {t["id"]: t for t in creator.load_tasks()}["C03"]
        good_c = self.fake.creator_policy(c3)

        def promo(params):  # makes a content card on a casualty event
            m = good_c(params)
            if "Pages collected" in self.fake._request_text(params) and m.stop_reason == "end_turn":
                f = c3["trend"]["reference_facts"][0]
                return self.fake.text_message({"declined": False, "decline_reason": "", "cards": [
                    {"kind": "content", "title": "露营装备", "hook": "h", "angle": "a", "fit": "f", "facts": [f], "risk_notes": ""}]})
            return m
        _, cs = self._trial("creator", c3, {"creator_setting": "skills"}, promo)
        self.assertEqual(checks.first_failure(cs)["id"], "U-CR-2")

        b17 = {t["id"]: t for t in biz.load_tasks()}["B17"]
        good_b = self.fake.biz_policy(b17)

        def both(params):  # names the other planted event too, which the scoped request did not ask about
            m = good_b(params)
            if m.stop_reason == "end_turn":
                d = json.loads(m.content[0].text)
                other = b17["case"]["truth"]["causes"][0]
                d["causes"].append({"segment": [{"dimension": k, "value": v} for k, v in other["segment"].items()],
                                    "lever": other["lever"], "direction": other["direction"]})
                return self.fake.text_message(d)
            return m
        _, cs = self._trial("biz", b17, {}, both, self.tmp / "biz" / "B17-bad")
        self.assertEqual(checks.first_failure(cs)["id"], "U-BZ-2")
        self.assertFalse(checks.outcome_pass(cs))

    def test_run_score_report(self):
        """bench.run with the stand-in model, then scoring and the report tables."""
        from bench import report, run as runmod
        from bench.adapters import ad_review
        tasks = {t["id"]: t for t in ad_review.load_tasks()}
        policies = {}

        def policy(params):  # route each request to its task's answer-key policy by the ad caption in the first message
            first = json.dumps(params["messages"][0]["content"], ensure_ascii=False)
            for tid, t in tasks.items():
                if t["item"]["copy"] in first:
                    policies.setdefault(tid, self.fake.ad_policy(t))
                    return policies[tid](params)
            raise AssertionError("unknown task")
        self.fake.install(policy)
        out = runmod.run("base", trials="1", families="ad_review", tasks="zh-L1-01,en-L2-03", batch=False, out_name="t-run", log=lambda *a: None)
        rows = report.score_runs(["t-run"])["t-run"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["success"] for r in rows))
        s = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(s["overall"]["success"], 2)
        report.build_all()
        self.assertTrue((config.RESULTS / "summary.md").exists())


class TestStoredRuns(unittest.TestCase):
    def test_rescoring_matches_stored_summary(self):
        res = config.EVAL / "results"
        stored = sorted(res.glob("*/summary.json"))
        if not stored or not AGENTS_PRESENT:
            self.skipTest("no stored runs, or the agent repositories (needed for the answer keys) are absent")
        from bench import report
        for p in stored:
            run = p.parent.name
            if not (p.parent / "traces.jsonl").exists():
                continue
            before = json.loads(p.read_text(encoding="utf-8"))["overall"]
            old = config.RESULTS
            config.RESULTS = res
            try:
                rows = report.score_runs([run])[run]
            finally:
                config.RESULTS = old
            after = metrics.summarise(rows)
            for k in ("trials", "success", "needs_human", "silent_failures", "tool_calls", "tool_calls_correct"):
                self.assertEqual(before[k], after[k], f"{run}: {k}")


if __name__ == "__main__":
    unittest.main()
