# Agent Trace Eval

[English](README.md)

评一个会调工具的 Agent **每一步做了什么**，不只看最后的答案。题目和答案在出题时定好、跑前冻结；评分表是对每一步记录的规则检查，分成五个阶段（理解意图、规划、工具调用、知识引用、结果质量）；另有“上线前自动关卡”，决定哪些结果要人确认；最后是失败归因表：每个失败从哪一步开始、为什么、怎么改。

用在已有的 Agent 上（不改它们的代码），共 46 道新题（广告审核 20、经营分析 18、创作流程 8）：

| Agent | 做什么 | 题数 |
|---|---|---|
| [grounded-review](https://github.com/llia0031-dotcom/grounded-review) | 对照政策库审广告，自己核对引文，给广告主修改建议，再审广告主的新版本 | 20（中英各半，其中 6 道是三轮对话） |
| [grounded-business-review](https://github.com/llia0031-dotcom/grounded-business-review) | 用 SQL 工具给合成的本地生活平台写经营周报，每个数字都对回查询结果 | 18（一处或两处埋好的原因、平静的周、节假日，以及“只问一件事”的请求） |
| [creator-skills](https://github.com/llia0031-dotcom/creator-skills) | 同一段对话里三件事：热点选题、按定稿口播稿拆分镜、对自动字幕 | 8（中英各半，每题三个请求） |

## 结果

基线结果（[完整报告](eval/EVALUATION.md)，英文）：

| 指标 | 广告审核 | 经营分析 | 创作流程 |
|---|---|---|---|
| 任务成功率（pass^1） | 53/60 (88%)，95% 区间 78%–94% | 47/54 (87%)，95% 区间 76%–94% | 21/24 (88%)，95% 区间 69%–96% |
| pass^3（三次都成功） | 80% | 72% | 88% |
| 需要人工确认（被自动关卡拦下） | 0/60 (0%) | 9/54 (17%) | 0/24 (0%) |
| 关卡放过去的失败 | 7 | 6 | 3 |
| 工具调用正确 | 351/387 (91%) | 546/557 (98%) | 106/106 (100%) |
| 每次试验费用（实付） | $0.0026 | $0.0067 | $0.0136 |
| 每次试验时延中位数（普通接口） | 13.0 s (n = 20) | 47.4 s (n = 18) | 63.0 s (n = 8) |

- **关卡管得住格式，管不住判断。** 基线失败 17 次，关卡拦下 1 次、放过 16 次。广告审核的每次失败，引的广告原话和条款都是真的，错在结论。
- **失败从哪里开始。** 大多在结果这一步：同一句话贴了两个类别、门店下线定位得太粗、把普通的产品参数判成违规。17 次失败里有 5 次是题目或答案本身的问题，留在分数里并写明原因。
- **装和不装 Skill**（创作流程）：成功率 88% 对 33%，差 54 个百分点。不装 Skill 时，Agent 给一场致死火灾出了营销选题，来源核对也放过了。
- **两个修复：跑之前先写下预测，再做回归重测。** 广告审核：审核那一轮误调建议工具的试验从 36/60 降到 0/60，每次输出 token 从 1,756 降到 1,492（-15%）。经营分析：门店下线定位到具体商家 6/6（原来 3/6），但有一道题变差了，回归重测把它查了出来。
- 全部花费（所有运行、判官和试跑）：US$1.48。

## 怎么做的

- **题目**：为这套评测新写，校验和生成都用三个 Agent 项目自己的代码：广告题的违规原文必须逐字在广告里、条款必须存在；经营分析题用原项目的出题器，只保留监控规则看得见的异动；创作题的材料要通过原项目自己的自检（参考事实过冻结脚本，陷阱事实按预期被标出）。从一步就能答的题，到多轮对话、要拿捏分寸的题，分成几档。
- **过程记录**：每个 Agent 照原样运行；本仓库只把它的模型调用接到同一个记录器上，再把它自己保存的对话转成一步一步的记录（`bench/trace.py`）。
- **评分表**：五个阶段的规则检查，共 40 条，其中 36 条决定阶段成败、4 条只报告（[rubric/RUBRIC.md](rubric/RUBRIC.md)）。引文和数字用 evidence-check 的脚本核对，工具轨迹用 [agentevals](https://github.com/langchain-ai/agentevals) 比对，结果用各 Agent 项目自己的打分代码。**任务成功只看结果检查**，过程检查用来说明它是怎么走到这一步的。
- **自动关卡**：上线前会跑的检查（引文核对、数字核对、Skill 自带的检查脚本）。关卡拦下的结果要人确认；关卡放过去的错，就是剩下的风险。
- **按评分表判的三项**：规则查不了的三件事（给广告主的建议是否讲清、经营建议是否从原因出发、选题是否贴合账号），按写好的评分表判，每个答案判两遍，报两遍的一致率和在校准题上的准确率；调用的是和三个 Agent 相同的模型。
- **归因**：失败试验的阶段 = 最早没过的那条检查；原因分六类（知识缺失、工具调用、提示词、模型能力、交互设计、评测题本身），来自每条检查的规则，或读完过程后写的说明（[eval/attribution_notes.yaml](eval/attribution_notes.yaml)）。
- **预注册**：题目、答案、评分代码和配置在第一次调用模型前记下指纹，并写好预测（[eval/PREREGISTRATION.md](eval/PREREGISTRATION.md)）；之后的每处改动都记在它的变更记录里。
- **查看器**：每次试验的每一步和没过的检查，都能在 [Inspect AI](https://inspect.aisi.org.uk/) 的日志查看器里逐条看：[llia0031-dotcom.github.io/agent-trace-eval](https://llia0031-dotcom.github.io/agent-trace-eval/)（由 `tools/make_viewer.py` 从存下的记录生成；在本机可以 `python -m bench inspect-log --run base`，再 `inspect view`）。

## 怎么跑

```bash
pip install -r requirements.txt
python -m unittest discover -s tests     # 离线：假模型按答案作答，必须满分
python -m bench all                      # 离线：核对冻结指纹、给存好的过程重新打分、生成结果表
```

真跑试验要把三个 Agent 仓库放在本仓库旁边（或用 `APE_AD_REVIEW_DIR`、`APE_BIZ_DIR`、`APE_CREATOR_DIR` 指定），并在 `eval/.env` 里写 `ANTHROPIC_API_KEY=...`（或用 `EVAL_ENV_FILE` 指向别的文件）：

```bash
python -m bench run --config base --trials 1 --no-batch   # 普通接口：量时延
python -m bench run --config base --trials 2-3            # 批量接口：半价
python -m bench judge --run base
```

## 在整套项目里的位置

这个仓库是一组“让 AI 拿出证据”的项目里的评测层（总览和每个项目的评测卡在 [evidence-first](https://github.com/llia0031-dotcom/evidence-first)）。

- 它评的三个 Agent 各自都有针对最终答案的预注册评测：
  [grounded-review](https://github.com/llia0031-dotcom/grounded-review)、
  [grounded-business-review](https://github.com/llia0031-dotcom/grounded-business-review)、
  [creator-skills](https://github.com/llia0031-dotcom/creator-skills)。这里补的是它们没有量的部分：多轮运行的每一步、自动关卡，以及每个失败从哪一步开始。
- 引文和数字用 [evidence-check](https://github.com/llia0031-dotcom/evidence-check) 的脚本核对。
- 被关卡拦下的结果之后怎么处理（人工复核队列，复核结果回流成回归题），在 [grounded-labelling-pipeline](https://github.com/llia0031-dotcom/grounded-labelling-pipeline) 里做了。

## 目录

```
tasks/        ad_review/（题目、图片、OCR）、biz/（题目）、creator/（网页、口播稿、字幕、答案）
bench/        adapters/（每个 Agent 一个连接）、trace、checks（评分表）、metrics、attribution、judge、report、run、inspect_log
rubric/       RUBRIC.md、judge_calibration.yaml
eval/         PREREGISTRATION.md、EVALUATION.md、configs.yaml、frozen.json、attribution_notes.yaml、
              results/<运行名>/ traces.jsonl、scores.jsonl、summary.json；results/summary.md、attribution.md、checks.md；
              results/replay/replay.json（按固定规则挑几次试验做的逐步回放，由 tools/make_replay.py 生成）
tests/        离线测试和假模型
```

TikTok 政策原文不转载：存下的过程里换成了条款编号和政策页面链接。题目里的品牌、地名、人名和数据都是虚构的。

MIT 许可证。
