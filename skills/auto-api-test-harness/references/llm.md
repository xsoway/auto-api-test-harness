# LLM：BaseLLM / LLMClient / llm_enabled / 4 个 agent

`ah.llm` 层定义 LLM 接入与 4 个辅助 agent。**隔离（`LLMOFF` / 无密钥）时全部零网络、返回确定性固定值**——这是默认，不是降级。

## 接口与开关

```python
from ah.llm import BaseLLM, LLMClient, llm_enabled
llm_enabled()                     # False 当 LLMOFF=1 或缺少密钥

client = LLMClient(...)           # llm_enabled() 为 False 时抛 LLMDisabledError
result = client.generate(task, prompt, *, model="cheap"|"strong")
judged  = client.judge(task, prompt, *, model="cheap"|"strong")
```

- `model` 取值 `"cheap"`（数据生成）与 `"strong"`（诊断/自愈/报告）。
- LLM 关闭时不要去 `client.generate/judge`——会抛 `LLMDisabledError`。agent 内部已处理：`self._llm is None` 时直接给固定值。

## 4 个 helper agent（`src/ah/llm/agents.py`）

### AgentDataGen —— 生成请求体 / 造数

```python
class AgentDataGen:
    def __init__(self, llm: Any = None) -> None
    def generate_request_body(self, case_schema: dict, example: Any = None) -> dict
```

- **隔离**：返回确定性 `{}`。
- 真实 LLM：用 `judge("data-gen", prompt, model="cheap")`，按 `case_schema` 产出一个具体 JSON 实例；`example` 作为参考样例。

### AgentTriage —— 失败诊断

```python
class AgentTriage:
    def __init__(self, llm: Any = None) -> None
    def explain_failure(self, case: Case, resp: Optional[APIResponse], error: BaseException) -> dict
```

- **隔离**：返回 `{"llm_off": True, "root_cause": f"hard failure in {case.name}", "error": str(error)}`。
- 真实 LLM：返回含 `root_cause` 等字段的 dict。

### AgentHeal —— 自愈（反思循环 + 重跑验证）

```python
class AgentHeal:
    def __init__(self, client: Any, ctx: Context, llm: Any = None, *, max_heal_attempts: int = 2) -> None
    def heal(self, case: Case, resp: Optional[APIResponse], error: BaseException, attempt: int) -> tuple[bool, dict]
    def _propose_candidate(self, case, resp, error, attempt) -> Case
```

- **隔离**：`(False, {"llm_off": True, "action": "no-fix-available", "original_error": str(error)})`，零网络。
- 真实 LLM：`_propose_candidate` 提出修正候选 `Case` → `ctx.copy()` 新上下文 → `run_single(candidate, ...)` 重跑验证。通过返回 `(True, {"candidate": ..., "assert": ..., "validated_by": "re-run"})`；否则 `(False, {"candidate": ..., "re_run_error": ..., "action": "re-run-failed"})`。
- **需要 `client` + `ctx` 运行时依赖** → 不能在 `IntentRouter.agent()` 惰性创建，只能在 dispatch HEAL 分支按需构造。

### AgentReport —— 报告洞察

```python
class AgentReport:
    def __init__(self, llm: Any = None) -> None
    def analyze_results(self, results: list[StepResult]) -> str   # markdown
```

- 隔离纯计数 markdown；真实 LLM 补注记。详见 `references/reports.md`。

## 接入真实 LLM（加 key 才启用）

见 `workflows/add-llm.md`。要点：`.env` 设 `OPENROUTER_API_KEY` + 取消 `LLMOFF`；构造 agent/LLMHooks 时注入 llm 实例；所有 hook 异常被 `run_single` 吞掉记 `llm_notes`，绝不破坏隔离跑。

## 关键约束

- **`llm_enabled()` 为 False 时 `LLMClient` 抛 `LLMDisabledError`**——别拿它当可空对象。
- agent 的 `llm=None` 即隔离，返回固定值；真实调用前确认 `llm_enabled()`。
- 不要为了"看起来更智能"擅自给默认跑注入 LLM——默认是隔离的。