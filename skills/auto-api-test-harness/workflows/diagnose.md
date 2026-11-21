# 失败诊断（AgentTriage）

**进入时机**：用户说"这个用例为什么 500 / 诊断失败原因 / 定位根因"——对应 `diagnose`。

## 前置知识

- `references/llm.md`（AgentTriage 行为）
- `references/dispatch.md`（dispatch 的 diagnose 分支依赖）
- `references/run.md`（StepResult.error）

## 步骤

1. **拿到失败事实**：确定是哪个 `Case` 失败，及其 `StepResult.error`（原异常）、`response.status_code`、`response.text`。来自 `report.md` 或运行输出。

2. **用 AgentTriage 解释**（两条路径任选）：

   **A. 直接调 agent（推荐，结构清晰）**
   ```python
   from ah.llm.agents import AgentTriage
   from ah.harness.loader import Case

   case = Case(name="get-user-by-id", request={"method": "GET", "path": "/users/1"})
   triage = AgentTriage(llm=None)                     # hermetic
   diag = triage.explain_failure(case, resp, error)
   # {"llm_off": True, "root_cause": "hard failure in get-user-by-id", "error": ...}
   ```

   **B. 走 dispatch（当你想从一句话自动路由）**
   ```python
   from ah.core.dispatch import get_router
   res = get_router().dispatch(
       "为什么失败", use_llm=False,
       case=case, resp=resp, error=error,
   )
   if res.skipped:            # 缺 case 会 skipped
       print(res.reason)
   else:
       print(res.output)      # triage dict
   ```

3. **人工核对根因**：dispatch/agent 给出的 `root_cause` / `error` 只是辅助线索，结合 `response.text` 与断言规则判断真实原因：
   - 状态码不符 → 服务逻辑 / 请求参数问题。
   - jsonpath 取不到 → 响应结构变化 / 提取路径错。
   - 连接错误 → 服务不可达（`workflows/run-suite.md` 说过是预期）。
4. **修复**：
   - 改 `assert` / `extract` / `request` → 改 YAML 后重跑。
   - 想自动修正 → 走 `HEAL`（见下，AgentHeal 需 client+ctx）。

## HEAL 分支（可选，自动修 + 重跑验证）

```python
from ah.core.dispatch import get_router
from ah.harness.http import HTTPClient
from ah.utils.context import Context

res = get_router().dispatch(
    "帮我修复", use_llm=False,
    case=case, client=client, ctx=Context(), error=error,
)
# 隔离下: ok=False, meta={"llm_off": True, "action": "no-fix-available", ...}
ok, meta = res.output
```

> **AgentHeal 只在 dispatch HEAL 分支构造**（需要 client+ctx）；别 `router.agent("AgentHeal")`（会 `KeyError`）。真实 LLM 下才会提出候选并重跑。

## 验收

- 你能对每个失败用例说清：断言/提取/连接中的哪一类根因。
- 如需自愈，明确给出通过/失败结果与 `meta` 依据。

## 关键约束

- **先拿失败事实再下结论**，别凭印象。
- LLM 极低时诊断是固定文本，真正根因要靠你人工核对报文与断言。