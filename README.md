# Agent Trace Eval

[中文](README.zh-CN.md)

Evaluating what a tool-using agent **does**, step by step, not only what it answers. A frozen task set
with answers fixed when the tasks were written, a rubric of rule checks over every recorded step,
grouped into five stages (understanding, planning, tool calls, knowledge and citation, result quality),
the gates that decide what a person must confirm before release, and a failure attribution table that
says where each failure started, why, and what to fix.

It is applied to three existing agents, unchanged. Tasks: 46 in all; ad review 20, business review 18, creator workflow 8.

| Agent | What it does | Tasks |
|---|---|---|
| [grounded-review](https://github.com/llia0031-dotcom/grounded-review) | reviews ads against a policy library, checks its own quotes, advises the advertiser, reviews the next version | 20 (Chinese and English, 6 of them three turns long) |
| [grounded-business-review](https://github.com/llia0031-dotcom/grounded-business-review) | writes the weekly business review of a synthetic local-services platform from SQL tools; every number is checked against query rows | 18 (one or two planted causes, quiet weeks, a holiday, and requests about one thing only) |
| [creator-skills](https://github.com/llia0031-dotcom/creator-skills) | three skills in one conversation: topic cards from a trend, a shot list from the approved script, the auto captions checked against it | 8 (Chinese and English, three requests each) |

## Results

Base run, three trials per task ([full report](eval/EVALUATION.md)):

| Measure | Ad review | Business review | Creator workflow |
|---|---|---|---|
| Task success (pass^1) | 53/60 (88%), 95% CI 78%–94% | 47/54 (87%), 95% CI 76%–94% | 21/24 (88%), 95% CI 69%–96% |
| pass^3 (all three trials succeed) | 80% | 72% | 88% |
| Needs human confirmation (an automatic gate held it) | 0/60 (0%) | 9/54 (17%) | 0/24 (0%) |
| Failures the gates let through | 7 | 6 | 3 |
| Tool calls correct | 351/387 (91%) | 546/557 (98%) | 106/106 (100%) |
| Cost per trial (as paid) | $0.0026 | $0.0067 | $0.0136 |
| Latency per trial, median (standard API trials) | 13.0 s (n = 20) | 47.4 s (n = 18) | 63.0 s (n = 8) |

- **The gates guard form, not judgement.** Base failed trials 17: caught by a gate 1, let through 16. Every
  failed ad review cites real words and real clauses; it is the decision that is wrong.
- **Where failures start.** Most begin at result quality (a phrase listed under two categories, a store closure
  located too coarsely, an ordinary specification rejected); of the 17 failed base trials, cause task (problems
  of the tasks or the answer key) 5, kept in the scores and explained.
- **Skills against no skills** (creator workflow): success 88% against 33%, a difference of 54 points. Without
  the skills the agent made promotional cards on a fatal fire, and the source check let them through.
- **Two fixes, registered with predictions before they ran, then a regression run.** fix-ad: trials with
  submit_advice called in a review turn 0/60 (36/60 before), output tokens per trial 1,756 against 1,492 (-15%).
  fix-biz: store closures located at the merchant 6/6 (3/6 before), but one task got worse, which the
  regression run caught.
- Total spend, all runs, the judge and the smoke test: US$1.48.

## How it works

```
tasks (answer keys fixed when written, frozen by hash)
   │
   ▼
agent, unchanged ── every model call, tool call, input, output, error ──► trace (traces.jsonl)
   │                                                                          │
   ▼                                                                          ▼
the agent project's own scorer ──► outcome per turn          rubric checks per step and turn (bench/checks.py)
                                                                   U  P  T  K  R   + gates + judged items
                                                                          │
                                                                          ▼
                                                 metrics, stage tables, failure attribution, comparisons
```

- **Tasks.** Written for this bench, with the agent projects' own validators and builders: ad review
  items are checked so every labelled phrase is verbatim in the ad and every clause exists; the business
  cases come from that project's generator, which keeps a planted event only if the weekly monitor sees it;
  the creator materials pass that project's own case checks (reference facts pass the frozen checking
  script, trap facts are flagged as written). Tiers go from one-step requests to multi-turn ones and judgement calls.
- **Traces.** Each agent runs as its project ships it; this repository only routes its model calls
  through one recorder and converts the conversation it keeps into steps (`bench/trace.py`).
- **Rubric.** Rule checks over the steps, grouped in stages ([rubric/RUBRIC.md](rubric/RUBRIC.md)); rubric checks seen in the runs: 40 in all, 36 core and 4 report only.
  Quotes and numbers are checked with evidence-check's script, tool trajectories with
  [agentevals](https://github.com/langchain-ai/agentevals), results with each agent project's scorer.
  **Task success uses the result checks only**; the process checks explain how a trial got there.
- **Gates.** The checks a deployment would run before release (quote verification, the number check,
  the skills' checking scripts). A trial that a gate holds needs human confirmation. Failures the gates
  let through are the residual risk.
- **Judged items.** Three things rules cannot check (is the advice to the advertiser complete and honest,
  does business advice follow from a cause, does a topic card fit the account) are judged by a model
  against a written guide, twice, with agreement and accuracy on a calibration set reported.
- **Attribution.** A failed trial's stage is its earliest failing check; its cause is one of knowledge,
  tool, prompt, model, interaction or task, from a rule per check or from a note written after reading
  the transcript ([eval/attribution_notes.yaml](eval/attribution_notes.yaml)).
- **Pre-registered.** Tasks, answer keys, rubric code and configurations were hashed before the first
  model call, with predictions ([eval/PREREGISTRATION.md](eval/PREREGISTRATION.md)).
- **Viewer.** Traces can be written as an [Inspect AI](https://inspect.aisi.org.uk/) log and browsed in
  its viewer (`python -m bench inspect-log --run base`, then `inspect view`).

## Run it

```bash
pip install -r requirements.txt
python -m unittest discover -s tests     # offline: a stand-in model answers from the keys and must score full marks
python -m bench all                      # offline: check the frozen hashes, re-score the stored traces, write the tables
```

Running trials needs the three agent repositories next to this one (or `APE_AD_REVIEW_DIR`, `APE_BIZ_DIR`,
`APE_CREATOR_DIR`) and an API key in `eval/.env` (`ANTHROPIC_API_KEY=...`, or point `EVAL_ENV_FILE` at another file):

```bash
python -m bench run --config base --trials 1 --no-batch   # standard API: latency is measured
python -m bench run --config base --trials 2-3            # Message Batches API: half price
python -m bench judge --run base
```

## Where this sits

This repository is the evaluation layer of a set of projects that ask an AI to show its evidence
([evidence-first](https://github.com/llia0031-dotcom/evidence-first) has the overview and one evaluation card per project).

- The three agents it evaluates each have their own pre-registered evaluation of the final answer:
  [grounded-review](https://github.com/llia0031-dotcom/grounded-review),
  [grounded-business-review](https://github.com/llia0031-dotcom/grounded-business-review),
  [creator-skills](https://github.com/llia0031-dotcom/creator-skills). This bench adds what those do not
  measure: every step of a multi-turn run, the gates, and where each failure starts.
- Quotes and numbers are checked with [evidence-check](https://github.com/llia0031-dotcom/evidence-check)'s script.
- What happens to a trial a gate holds, a human review queue whose decisions return as regression items, is
  built in [grounded-labelling-pipeline](https://github.com/llia0031-dotcom/grounded-labelling-pipeline).

## Layout

```
tasks/        ad_review/ (items, images, OCR), biz/ (cases), creator/ (pages, scripts, captions, answers)
bench/        adapters/ (one per agent), trace, checks (the rubric), metrics, attribution, judge, report, run, inspect_log
rubric/       RUBRIC.md, judge_calibration.yaml
eval/         PREREGISTRATION.md, EVALUATION.md, configs.yaml, frozen.json, attribution_notes.yaml,
              results/<run>/ traces.jsonl, scores.jsonl, summary.json; results/summary.md, attribution.md, checks.md
tests/        offline tests and the stand-in model
```

TikTok policy text is not reproduced: in the stored traces it is replaced by the clause id and a link to
the policy page. All brands, places, people and data in the tasks are fictional.

MIT licence.
