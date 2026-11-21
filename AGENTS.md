# Auto API Test Harness Agent 指引

在仓库根目录运行命令。业务测试资产放在 `projects/<project_id>/`，框架实现放在 `src/ah/`，框架测试放在 `tests/`。优先复用现有 CLI、YAML schema 和 pytest 入口。

## 按任务选择入口

| 任务 | 先读 | 入口 |
|---|---|---|
| 新建项目或接入 OpenAPI | [AI 项目接入与跑批规范](docs/ai-api-testing.md#新建测试项目) | `ah scaffold`、`ah openapi` |
| 新增单接口、串联全流程或分段执行 | [用例格式](skills/auto-api-test-harness/references/cases.md)、[流程编排](docs/ai-api-testing.md#单接口与场景) | `load_cases`、`run_single`、`run_flow` |
| 跑单条、冒烟、回归或 CI | [跑批与验收](docs/ai-api-testing.md#跑批与验收) | 项目 pytest 入口、`ah scheduled-run` |
| 诊断失败、自检或补测 | [失败诊断](skills/auto-api-test-harness/workflows/diagnose.md)、[AI 闭环](docs/ai-api-testing.md#失败自检与用例扩展) | 保留报告，定位首个失败步骤，再定向复跑 |

[Agent Skill](skills/auto-api-test-harness/SKILL.md) 提供更细的工作流和 CLI 索引。参数以当前命令的 `--help` 和源码为准。

## 执行约定

1. 从用户提供的接口契约、现有用例和项目 README 获取请求方法、目标、认证方式、依赖和业务预期。缺少业务规则时标记待确认，先完成有证据支持的用例和离线验证。
2. 先加载 YAML 并运行项目的离线入口。关模型用 `LLMOFF=1`，隔离被测 HTTP 要用经确认的桩入口或显式 `--stub`。脚手架项目可用 `AH_RUN_LIVE=1` 开启实连，离线运行前检查该变量和项目 conftest。
3. 实连须在用户给定或已授权的环境和执行范围内。核对绝对 URL、服务域、运行账户以及写入和清理范围。批跑的 `--env` 过滤用例标签，连接目标由有效配置和请求路径决定。
4. 凭证走环境变量或私有令牌存储。写入用例、日志摘要或报告前去除真实凭证和敏感样本。保持用例、执行入口与运行产物在各自项目内。
5. 交付时说明项目、运行模式、套件及筛选条件、退出码、实际执行数、通过数、失败数、跳过或阻塞数和本次报告路径。筛选为空、全跳过或缺少本次执行证据时，按实际状态汇报。
6. 失败后先区分用例脚本、数据或环境、产品行为和偶发失败。修复有证据的脚本问题后按原范围复跑。新增候选与断言变更须有契约或业务规则依据，原始失败证据应保留。
