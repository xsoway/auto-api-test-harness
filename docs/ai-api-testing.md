# AI 驱动的 API 测试项目接入与跑批规范

适用于 Codex、Pi 等编码 Agent 在本仓库创建项目、维护测试资产及执行回归。运行示例从仓库根目录开始。依赖安装见 [README](../README.md#1-安装)，完整命令见 [CLI 参考](../skills/auto-api-test-harness/references/cli.md)。

## 新建测试项目

| 输入 | 最少需要的信息 |
|---|---|
| 项目 | 稳定的 `project_id`，建议小写英文、数字和下划线，如 `example_api` |
| 服务 | 已知测试目标、接口契约和认证引用。使用多环境时记录各 profile 或配置文件的选择方式 |
| 用例 | 方法、路径、必要入参、预期 HTTP 状态及业务结果。场景另记录步骤依赖、数据准备和清理 |
| 执行 | 本次是离线验收还是真实接口测试，需要跑哪些用例或场景，是否涉及业务写入 |

1. 先检查同名目录。已有项目应复用其执行入口与用例约定。`ah scaffold` 和 `--gen-entry` 都会重写目标测试入口，维护已有项目时先读现有入口。
2. 对尚未存在的项目，用一个已知的健康检查 URL 初始化。下面的回环地址用于演示离线接线。

   ```bash
   LLMOFF=1 uv run --locked ah scaffold example_api --api http://127.0.0.1:8000/health
   AH_RUN_LIVE=0 LLMOFF=1 AH_LOCAL_OBS=0 AH_TRACE_ENABLED=0 uv run --locked \
     pytest -q -p no:cacheprovider projects/example_api
   ```

3. 在 `data/*.yaml` 下维护用例。用例 `name` 在项目内保持稳定且唯一，按实际用途标注 `suite`、`env`、`priority` 和 `tags`。对有业务返回体的接口，按契约加业务码、状态或字段断言。导入本地 OpenAPI 时可用 `uv run --locked ah openapi example_api openapi.yaml`，先检查输出文件，再补齐实际入参、认证和预期。
4. 保留 `test_<project_id>.py` 与 `conftest.py` 作为项目执行入口。脚手架默认离线桩固定返回 HTTP 200 文本，适合健康检查。对于 JSON 断言、非 200 响应、认证及场景变量传递，为相关测试准备符合契约的 `httpx.MockTransport`，参考 [demo](../projects/demo/test_demo.py)。
5. 把契约来源、目标选择、凭证变量名、离线与实连命令、测试数据和清理方式补到项目 `README.md`。如果项目有自己的执行或审批规则，可在 `projects/<project_id>/AGENTS.md` 写明适用范围及例外。

标准目录如下。场景定义和单接口用例继续复用现有格式。

| 路径 | 内容 |
|---|---|
| `projects/<project_id>/data/*.yaml` | 单接口、用例集、`scenario:` 多步骤场景 |
| `projects/<project_id>/skills/`、`flows/` | 可选。可复用 API Skill 和 `flow:` 组合 |
| `projects/<project_id>/test_<project_id>.py`、`conftest.py` | 项目独立执行入口、fixture、报告和实连开关 |
| `projects/<project_id>/README.md` | 业务和运行说明 |
| `projects/<project_id>/report/` | 运行时产生的报告、日志和历史 |

## 单接口与场景

用 [单接口示例](../projects/demo/data/users_single.yaml)、[场景示例](../projects/demo/data/order_flow.yaml) 和 [YAML schema](../skills/auto-api-test-harness/references/cases.md) 核对字段。用例的请求参数和期望从契约获得，认证只引用已配置的令牌名或由项目注入。

| 用户要求 | 编排规则 |
|---|---|
| 只跑一个接口 | 准备该接口自身的认证与必要入参。用项目 pytest 节点或 `ah debug <project> <case>`，先确认所用传输 |
| 跑全链或前几步 | 按声明顺序通过 `run_flow` 执行选中区间，保留变量提取、依赖校验和 `on_failure` |
| 从中间或尾段跑 | 根据选定区间列出缺失的上游输出。用用户提供的测试值或已授权的前置步骤补齐，每次使用独立上下文。必要值仍缺失时标记阻塞 |
| 复用多个业务流程 | 业务操作放 `skills/`，组装条目放 `flows/`，使用 `ah run <project> --flow <name>`，参数说明见 [操作指南](operation.md#单接口与场景) |

脚手架的默认 pytest 入口逐例调用 `run_single`。依赖上游输出的场景需要以 demo 为参考接入 `run_flow`，并验证下游没有消费缺失变量。部分执行通过项目入口准备所需数据，`scheduled-run` 的标签过滤可能切断依赖链。对完整回归场景保持步骤的筛选元数据一致，检查选中列表包含完整链及正确顺序。

## 跑批与验收

先确认项目、模式、套件、用户选定的范围以及实际连接目标。目标还受绝对 URL、`request.domain`、`AH_BASE_URL` 和 profile 影响，见 [配置参考](../skills/auto-api-test-harness/references/config.md)。检查 `AH_CASES` 是否覆盖默认发现范围。已登记的隔离用例默认从 `scheduled-run` 主回归中排除。

| 目的 | 命令示例 |
|---|---|
| 离线项目验收 | `LLMOFF=1 AH_RUN_LIVE=0 uv run --locked pytest -q projects/example_api` |
| 查看 pytest 可收集的节点 | `LLMOFF=1 AH_RUN_LIVE=0 uv run --locked pytest --collect-only -q projects/example_api` |
| 单接口桩调试 | `LLMOFF=1 uv run --locked ah debug example_api health-check --stub` |
| 批跑接线验收 | `LLMOFF=1 uv run --locked ah scheduled-run example_api --suite regression --stub` |
| 测试环境实连回归 | `LLMOFF=1 uv run --locked ah scheduled-run example_api --suite regression --env test --profile <已配置的测试profile>` |
| 项目自定义实连 | `LLMOFF=1 uv run --locked pytest -q projects/example_api --run-live`，适用于注册了该选项的项目 |

- `LLMOFF=1` 只关闭模型调用。对 `scheduled-run` 和 `debug`，省略 `--stub` 时可能实连，`AH_SCHED_STUB` 也可能使批跑走桩。通用桩仅返回 HTTP 200 和空 JSON，业务断言和跨步取值用项目自己的桩测试验证。
- `--env test` 是数据筛选，`--profile` 是配置选择。实连前核对目标域和路径。脚手架生成的用例使用绝对 URL，切换 profile 后仍会命中该 URL。逐项目执行 pytest，生成项目各自在 conftest 注册 `--run-live`。
- 先以默认串行跑批，确认链之间的数据、账号、写入和配额相互独立后，再按需要使用 `--parallel N`。并行只在链之间生效。设置 CI 门禁时保留默认失败退出码。
- 对 `scheduled-run`，命中零条会返回 0 且不生成本次报告。用唯一的 `--basename` 区分批次，在 CI 同时检查退出码、本次报告是否存在和 JSON 中的 `execution_counts.executed`。全量门禁还应核对 `execution_counts.planned` 与选中范围，并检查阻塞、跳过和未验证记录。批跑在 `projects/<project_id>/report/` 输出同名 Markdown 和 JSON，并追加 `history.jsonl`。

验收记录包括项目、执行模式、目标环境、套件和过滤条件、预期命中数、实际执行数、通过、失败、跳过或阻塞、退出码和报告路径。场景报告的统计单位可能是步骤。实连通过、离线桩通过、筛选为空和全跳过分别按实际状态记录。

## 失败自检与用例扩展

1. 先保留本次报告和首个失败步骤。按连接或认证、缺失变量或数据、断言不符和偶发问题定位。场景后续步骤被阻塞时，记录依赖失败原因。
2. 用已有证据区分脚本问题和服务行为。对可修复的脚本问题，最小修改后先离线复跑受影响步骤和场景。写入类请求的实连复跑须考虑首轮是否已产生状态变化。
3. 根据失败模式、接口契约变化和覆盖缺口提出补测候选，说明候选的预期、修复依据和覆盖目标。需要框架模型时有意启用并确认输入材料可发送给该模型。`ah add-case` 会写项目 `data/`，`ah evolve` 可能产生候选和审计产物，先在临时项目评估，再审查并采纳验证过的用例。
4. 交付时列出本次验证结果、仍失败或阻塞的步骤及已采纳的用例。查看 [失败诊断](../skills/auto-api-test-harness/workflows/diagnose.md) 和 [Agent Loop 实操](operation.md#agent-loop-实操) 获取进一步操作。
