# 预测分析层：ah.predict

`src/ah/predict/` 是框架的**原生预测分析层**（不依赖外部 MiroFish/laya）。它把历史运行结果累积成库，统计每接口出问题概率 + 置信度，用真实 LLM 基于现有用例集 + 失败历史**生成并自动验证新测试用例**，并给出回归测试推荐。**LLM 是 helper，不是 test driver**：生成候选必须通过 `run_single` 重跑验证才被接受。

## 三个能力模块

| 模块 | 职责 | 出口 |
|---|---|---|
| `history.py` | `HistoryStore` JSONL 追加式历史库 | 序列化 `ResultRecord` → dict 行 |
| `risk.py` | 接口风险概率 + 置信度 + 回归推荐 | `risk_profiles` / `rank_regression` |
| `generator.py` | LLM 提议新用例 + 自动重跑验证 | `Predictor.propose` |

## HistoryStore（历史累积）

```python
from ah.predict.history import HistoryStore, load_history
store = HistoryStore("artifacts/reports/history.jsonl")  # 默认 report_dir/history.jsonl
n = store.append_all([ResultRecord(name="a.GET", result="FAIL", failure_msg="...")], now=1234.5)
rows = store.load()          # list[dict]
rows = load_history(path)    # 便捷函数，空/缺文件返回 []
```

- 追加式（append-only），每行一条 `ResultRecord` + `ts`/`interface`/`failure_class`。
- `_normalize_failure_class`：优取 `rec.triage.category`，否则按错误文本启发式（assert/schema/status/extraction/timeout/network/unknown）。
- `_interface_key`：用 `request.path`，否则回退 `name`。**pytest 插件在 `pytest_sessionfinish` 自动把本轮结果追加进 `report_dir/history.jsonl`**——你无需手动写。
  > 项目自包含执行入口（``run_single`` 直接驱动）不经插件记录，需手动
  > ``store.append_all(...)`` 才会累积历史（见 ``references/reports.md`` 注意）。

## 风险统计（risk.py）

```python
from ah.predict.risk import RiskProfile, rank_regression, risk_profiles
profiles = risk_profiles(rows)                 # list[RiskProfile]、按 (risk, total) 降序
plan = rank_regression(rows, risk_threshold=0.3, top_k=5)  # RegressionPlan
plan.focused                                   # 高风险接口（>= threshold）
d = plan.to_dict()                             # {"focused": [...], "all_profiles": [...]}
```

每个 `RiskProfile`：
- `failure_rate` = 失败数/总数（保留 4 位小数）。
- `risk` = `failure_rate × 《失败类别权重》加权均值`，clamp [0,1]。
- `confidence` = `min(1, total/10)`，且 `total<3` 再乘 `total/3`（样本少 → 打折）。
- `top_failure_classes` = 本接口失败类别分布。
- 类别权重：assertion/schema/jsonpath=1.0，status=0.9，extraction=0.85，unknown=0.7，timeout/network=0.5。

## Predictor（LLM 生成 + 验证闭环）

```python
from ah.predict.generator import Predictor
predictor = Predictor(llm=llm, client=client, ctx=ctx)
accepted = predictor.propose(cases, history, limit=3, max_heal_attempts=2)
# accepted: list[GeneratedCase]（仅 result.succeeded 的被接受，g.validated=True）
```

- `llm=None` → 返回 `[]`（hermetic 零网络）。
- `_build_prompt`：现有接口清单 + 失败样本（`failure_msg` + `triage.root_cause` + `failure_class`），让强模型（`model="strong"`）提议 `{"cases":[{name,request,assert,extract}]}`。
- 每个候选走 `run_single(gen.to_case(), ctx, client, hooks=LLMHooks())` **重新执行验证**；候选必须是**绝对 URL path**（相对 `/x` 会被 httpx 拒 `unknown url type`）。
- `GeneratedCase.to_case()` / `to_yaml_mapping()` 供落盘。

### 从 dispatch 触发

```python
res = router.dispatch("预测接口风险", use_llm=False,
                      client=client, ctx=ctx,
                      cases=cases, history_rows=rows, limit=3)
if not res.skipped:
    accepted = res.output            # list[GeneratedCase]
    flags = res.metadata["validated"]
```

`PREDICT` intent 已注册（关键词：「预测/predict/推断/预判/风险分析/哪些会挂/出问题概率/回归建议」），分派到 `Predictor.propose`。依赖缺失（cases/client/ctx）→ `skipped=True`。**注：真实 LLM 生效需给 `IntentRouter` 注入 `llm`**；不注入时返回空列表。

## 报告中的预测分析段

`ah.reporting.writer.build_report(results, *, history_rows=None)` 与 `build_html_report`：传 `history_rows`，或留 `None` 自动读 `report_dir/history.jsonl`。有历史时：
- `report.md` 追加「## 预测分析 / ### 高风险接口」段（`_prediction_section(plan)`）。
- `report.json` 加 `prediction` key。
- HTML 加 `{% if prediction %}` 预测表。
- **无历史则报告零变化**，向后兼容。

## 关键约束

- Hermetic 默认：不注入 LLM 时 `Predictor` 返回空、`rank_regression`/`risk_profiles` 是确定性计算。
- 验证闭环 = 复用 `run_single` + 空 `LLMHooks()`，**不叠加自愈**——避免把「生成即失败」误判为通过。
- 历史库累积极小（JSONL 追加），报告自动附带预测段，无需手动维护元数据。

## AI 扩展助手（ah.predict.assist）

`src/ah/predict/assist.py` 在 `Predictor` 之上横向扩展 5 个 **helper agent**。它们全部服务预测
分析层、复用 `run_single` 验证闭环与 `GeneratedCase`；LLM 只做提议/评分，执行与验证永远由框架
确定性完成。**人审护栏**：所有外部提议默认需要人审，LLM 绝非无人监督的写入者。

```python
from ah.predict.assist import (
    AgentImpact, AgentNL2Case, AgentCluster, AgentCoverage, AgentQuality,
)
```

| agent | 输入 | 输出 | 决策模型 |
|---|---|---|---|
| `AgentImpact.analyze` | 用例 + 历史 + 变更描述（commit/diff/Swagger） | `[{interface, affected_assert_paths[], impact_score, rationale}]` | `judge("impact", strong)` |
| `AgentNL2Case.to_case` | 自然语言请求（+ 可选现有用例） | `Optional[NL2CaseResult]`（`GeneratedCase` + `needs_human_review=True` + `review_reason`） | `judge("nl2case", strong)` |
| `AgentCluster.cluster` | 历史失败记录 | `[{id, title, members[], root_cause, fix_suggestion}]` | `judge("cluster", strong)` |
| `AgentCoverage.propose` | 现有用例 + spec | `list[GeneratedCase]`（**仅 `validated=True`**） | `judge("coverage", strong)` |
| `AgentQuality.score` | 用例集 | `[{case, score, assertion_effectiveness, param_coverage, maintainability, issues[], suggestion}]` | `judge("quality", strong)` |

### 关键行为

- **Hermetic**：`llm is None` → `AgentImpact/AgentCluster/AgentQuality` 返回 `[]`、
  `AgentNL2Case` 返回 `None`、`AgentCoverage` 返回 `[]`——零网络。
- **`AgentCoverage` / `AgentNL2Case(validate=True)`** 需要 `client` 走 `run_single` 验真；
  候选必须**绝对 URL path**（相对 `/x` 被 httpx 拒）。`AgentCoverage` 无 client 或空用例 → `[]`。
- **`AgentNL2Case`** 产物默认 `needs_human_review=True`；缺 path → `None`；验真失败 →
  `generated.validation_error` 记录但对象仍返回（供人审）。
- **`AgentCluster`** `member_indices` 指向**失败子集**（已滤掉 PASS 的索引）；无成员 cluster 被丢弃，
  无效索引被跳过。
- **judge 异常不会成为测试失败**：`try/except` 吞掉记 `log.warning("... llm call failed")` 返回空。
- 评分 clamp：`impact_score` / `score` 一律 clamp 到 `[0,1]`，`AgentQuality.score` 还 round 到 3 位小数；
  非数字评分安全降级为 `0.0`。