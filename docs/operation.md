# 操作指南

日常安装、首次离线运行和项目接入见 [README](../README.md)。

交给编码 Agent 新建项目、编排接口或执行回归时，参见 [AI 项目接入与跑批规范](ai-api-testing.md)。

## Agent Loop 实操

第一次使用时，可以从 [商品分页闭环示例](../projects/demo/ai-loop.md) 开始。它提供一次完整演示的运行命令、修复前后的原生报告，以及可以直接交给编码 Agent 的提示词。

先用临时项目观察一次失败进入闭环。下面的用例期待 HTTP 503，离线传输返回 200，因此会产生失败记录：

```bash
work_dir="$(mktemp -d)"
mkdir -p "$work_dir/data"
cat > "$work_dir/data/health.yaml" <<'YAML'
name: health-check
request:
  method: GET
  path: /health
suite: smoke
assert:
  status_code: 503
YAML

LLMOFF=1 AH_LOCAL_OBS=0 uv run --locked ah evolve "$work_dir" --rounds 1
ls "$work_dir/issues"
cat "$work_dir/_evolution_log.jsonl"
```

本例应看到 1 条失败、0 条候选和停止原因。复现草稿保存在 `issues/`。审计日志记录本轮候选数量、筛选结果与回归计划。CLI 的退出码代表演进命令已返回，CI 门禁使用 pytest 或 `ah scheduled-run`。

用已有离线测试验证完整能力：

```bash
LLMOFF=1 AH_LOCAL_OBS=0 AH_TRACE_ENABLED=0 uv run --locked pytest -q \
  tests/test_evolution.py tests/test_predict.py \
  tests/test_failure_orchestrator.py tests/test_execution_contract.py tests/test_receipt.py
```

| 希望验证的能力 | 如何接入 |
|---|---|
| 失败分类与自检 | 调用 `run_single(..., governor=...)`。接入恢复器后，观察类问题可重跑原步骤。产品缺陷保留失败，修复候选按已配置的辅助钩子处理。 |
| AI 提出补测用例 | 给 `AutoEvolutionLoop` 注入 LLM 和专用测试客户端，通过 `Predictor` 或 `AgentCoverage` 生成并试跑。也可注入候选工厂实现团队规则。 |
| 质量门控与复跑 | 审查 `data/evolution.yaml` 与 `_evolution_log.jsonl`，关注实际验证结果和拒绝理由。独立的质量阈值用于筛选，复跑使用明确包含候选的套件范围。 |
| Agent 读取结果 | 在 Python 中使用 `build_verification_receipt` 汇总步骤和断言证据。准备给 CI 或 Agent 做决策时，检查实际执行数、断言与阻塞状态。 |

## 单接口与场景

- 单接口定义包含 `name`、`request` 和 `assert`，可用 `extract` 把响应字段写入上下文。
- 多步骤定义用 `scenario:` 表达顺序。通过 `${变量名}` 引用已提取字段。可按场景配置失败中止策略。
- 多项目复用的请求组可写成 API Skill，用 `flow:` 组合并显式传入输入，参见 [流程示例](../projects/demo/flows/order-create.yaml) 和 [Skill 定义](../projects/demo/skills/)。
- 用例字段、文件布局和 Python 执行入口分别见 [YAML 参考](../skills/auto-api-test-harness/references/cases.md) 和 [运行参考](../skills/auto-api-test-harness/references/run.md)。

## 选择执行方式

| 目的 | 入口 | 结果 |
|---|---|---|
| 离线体验 | `pytest projects/demo` | 用业务形状的桩验证请求渲染、提取、传递和中止，并输出报告 |
| 单条调试 | `ah debug <project> <case> --stub` | 打印请求、响应和断言。当前 CLI 调试使用终端的 PASS/FAIL 判定 |
| 批量回归 | `ah scheduled-run <project> --suite regression` | 请求配置的服务，报告与失败退出码可供 CI 使用。加 `--stub` 切换到通用空响应桩 |
| 组装场景 | `ah run <project> --flow <name>` | 输出流程汇总。缺省使用通用空响应桩，有字段提取的流程需要准备匹配业务的响应桩或显式使用 `--live` |
| 项目巡检 | `pytest projects/<project> --run-live` | 通过脚手架项目的测试入口执行真实请求，pytest 判定结果 |

`ah run` 的筛选条件可能匹配零条用例。用于巡检前先确认命中的用例数。CLI 的通用空响应桩用于接线调试，带响应体断言的业务场景优先参考 demo 的自定义桩。

## 服务与凭证配置

`AH_CONFIG` 可指定配置文件。环境变量 `AH_BASE_URL` 覆盖默认服务，`AH_PROFILE` 选择环境配置。各域可在 `settings.domains` 下配置服务地址和令牌名。真实令牌放在私有令牌存储中。通过环境变量管理的配置应引用变量名。配置示例见 [配置参考](../skills/auto-api-test-harness/references/config.md)。

## OpenAPI 和离线依赖链

```bash
uv run --locked ah openapi example_api openapi.yaml
uv run --locked ah scenario discover --openapi openapi.yaml --har example.har --output-dir drafts/order
uv run --locked ah scenario validate drafts/order/relationships.json
uv run --locked ah scenario compile drafts/order/relationships.json --output projects/example_api/data/order.yaml
```

`openapi` 生成用例骨架后，补齐认证、业务输入和预期。依赖链候选生成后，先确认字段生产者、列表选择条件和业务断言，再运行 `validate` 与 `compile`。使用去除真实凭证的 HAR。产物和命令参数见 [CLI 参考](../skills/auto-api-test-harness/references/cli.md)。

## 本地观察与 AI 辅助

```bash
uv run --locked ah observe seed --db observe-demo.sqlite3
uv run --locked ah observe serve --db observe-demo.sqlite3 --host 127.0.0.1
```

前端可在 `web/` 用 `npm ci` 和 `npm run build` 构建，随后通过 `--web web/dist` 指向构建目录。报告和观察数据可能包含输入和响应内容。模型配置及 Python 辅助接口见 [LLM 参考](../skills/auto-api-test-harness/references/llm.md)。
