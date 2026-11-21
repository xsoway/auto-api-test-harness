# CLI 命令参考：ah（python -m ah.cli）

统一 CLI 入口 `src/ah/cli.py`：把各功能子模块的命令挂到 argparse 并分发。安装后 `ah <子命令>`；未安装用 `python -m ah.cli <子命令>`。

**全局开关**（作用于所有子命令）：
- `--llm-off`：等价 `LLMOFF=1`，关闭模型调用。
- `--debug`：显示 DEBUG 日志（默认 INFO）。
- `--version`：显示框架版本。

离线执行还要选择对应的 HTTP 桩入口。`scheduled-run` 与 `debug` 可显式传 `--stub`，参见 [跑批与验收](../../../docs/ai-api-testing.md#跑批与验收)。

## 子命令总览

| 子命令 | 模块 | 作用 | 判闸语义 |
|---|---|---|---|
| `run`, `evolve`, `trend-gate` | `ah.evolution` | 跑套件 / 自主涌现闭环 / 趋势闸 | `run` 有用例失败时返回非零；`evolve` 看轮次结果 |
| `scaffold`, `add-case` | `ah.scaffold` | 业务项目骨架 / NL→用例 | 纯本地，无判闸 |
| `scheduled-run`, `ci-trigger`, `flaky-quarantine` | `ah.scheduler` | 回归调度 / 变更触发 / flaky 隔离 | scheduled-run 任一失败→非零 |
| `telemetry` | `ah.telemetry` | LLM 成本聚合看板 | 只聚合，返回 0 |
| `property` | `ah.property` | Hypothesis 参数空间探索 | 全部样本失败→非零 |
| `debug` | `ah.debug` | 单用例独立执行并 dump 请求/响应/断言 | 用例找不到/失败→非零 |
| `openapi` | `ah.debug` | 把 OpenAPI 3.x paths 导入为最小用例 | 无文件/解析失败→非零 |
| `scenario discover/validate/compile` | `ah.scenario.cli` | 离线候选、确认校验与场景编译 | 校验/编译失败非零，未确认不可执行 |
| `observe` | `ah.observe.cli` | 本地类 Langfuse 可观测平台（serve/ingest/seed/llm-detail） | 服务无判闸，返回 0 |

CLI 挂载约定：子模块公开 `register(subparsers)`，在 `cli.py` 的 `_MODULES` 表登记；入口多绑定 `set_defaults(func=...)`（evolution 例外用 `handler=...`）。加新子命令 = 新模块 register + 在 `_MODULES` 加一行 + 惰性 import。

## evolution（自主涌现）

```bash
ah run <project> --flow <name>   # 只执行一条 skill 组装流程（flows/<name>.yaml），不走用例套件
ah run <project> [--suite smoke|regression|prod|all] [--env E] [--priority P0,P1] [--tags t] [--diff ref] [--report-dir d]
ah evolve <project> [--suite ...] [--env] [--priority] [--tags] [--diff] [--report-dir] [--rounds N] [--budget N] [--min-quality F] [--dry-run] [--undo]
ah trend-gate <project> [--suite ...] [--drop P] [--k K]
ah evolution-help
```

- `run`：跑一套用例并出报告（默认 `--suite smoke`）；`project` 为项目名（`projects/<project>`）或含 `data/` 的目录路径。`run --flow <name>` 不走用例套件，只执行 `<project>/flows/<name>.yaml` 的 skill 组装流程（`call/with/assign` 互调 + 内联步），任一 `call`/内联步失败返回非零。
- `evolve`：自主涌现闭环 `跑→分析→生成→持久化→下一轮`。`--rounds`（默认 3）、`--budget`（采纳候选预算 10）、`--min-quality`（质量门控 0.6）、`--diff <ref>`（git 变更驱动，只对受影响接口生成）、`--undo`（删除最近一轮新增演进用例）。
- 停轮条件：达到 `max_rounds` | `budget` 耗尽 | 无新候选采纳 | 全绿两轮无变化。
- `trend-gate --drop P --k K`：近 K 次通过率下滑 ≥ P → 自动升套件（smoke→regression→prod）或追加候选生成一轮，stderr + 报告横幅告警。

CLI 演进使用离线传输和确定性候选路径。需要 LLM 生成和候选试跑时，通过 Python 向 `AutoEvolutionLoop` 注入模型与客户端。

**持久化**：候选写回 `data/evolution.yaml`（追加、`# --- ah-evolved round N ---` 段分隔、`(method,path,name)` 去重、`tags:[ah-generated]`、`suite: evolution`）。审计日志 `_evolution_log.jsonl`（候选/门控/采纳/拒绝/理由）。
**质量护栏**：候选过 `min_quality` 门控，低分拒绝记 `rejected_reason`；hermetic 走确定性启发式评分（断言类型/参数覆盖），不依赖 LLM。
**失败自动动作**：triage 结论 → 生成 issue/bug 单草稿（写项目 `issues/` 或 `_evolution_log.jsonl`），带复现用例。
**回归计划**：消费 `rank_regression` → `RegressionPlan` 反推 `--suite`/`--priority` 触发回归。

## scaffold（脚手架）

```bash
ah scaffold <business> [--api <url>] [--gen-entry] [--name <fn>] [--project-dir <dir>]
ah add-case <project> "<自然语言>" [--run-live] [--project-dir <dir>]
```

- `scaffold <business>`：生成业务项目骨架 `projects/<business>/`（`data/` 最小用例 + `test_<name>.py` 执行入口 + `conftest.py` + `README.md`），带 hermetic 护栏与 `--run-live` 巡检范本。`--api` 为被测服务 URL 或 spec 路径。
- `scaffold --gen-entry <business>`：从已有 `data/*.yaml` 用 `load_cases` 确定性生成/刷新 `test_*.py`（纯代码生成，不依赖 LLM）。
- `add-case <project> "文本"`：用 `ah.predict.assist.AgentNL2Case` 把自然语言转成用例，**追加 + 去重**写回 `data/<name>.yaml`；可选 `--run-live` 对真实服务验证。
- `scaffold` / `--gen-entry` 是纯本地文件生成；`add-case` 在 LLM 关闭时返回明确错误（提示需配置 LLM），零网络。

## scheduler（调度）

```bash
ah scheduled-run <project> [--suite smoke|regression|prod|all|quarantine] [--priority P0,P1] [--env E] [--tags t] [--basename fn] [--stub] [--no-gate] [--parallel N] [--profile NAME]
ah ci-trigger <project> [--diff ref] [--changes 'f1 f2'] [--stub] [--no-gate]
ah flaky-quarantine <project> [--min N] [--dry-run]
```

- `scheduled-run`：按用例集筛选用例跑回归出报告；**任一失败返回非零**（判闸）。`--suite` 缺省 regression；`quarantine` 按隔离仓标签挑选。`--stub` 用 `MockTransport` hermetic 端到端跑；`--no-gate` 关闭判闸（失败仍返回 0）。`--parallel N` 并发执行（>1 时只并行相互独立的用例链，同场景仍按顺序）；`--profile NAME` 切换多环境 profile（覆盖 base_url/domains）。
- `ci-trigger`：对 git diff 受影响路径做影响分析，只跑受影响接口用例。`--diff <ref>`（默认 HEAD 对比工作区）；`--changes` 显式给变更文件路径（优先于 git 解析）。LLM 关闭时回退确定性路径匹配。
- `flaky-quarantine`：隔离仓 = **标签 `tags:[quarantined]` ∪ 失败簇群 quarantined 状态**
  （`data/failure_clusters.json` 中 `ClusterStore` 状态机判定的用例）。两种来源的
  用例都从主回归排除、单独 `--suite quarantine` 可跑。历史连续 flaky ≥ N（默认 2）打
  `quarantined` 标签写回 YAML；簇群隔离按 `(method,path,failure_class)` 指纹聚合，
  `record_pass` 回移后 quarantined→recovered 自动解除隔离。`--dry-run` 只列出不写回。

## telemetry（遥测）

```bash
ah telemetry <project> [--fields cost,requests,success,errors,success_rate] [--report-dir <dir>] [--json]
```

从本地 LLM 用量账本（`<report_dir>/llm_usage.jsonl`）聚合 LLM 调用 `cost / success / error` 计数，输出可配置摘要（CLI 打印；`--json` 另写 `llm_telemetry.json` 报告段）。只做聚合，不改 `core/trace.py` 埋点；hermetic 账本为空→0/空，绝不触网。`--report-dir` 覆盖账本所在目录。

## property（属性探索）

```bash
ah property <project> <case-name> [--examples N] [--seed S] [--json]
```

用 Hypothesis 对用例 `request.json` / `params` 做参数空间探索，复用 `ah.metrics.assertions.assert_response` 判每个样本。Hypothesis 为 optional dependency：未装报清晰错误，已装零配置不静默降级。`--examples`（默认 100）样本数、`--seed` 确定性复现；HTTP 走注入的 `HTTPClient`（测试用 `httpx.MockTransport` 注入即零网络）。全部样本失败→判闸非零。

## observe（本地可观测平台）

纯本地、类 Langfuse 的可观测看板：把 `report/` 数据摄入 SQLite 重建索引，Vite/React
前端浏览 Traces / Trace 详情（span 时间线 + LLM input/output/token 成本）/ 成本看板。
零新增运行时依赖（标准库 `ThreadingHTTPServer`），LLMOFF/hermetic 安全、不发网。

```bash
ah observe seed     [--db PATH] [--runs N]              # 灌入合成演示数据（含 LLM 明细）
ah observe ingest   <report_dir> [--name PREFIX] [--db PATH]  # 摄入真实 report/
ah observe serve    [--db PATH] [--host H] [--port N] [--web DIR]  # 启动 HTTP 服务
ah observe llm-detail ...                               # 写一条 LLM 明细（演示/测试）
```

- `serve`：`--web` 指向 Vite build 产物（缺省探 harness 根 `web/dist`）。`GET /` 托管前端；
  `/api/*` 提供 JSON API（`/api/metrics`、`/api/traces[/{id}]`、`/api/observations[/{id}]`、
  `/api/tests`、`/api/scores`）。URL 编码 id（含冒号/空格）由 `server.unquote` 解码。
- `ingest`：重建 runs / GENERATION observations / tests / scores 索引；`--name` 作 run 前缀。
- `seed`：灌合成数据，便于看板演示（含可展开的 LLM input/output 明细）。
- `llm-detail`：写一条 LLM 明细到 `llm_detail.jsonl`（`ah.observe.llmdetail.record_llm_detail`），
  不改 `telemetry.record_llm_call` 签名 / `llm_usage.jsonl` / `Tracer`。`llm_usage.jsonl` 只含
  聚合（cost/tokens/latency）无 per-call 入参；input/output 在明细里，前端 GENERATION 节点据此展开。

**前端构建**（首次 / 改前端后）：`cd web && npm install && npm run build` → 产物 `web/dist`
供 `serve --web` 托管。

实现：`src/ah/observe/`（`store.py` / `ingest.py` / `server.py` / `llmdetail.py` / `cli.py`）。
设计见 `docs/designs/local-obs-platform.md`。

## 关键约束

- 机器判闸使用 pytest 或 `scheduled-run`。 `evolve` 的失败数记录在轮次结果中。
- 所有子命令 LLMOFF 可跑，LLM 能力绝不隐式触网。
- 加新功能点子命令：新模块注册 + `_MODULES` 登记 + 必要时补 `references/cli.md`。

## scenario（离线依赖链）

```bash
ah scenario discover --openapi spec.yaml --har session.har --output-dir project/drafts/chain --name chain
ah scenario validate project/drafts/chain/relationships.json [--output validation.json]
ah scenario compile project/drafts/chain/relationships.json --output project/data/chain.yaml [--notes modeling_notes.md]
```

`discover` 只读取本地 OpenAPI/HAR，生成 `relationships.json`、`validation.json`、`modeling_notes.md`。输出目录必须为新目录且不能位于 `data/` 中。可重复 `--har` 提供独立会话证据，不自动将多个会话拼为一条链。`--service` 缺省为 `default`。

审阅 edge 的字段来源、scope 和 evidence，选定生产者改为 `confirmed`，其他候选 `rejected`；`observed` 只表示相等值证据。列表需稳定业务键 selector 和 `selection_confirmed: true`。外部值在 `inputs` 声明并运行时注入，不能把抓包鉴权值固化。步骤 `assertions` 需人工补充业务 oracle，默认状态码不是业务终态证明。

`validate` 与 `compile` 共用严格校验：未知接口、前向/无来源变量、歧义、跨 scope、类型不符、空断言和 unsupported 都阻止发布。编译生成 `schema_version: 2` 的现有 YAML 与显式 `depends_on`；任何输出均不覆盖已有文件。完整操作与安全边界见 `docs/operation.md` 的「OpenAPI 和离线依赖链」。
