# Pre-registration

Written on 2026-11-18, before any model call of this evaluation. The tasks, answer keys, rubric code and
run configurations are frozen: their hashes are in `eval/frozen.json` (`python -m bench check-frozen`
compares). A change to any of them after this point is listed in the change log at the end, and results
on changed files are not reported as pre-registered results.

## 1. What is evaluated

Three agents that already exist, each as its project ships it, on new tasks written for this bench:

| Family | Agent | Tasks | Turns per task |
|---|---|---|---|
| Ad review | grounded-review, agent v1.3: reads the image, searches the policy library, checks its quotes with evidence-check, writes advice and a corrected version | 20 (10 Chinese, 10 English; L1 6, L2 8, L3 6) | 1, or 3 for L3 (review, advice, review of the advertiser's next version) |
| Business review | grounded-business-review, prompt v4: overview, breakdown, funnel and SQL tools over a synthetic local-services database; every number is checked against query rows | 18 (L1 6, L2 6, L3 6) | 1 |
| Creator workflow | creator-skills: trend-topic-check, shotlist-check and subtitle-check loaded together, with their checking tools | 8 (4 Chinese, 4 English; L2 4, L3 4) | 3 (topic cards, shot list, captions) |

Model `claude-haiku-5-5`, effort medium, for every agent and the judge.

## 2. Runs

| Run | What changes | Trials |
|---|---|---|
| `base` | nothing | 3 per task: trial 1 through the standard API (latency is measured), trials 2 and 3 through the Message Batches API |
| `no-skills` | creator workflow only: each skill's one-line plain instruction, no skills and no tools | 3 per task, Message Batches |
| fix configurations | one change aimed at the most frequent failure pattern of `base` that a configuration can address | 3 per task of the family, Message Batches |

A fix configuration is chosen after `base` is scored. Before it runs, its change, its target pattern and
its predictions are added to section 6 of this file, with a timestamp. Its run on every task of its
family is the regression test.

## 3. Measures

Defined in `rubric/RUBRIC.md` and computed by `bench/metrics.py`:

- **Task success**: every result check (R) passed. Reported as pass^1 with a Wilson 95% interval, and
  pass^3 (tau-bench: the chance that three trials of the same task all succeed, averaged over tasks).
- **Needs human confirmation**: share of trials an automatic gate held; failures caught, failures let
  through, successes held.
- **Tool-call correctness**: correct calls / all calls (offered tool, schema-valid input, no error).
- **Cost** per trial as paid, and at the standard price; **latency** per trial, median and 90th
  percentile, on the standard-API trials.
- **Stage pass rates** and the **failure attribution table** (first failing stage, cause, suggested fix).
- **Judged items** J-AD, J-BZ, J-CR: two passes each, agreement, calibration accuracy.

Comparison `no-skills` against `base` on the creator tasks: per-task success rates, exact two-sided sign
test over tasks.

## 4. Predictions (written before any run)

Base run:

1. Ad review: task success 75–95%. L1 ≥ L2 ≥ L3. The likeliest misses: the gin ad with no strength and
   no responsible-drinking line (en-L2-03, a violation by absence), and L3 tasks, where three turns must
   all be right.
2. Business review: weekly tasks 75–95% (its own test set gave 34/36 with the same prompt). The two scoped
   requests (B17, B18) fail in at least half of their trials, because the prompt only knows the weekly
   report and will list both planted events. These failures are attributed to understanding (U-BZ-2).
3. Creator workflow with the skills: task success 50–85%. The three turns multiply: a trial needs every
   one right.
4. Tool-call correctness ≥ 95% in every family.
5. Needs human confirmation: ad review 5–25% of trials, business review 0–15%, creator 15–50%. In every
   family, most failed trials are caught by a gate, but some are not (a wrong decision with verbatim
   quotes passes every gate).
6. Planning checks fail in some successful trials (steps skipped without harm), so planning pass rates
   are lower than task success suggests.
7. Cost per trial ≤ US$0.01 for ad review, ≤ US$0.02 for business review and the creator workflow, as
   paid. Whole evaluation (all runs and the judge) ≤ US$2.
8. Median latency per trial: ad review 8–30 s, business review 30–120 s, creator workflow 30–150 s.

`no-skills` against `base` (creator workflow):

9. `no-skills` task success ≤ 30%; `base` higher by at least 30 points; base better on at least 5 of the
   8 tasks, worse on none. Without the skills, the casualty tasks (C03, C07) fail at the topic cards
   (U-CR-2), the captions change correct cues or put back skipped lines (K-CR-4), and the shot lists
   overflow or miss the target in the tight tasks (C04, C08).

## 5. What would change the conclusions

If an answer key turns out to be wrong, the task is fixed or dropped, the change is listed below, and
both the old and new figures are reported. If a run fails for reasons outside the agent (API errors
after retries), the affected trials are re-run and reported as such.

## 6. Change log and later registrations

(Entries are added below with a date and time; nothing above is edited after the freeze.)

### 2026-11-18 18:37 +0800: judge calibration, one written verdict corrected

The judge was run on the calibration set before judging any run (24 judgements, US$0.0029). It agreed with
the written verdicts on 51 of 56 criterion judgements. Of the 5 disagreements, 3 are on `ad-good-en`: its
corrected caption adds "aged 8 years in oak casks", a new claim with a number, which J-AD-3 says must not
happen, so the written verdict (pass) was wrong and the judge was right; the verdict is corrected to fail
for J-AD-3. J-AD-2's split on the same example (one pass failed it for the same added claim) is left as
written. The other 2 are both passes failing J-CR-2 on `cr-bad-fit`, whose second hook is about investment
returns in a budget-coffee account; the written verdict (pass) is kept. With the correction the judge agrees
on 53 of 56. Both figures are reported. The judge's verdicts were not re-run.

### 2026-11-18 18:45 +0800: U-CR-3 corrected to match the rubric

RUBRIC.md defines U-CR-3 as "the unrelated product video does not mention the event". The frozen code
also searched the shot list's `notes` field (a note to the creator, not part of the video). In base trial 1
of C03 that note said the shot list was unrelated to the flood, naming it, and the check failed. The check
now reads only what the video shows, says and puts on screen, recomputed from the stored shot list at
scoring time, so every trace is scored the same way. U-CR-3 is not a result check, so task success does
not change.

### 2026-11-18 18:55 +0800: business review trials read a copy of their database

In the batch run of base trials 2 and 3, trials of the same business case ran at the same time in one
process and the second could not open the case database (DuckDB keeps one instance per file in a process,
and the agent's read-only connection locks its settings). Those trials stopped before any model call
(no cost) and are stored as errors. The adapter now copies the case database for each trial; the agent,
its prompt and the data are unchanged. The failed trials are run again; the stored trace of each
(task, trial) is the last one written.

### 2026-11-18 20:48 +0800: fix configurations `fix-ad` and `fix-biz`, registered before they run

The base run (138 trials) is scored. Failed trials by first failing check and cause (eval/results/attribution.md):
ad review 7 (one phrase under two categories, cause prompt: 4; an ordinary specification rejected: 2; the right
clause with the category OTHER: 1); business review 7 (a store-closure cause located at category or merchant type
instead of the merchant: 3; an extra cause: 3, of which one on a slice the monitor did not flag; a cause one level
finer than planted: 1); creator 3 (the scorer reads the route name "N7" as a number: a task problem). The most
frequent process fault: in 36 of 60 ad review trials the agent also called submit_advice in a review turn.

Each fix is one addition to the agent's instructions (eval/configs.yaml), written in general terms, without the
tasks' wording. Both run on every task of their family, 3 trials, Message Batches: that is the regression test.
These tasks are the ones the failures were seen on, so a gain here shows the fix works on them, not that it
generalises.

`fix-ad` predictions:
1. Failed trials with a phrase listed under two categories: 4/60 in base, at most 1/60.
2. Trials with submit_advice called in a review turn: 36/60 in base, at most 6/60.
3. Task success at least 53/60 (base); no task that succeeded in 3 of 3 base trials fails in 2 or more.
4. Output tokens per trial at least 10% lower than base (1,756).
5. Not targeted, expected to stay: the specification rejected in zh-L3-03.
Adopt if 1, 2 and 3 hold.

`fix-biz` predictions:
1. Store-closure causes located at the merchant: 3/6 base trials of B03 and B12, at least 5/6.
2. Causes on slices the monitor did not flag: 1 in base, 0.
3. Task success at least 47/54 (base); no task that succeeded in 3 of 3 base trials fails in 2 or more.
4. Not targeted, expected to stay: the extra cause in B02 on a slice the monitor did flag.
Adopt if 1 and 3 hold.

### 2026-11-18 21:05 +0800: T-1 no longer counts a refused call of the other request's finishing tool

Scoring base showed that in 36 of 60 ad review trials the agent also called submit_advice in a review turn. The
agent's harness refuses the call ("use submit_review for this turn") and the turn finishes normally. As frozen,
T-1 counted it as an unrecovered tool failure, so it became the first failing check of every failed trial it
appeared in, ahead of the result check that actually failed (the attribution table blamed tool calls for result
errors). Such calls now count as wrong calls in T-2 (the tool-call correctness rate, unchanged) and in a new report
check T-4, and not in T-1. Task success does not change; T-1's pass rate and the attribution of 5 failed base
trials do. The fix-ad registration (20:48) already names this fault and its prediction is measured on T-4.

### 2026-11-18 22:07 +0800: changes that do not touch the agents, the tasks or the scoring of a trial

- `bench/judge.py` read the creator account from a field the case file does not have; the first judge run on base
  stopped before any request (no cost) and was run again after the fix.
- `bench/report.py` excludes the three-trial smoke test from the tables and writes `facts.md`, the figures the report
  cites that are not in the metric tables; `tools/check_report.py` checks every number of the report and READMEs
  against the result files with evidence-check's script.
- `bench/config.py` reads the agents' folders from environment variables or an unpublished `eval/paths.local.json`.
- Frozen files changed after the freeze are listed with their new hash and their entry here in `eval/frozen-changes.json`;
  `python -m bench check-frozen` passes only when every changed file has such an entry.
