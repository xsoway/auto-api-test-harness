# 新增单接口用例

**进入时机**：用户说"给我加一个 get /users/{id} 的用例 / 新增单接口用例 / 测这个接口"（可先 `route_intent` 确认，或直接人工进入本流程）。

## 前置知识

- `Case` / YAML schema：`references/cases.md`
- 配置与 base_url：`references/config.md` + `workflows/add-service.md`
- 示例见 [单接口 YAML](../../../projects/demo/data/users_single.yaml)。

## 步骤

1. **确认被测服务**：base_url 是否为 `settings.base_url`？不是则先 `workflows/add-service.md`。
2. 在 `projects/<业务>/data/<name>.yaml` 按示例和实际契约填写用例。新项目走 [项目接入规范](../../../docs/ai-api-testing.md#新建测试项目)。
3. **填字段**（读 `references/cases.md` 后再写，别猜）：
   - `name`：唯一标识。
   - `request.method` / `request.path`：支持 `${var}`。
   - `request.headers` / `request.json` / `request.params` / `request.data`：按需。
   - `auth`：需要令牌时填令牌名（如 `default_token`）。凭证走 `.env`，别硬编码（`references/config.md`）。
   - `assert`：`status_code` 和/或 `jsonpath`（eq / type 等）。
   - `extract`：需要把响应值传给下游或断言前置变量时填 `jsonpath`。
4. **本地验证**（hermetic 优先）——先确认 YAML 能被正确加载：
   ```bash
   cd /path/to/auto-api-test-harness
   .venv/bin/python -c \
     "from ah.harness.loader import load_cases; print(load_cases('projects/<业务>/data/<name>.yaml'))"
   ```
5. **跑这个用例**（需被测服务可达；或项目 execution 用桩驱动则直接跑）：
   ```bash
   LLMOFF=1 .venv/bin/python -m pytest -q -p no:cacheprovider projects/<业务>
   ```
   或只想跑该用例：`... pytest -q projects/<业务>/test_<业务>.py -k <name>`。
   执行入口带 ``__main__`` 便捷入口，也可等价直跑（透传 pytest 参数）：
   `... python3 projects/<业务>/test_<业务>.py -k <name>`。
   > 每项目自包含：`test_<业务>.py` 显式 `load_cases(data/)` 并逐用例驱动。默认不连真实服务；真实巡检项目在 conftest 注册 `--run-live`，实连加该选项。`LLMOFF=1` 只关 LLM 辅助，HTTP 仍照常发（服务不可达时用例会 FAIL——这正常）。
6. **确认报告**：读该项目 `report/` 下的报告该用例行。

## 验收

- 用例 YAML 能被 `load_cases` 解析（不抛 `ValueError`）。
- 重构后的全套件仍绿：`LLMOFF=1 .venv/bin/python -m pytest -q -p no:cacheprovider`（若你的新用例依赖真实服务且未 mock，跳过此步并说明）。
- `${var}` 插值全部解析，无残留 `${...}` 字面量。

## 关键约束

- **别猜字段**：`on_failure` 只 `stop`/`continue`；`extract.extract_type` 用 `jsonpath`；`assert.jsonpath` 同理。
- 断言失败会写 `StepResult.error` 而非抛到外部——看结果要查 `succeeded`。
- 凭证/密钥绝不进 YAML。
