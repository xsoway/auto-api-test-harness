# 新增场景流程（多步串联）

**进入时机**：用户说"加一个登录→下单的场景 / 串联多步 / 跑一遍整条链路"（对应 `run_flow` 前置工序）。

> 两种路线：
> - **`scenario:`（本文件）**——扁平数据流，步骤写在 `data/<flow>.yaml`，共享同一 `ctx`。
> - **`flow:` 组装（skill 互调，新能力）**——把可复用 skill 声明式组装在
>   `flows/<name>.yaml`，`ah run <project> --flow <name>` 执行。适合跨用例复用的业务单元。
>   用户说"把登录/下单这些做成 skill 复用 / 用 skill 拼一条下单流程"走后者，见
>   `docs/operation.md` 的「单接口与场景」（操作手册）与 `projects/demo/flows/`、`projects/demo/skills/`（demo）。

## 前置知识

- 场景 YAML schema：`references/cases.md`
- 编排语义（共享 ctx、on_failure）：`references/run.md`
- 示例见 [场景 YAML](../../../projects/demo/data/order_flow.yaml)。

## 步骤（`scenario:` 扁平路线）

1. **确认服务就绪**（`workflows/add-service.md`）。
2. 在 `projects/<业务>/data/<flow>.yaml` 按示例和实际契约填写场景。新项目走 [项目接入规范](../../../docs/ai-api-testing.md#新建测试项目)。
3. **按顺序列步骤**（`scenario:` 下）：
   - 每一步 = 一个 `Case`（有 `name` / `request` / `assert` / `extract`）。
   - `on_failure: stop`（流程级）时，任一步失败即中止整条链路。
   - 上游 `extract` 出的变量（如 `token` → `user_token`）可在下游 `request` 用 `${user_token}` 引用——**同一条链路共享同一 `ctx`**（`run_flow` 传同一个 ctx）。
4. **验证加载**：
   ```bash
   cd /path/to/auto-api-test-harness
   .venv/bin/python -c \
     "from ah.harness.loader import load_cases; cs=load_cases('projects/<业务>/data/<flow>.yaml'); print(len(cs), cs[0].scenario)"
   ```
   场景被展开为多个 `Case`，`scenario` 相同、`order` 递增。
5. **跑场景**（服务可达时）：
   ```bash
   LLMOFF=1 .venv/bin/python -m pytest -q -p no:cacheprovider projects/<业务>
   ```
   项目内执行入口加载 ``data/`` 流程并 ``run_flow`` 逐场景驱动；默认不连真实服务
   （桩/hermetic），真实巡检 conftest 注册 ``--run-live`` 后实连。
6. **读报告**：确认该项目 ``report/`` 里整条链路通过率。

## 验收

- 场景文件解析为共享 `scenario` id 的多个 `Case`，`order` 正确。
- 变量沿链路传递正确（下游请求里无未解析的 `${...}`）。
- `on_failure` 语义符合预期（`stop` 时失败即停）。

## 关键约束

- **共享 ctx 是流程的粘合剂**——别把变量传断。
- `on_failure` 只在 `stop`/`continue` 选。
- 单步与流程互斥：单用例 `scenario` 为 `None`，场景步必填 `scenario`（由 loader 展开时赋值）。
