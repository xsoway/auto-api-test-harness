---
name: auto-api-test-harness
description: 在 Auto API Test Harness 中新建项目、维护 API 用例、编排场景、跑批回归和诊断失败。
---

# Auto-API-Test-Harness Agent Skill

本文档是 **auto-api-test-harness** 的 agent skill，面向任意 coding agent（Claude Code、Cursor Agent 等）。沉淀了这套分层 API 测试框架的全部领域知识：意图识别调度（`ah.core.dispatch`）、用例/场景 YAML schema、pytest 插件用法、报告产物解读、LLM 辅助接入与隔离护栏。

> **你（coding agent）的工作方式**：用你自己原生的工具完成所有操作——`Read` 读文件、`Write`/`Edit` 写 YAML、`Bash` 跑 pytest、直接向用户提问做人工确认。**禁止猜 YAML 字段或 API 签名，禁止破坏隔离模式。** 所有事实以 `references/` 为准，凭印象回答 = 错。

## 何时启动本流程

用户表达"给服务加用例 / 加一个场景流程 / 跑一遍测试 / 诊断某个用例为什么失败 / 生成请求体 / 自愈修复 / 分析运行报告 / 接入 LLM"等 API 测试意图时，立即进入本流程。先把用户的话交给 `ah.core.dispatch` 做意图识别（或用下表人工对照），再进入对应 workflow。

## 入口：先识别用户意图

意图由 `src/ah/core/dispatch.py` 的 :class:`IntentRouter` 按关键词规则 + 可选 LLM 兜底解析，规范意图标签与下方表格一一对应。**你可以直接调用 dispatch，也可用下表人工路由。**

| 用户说（示例） | 规范意图 | 进入哪个 workflow |
|---|---|---|
| 新建 API 测试项目 / 导入 OpenAPI | — | [AI 项目接入与跑批规范](../../docs/ai-api-testing.md#新建测试项目) |
| 给我加一个 get /users/{id} 的用例 / 新增单接口用例 | `run_case` 的前置工序 → **新增用例** | `workflows/add-case.md` |
| 加一个登录→下单的场景流程 / 串联多步 | `run_flow` 的前置工序 → **新增场景** | `workflows/add-flow.md` |
| 把登录/下单做成可复用 skill / 用 skill 拼一条下单流程 / 组装业务流程 | `run_flow` 的前置工序 → **skill 组装** | `workflows/add-flow.md`（`flow:` 路线）→ `docs/operation.md` 的「单接口与场景」 |
| 跑一下这批用例 / 跑全量 / 验证这个接口 | `run_case` / `run_flow` | `workflows/run-suite.md` |
| 这个用例为什么失败 500 / 诊断失败原因 / 定位根因 | `diagnose` | `workflows/diagnose.md` |
| 帮我生成一个请求体 / 造数 / 按 schema 造样例数据 | `generate_data` | `workflows/synthesize-data.md` |
| 分析一下运行报告 / 总结测试结果 / 洞察失败模式 | `analyze_report` | `workflows/analyze-report.md` |
| 预测哪个接口会挂 / 推断风险 / 给我回归建议 | `predict` | `references/predict.md`（`Predictor` 生成并验真新用例） |
| 接入 OpenAI/OpenRouter / 开真实 LLM 辅助 | — | `workflows/add-llm.md` |
| 接入一个新被测服务 / 换 base_url / 改 token 管理 | — | `workflows/add-service.md` |

不要凭印象回答 YAML 字段、agent 方法签名或环境变量——先用 `Read` 工具读对应 reference / workflow 文档。

## 主流程总览

`workflows/` 下的每个文档都是一条独立工序；`run-suite.md` 是主执行入口，其余为前置/后置。结构如下：

```
用例/场景定义（add-case / add-flow）──► 配置服务（add-service）
   │
   ▼
运行（run-suite：项目自包含 test 执行入口，`pytest projects/<业务>`）
   │
   ├─ 失败 ──► 诊断（diagnose）──► 自愈 / 手动改 YAML ──► 重跑
   │
   ▼
报告（analyze-report + references/reports.md 解读）
```

## 核心模块的 ground truth（一定要读对应文档再下笔）

- **意图调度**：`ah.core.dispatch`（`Intent` / `DispatchResult` / `IntentRouter` / `get_router` / `route_intent`），9 个规范意图（`generate_data` / `diagnose` / `heal` / `analyze_report` / `predict` / `assist` / `run_case` / `run_flow` / `unknown`）+ 关键词表。详见 `references/dispatch.md`。
- **编排**：`ah.core.orchestrator.run_single(case, ctx, client, hooks=None, tracer=None, gate=None, governor=None) -> StepResult`、`run_flow(steps, ctx, client, hooks=None, tracer=None) -> list[StepResult]`，以及 `StepResult` / `LLMHooks`（含 `observe` 钩子）。`governor` 注入失败治理链（见下方 failure）。详见 `references/run.md`。
- **HTTP 传输**：`ah.harness.http.HTTPClient(base_url, client=None, trace=None).request(method, path, ...)` + `APIResponse`。见 `references/cases.md` 与 `workflows/add-service.md`。
- **配置 / 令牌**：`ah.core.config`（`Settings` / `load_settings` / `settings`）+ `.env` 环境变量覆盖 + `ah.environment.token` 令牌生命周期。详见 `references/config.md`。
- **报告**：`ah.reporting.writer.build_report(results, trace_urls=None, *, report_dir=None, history_rows=None)` 写出 `report.md` + `report.json`；`AgentReport.analyze_results(results)` 生成洞察。有历史时自动附「预测分析」段。详见 `references/reports.md`。
- **预测分析**：`ah.predict`（`HistoryStore` / `risk_profiles` / `rank_regression` / `Predictor`）。pytest 插件自动把结果累积进 `report_dir/history.jsonl`，据此算接口风险 + 回归推荐，并用真实 LLM 生成并验真新用例。另含 5 个扩展助手 `ah.predict.assist`（影响分析 / NL→用例 / 失败聚类 / 覆盖缺口 / 质量评分）。详见 `references/predict.md`。
- **LLM 接口**：`ah.llm`（`BaseLLM`、`LLMClient`、OpenAI 驱动、`llm_enabled()`）+ 4 个 agent（`AgentDataGen`/`AgentTriage`/`AgentHeal`/`AgentReport`）。详见 `references/llm.md`。
- **离线链路生成**：`ah.scenario`：本地 OpenAPI/HAR → 关系证据 → 严格校验 → 现有场景 YAML。`ah scenario discover/validate/compile`；必须人工确认生产者、稳定列表 selector 和业务断言，禁止直接运行未确认候选。详见 `references/cli.md` 的 scenario 与 `docs/operation.md` 的「OpenAPI 和离线依赖链」。
- **CLI 子命令**：`ah`（`python -m ah.cli`）挂载 8 组子命令——`evolution`（`run`/`evolve`/`trend-gate`）、`scaffold`（`scaffold`/`add-case`）、`scheduler`（`scheduled-run`/`ci-trigger`/`flaky-quarantine`，含 `--parallel`）、`telemetry`、`property`、`debug`（`debug`/`openapi`）、`scenario`（`discover`/`validate`/`compile`）、`observe`（`serve`/`ingest`/`seed`/`llm-detail`，本地类 Langfuse 可观测平台）。详见 `references/cli.md`。
- **失败治理**：`ah.failure` 包（hermetic、零 LLM）——`triage`（6 分类 `script_bug`/
  `product_defect`/`environment`/`flaky`/`known_issue`/`unknown` → 动作 `heal`/
  `observe`/`block`，`normalize_category` 兼容历史）、`governor`（`FailureGovernor`
  决策链缩写 `decide`/`record_pass`/`observe_recover`）、`cluster`（`ClusterStore`
  指纹状态机 `active/flaky_watch/quarantined/recovered/resolved`，JSON 落盘）、
  `observe`（`AgentObserve` 无侵入恢复，**只重跑原步骤绝不回写脚本**
  `wrote_script=False`）、`knowledge`（`KnowledgeStore` 草稿→发布→命中改动作→
  反馈闭环）。接入 `run_single(..., governor=...)`；scheduler `flaky-quarantine`
  合并簇群 quarantined 状态（标签 ∪ 簇群）。详见 `references/run.md` +
  `references/cli.md`。

## CLI 命令（自动化入口）

用户在命令行跑套件 / 自主进化 / 脚手架 / 回归调度 / 遥测 / 属性探索时走 `ah <子命令>`。多数子命令在 `LLMOFF=1` 下可完整运行；`add-case` 与 LLM 生成类在 LLM 关闭时返回明确错误或空结果，绝不隐式触网。

```bash
ah run <project>             # 跑一套用例并出报告（默认 suite=smoke）
ah evolve <project>          # 自主涌现闭环：跑→分析→生成→持久化→下一轮
ah trend-gate <project>      # 趋势闸：近 K 次通过率下滑告警
ah scaffold <business>       # 生成业务项目骨架（data/ + test_*.py + conftest.py + README）
ah add-case <project> "自然语言" # NL→用例，追加 data/<name>.yaml
ah scheduled-run <project> --suite regression --stub  # 离线批跑接线
ah ci-trigger <project>      # git diff 受影响接口只跑相关用例
ah flaky-quarantine <project># 隔离仓 = 标签 ∪ 簇群 quarantined（cluster 状态机）
ah telemetry <project>       # 聚合 LLM 成本/成功率看板
ah observe serve <--db> <--web>    # 本地类 Langfuse 可观测平台（seed/ingest/serve/llm-detail）
ah property <project> <case> # Hypothesis 参数空间探索
ah debug <case>              # 单用例独立执行并 dump 请求/响应/断言
ah openapi <file.yaml>       # 把 OpenAPI 3.x paths 导入为最小用例
ah scenario discover --openapi spec.yaml --har session.har --output-dir project/drafts/chain
ah scenario validate project/drafts/chain/relationships.json
ah scenario compile project/drafts/chain/relationships.json --output project/data/chain.yaml
```

CI 使用 pytest 或 `scheduled-run`，同时核对本次报告和实际执行数。`scheduled-run` 命中零条也可能返回 0。`evolve` 通过轮次报告和审计日志判断结果。完整参数与产物见 [CLI 参考](references/cli.md)。

## 隔离护栏（不可绕过）

1. **先确认传输模式**：`LLMOFF=1` 关闭模型辅助。离线运行使用项目桩入口，或对 `scheduled-run`、`debug` 显式传入 `--stub`。实连前核对用户授权的目标与范围。
2. **从现有示例取 schema**：写 case/flow YAML 前先读 [用例格式](references/cases.md) 和 [demo 数据](../../projects/demo/data/)。调用 agent 或编排器前先读对应 reference。
3. **dispatch 永不抛错**：依赖缺失时 `DispatchResult(skipped=True, reason=...)`。校验 `skipped` 而非用异常流控。
4. **`AgentHeal` 不能在 `IntentRouter.agent()` 里惰性创建**——它需要 `client` + `ctx` 运行时依赖，只在 dispatch 的 HEAL 分支按需构造。
5. **绝不替用户填真实 API key**：`api_key` 只留空 / `${ENV_VAR}`（如 `OPENROUTER_API_KEY`），并提示用户自己在 `.env` 设置。加服务时同样，凭 `.env` 的环境变量注入，不硬编码密钥。

## 输出收尾

跑完 / 改完必须做的事：
1. 用 `AgentReport.analyze_results` 或读 `report.md`，给用户一段**洞察小结**：通过率、主要失败原因分布、逐用例注记、下一步建议。
2. 失败用例主动走 `workflows/diagnose.md` 定位根因；可修复的给 `workflows/` 里的自愈/改 YAML 方案。
3. 如开了真实 LLM，说明哪些结果来自 LLM 注记、哪些是隔离固定值，避免误导。
4. 任何 schema 改动后重跑 `LLMOFF=1 .venv/bin/python -m pytest -q -p no:cacheprovider` 验证全套件仍绿。

## 文件索引

```
skills/auto-api-test-harness/
├─ SKILL.md                          ← 你在读的文件（入口 + 意图表）
│  ├─ cases.md                       Case / YAML 字段 schema + 加载与发现
│  ├─ config.md                      配置 + .env 环境变量 + 令牌管理
│  ├─ run.md                         run_single / run_flow / StepResult / LLMHooks
│  ├─ reports.md                     报告产物解读 + build_report / AgentReport
│  ├─ predict.md                     预测分析层（HistoryStore / 风险 / Predictor / 5 个 assist 助手）
│  ├─ dispatch.md                    意图识别调度 API 手册
│  ├─ llm.md                         BaseLLM / LLMClient / agents / llm_enabled()
│  └─ cli.md                         `ah` 子命令参考（evolution / scaffold / scheduler / telemetry / property / debug / observe）
├─ workflows/
│  ├─ add-case.md                    新增单接口用例
│  ├─ add-flow.md                    新增场景流程（多步串联）
│  ├─ run-suite.md                   主流程：跑全套件 / 单接口
│  ├─ diagnose.md                    失败诊断（AgentTriage）
│  ├─ synthesize-data.md             请求体 / 造数（AgentDataGen）
│  ├─ analyze-report.md              报告洞察（AgentReport）
│  ├─ add-llm.md                     接入真实 LLM 辅助
│  └─ add-service.md                 接入新被测服务
```

## 快速开始（最小路径）

1. 按 [接入规范](../../docs/ai-api-testing.md#新建测试项目) 初始化项目，新增用例时复用项目入口。
2. 按 [运行流程](workflows/run-suite.md) 选择离线或实连，记录选中范围。
3. 阅读本次报告并按 [诊断流程](workflows/diagnose.md) 分析失败。
