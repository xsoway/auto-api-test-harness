# Design · local-obs-platform（本地类 Langfuse 可观测平台）

> 状态：规划。任务：`tasks/active/local-obs-platform/`。

## 1. 定位
在 auto-api-test-harness 之上提供**纯本地**的可观测性看板，数据展示视觉/交互对齐 Langfuse。
不依赖官方远程 Langfuse（可并行存在），零新增运行时依赖，LLMOFF/hermetic 安全。

## 2. 架构
```
┌────────────┐   ingest   ┌─────────┐  API   ┌──────────────────────┐
│ 现有数据    │ ──────────► │ SQLite   │ ◄───── │ stdlib HTTP 服务      │
│ jsonl/report│            │ 索引库   │        │ (ThreadingHTTPServer) │
└────────────┘            └─────────┘        └─────────┬────────────┘
   report_history.jsonl                                  │  /api/* JSON + 静态前端
   llm_usage.jsonl                                       ▼
   report JSON(ResultRecord)                      ┌──────────────┐
   llm_detail.jsonl(新增)                          │ Vite/React 前端│
                                                  └──────────────┘
```
- **数据层**：`src/ah/observe/store.py`（SQLite schema+upsert）+ `ingest.py`（从现有 jsonl/report 重建索引）+ LLM 明细持久化。
- **服务端**：`src/ah/observe/server.py`，标准库，Langfuse 风格 REST。
- **前端**：harness 根 `web/`，Vite + React。
- **CLI**：`ah observe serve / ingest / seed`（挂到 `cli._MODULES`）。

## 3. 数据模型（SQLite，对齐 Langfuse）
| 表 | Langfuse 对应 | 关键列 | 源 |
|---|---|---|---|
| `runs` | trace | id, name, start_time, total/passed/failed/flaky, pass_rate, result_dist(text), status_dist(text), avg/p95/total_duration_ms, per_case_dur_ms(text), total_cost, source_file | report_history.jsonl 一行 + report JSON + llm 聚合 |
| `observations` | span/generation/event | id, run_id, parent_id, name, type, start_ms, latency_ms, level, status, input(text), output(text), model, task, prompt_tokens, completion_tokens, cost, metadata(text) | llm_usage.jsonl 一行(success→generation) + trace 观察 + llm_detail.jsonl + ResultRecord 展开 |
| `tests` | 用例(score 载体) | id, run_id, name, result(PASS/FAIL/FLKY), duration_ms, failure, llm_notes(text: healed/failure_governance/recovered) | report JSON ResultRecord |
| `scores` | score | id, run_id, test_id, name, value(num), comment, source | llm_notes / severity_classify 派生 |

### LLM call 明细（新增，独立于聚合账本）
新增 `llm_detail.jsonl`，每行比 `record_llm_call` 多 `input`/`output`（明文提示词/响应）与 `prompt_tokens`/`completion_tokens`/`cost`。
- **不破坏** `telemetry.record_llm_call` 的现有签名与 `llm_usage.jsonl` 契约。
- `ah.observe.ingest` 摄入 `llm_usage.jsonl`（聚合）与 `llm_detail.jsonl`（明细）为 `observations`。

## 4. 服务端 API（Langfuse 风格，JSON）
| 端点 | 说明 |
|---|---|
| `GET /api/traces?page=&limit=&name=&start=...` | runs 列表（分页/可筛选），行 = {id,name,start,total,cost,pass_rate,status_dist,result_dist} |
| `GET /api/traces/{id}` | trace 详情：meta + observations 树 + tests + scores |
| `GET /api/observations?run_id=&type=` | observations 平铺列表 |
| `GET /api/observations/{id}` | 单 observation（含 input/output/usage/cost） |
| `GET /api/tests?run_id=&result=` | 用例结果列表 |
| `GET /api/scores?run_id=` | 打分列表 |
| `GET /api/metrics` | 成本/token/成功率/成功失败/模型分布/etc（看板） |
| `GET /` + 静态 | 托管 `web/dist`（前端构建产物） |

分页约定：`?page(1-based)&limit(默认50)`，响应 `{data, total, page, limit}`，Langfuse 风格平铺数组项。

## 5. 前端页面（Vite + React，Langfuse 风格）
1. **Traces 列表**：表格（名称/开始时间/耗时/成本/状态分布/通过率），点击进详情。
2. **Trace 详情**：顶部 meta 卡片 + 左侧 span 时间线树（observations 层级缩进、类型图标、耗时、level 颜色）+ 右/下 LLM 调用详情（input/output/token/成本折叠块）+ tests 结果 + scores。
3. **成本看板**：总成本/token、按模型、按 task，趋势。

## 6. 采集扩展（后续可选，本任务不做）
`get_tracer()` 增加本地观察写盘分支 → 运行中实时写入 `llm_detail.jsonl`；本任务只做「摄入现有报告 + seed 演示」。