# 编排：run_single / run_flow / StepResult / LLMHooks

`src/ah/core/orchestrator.py` 是执行核心。以下签名与行为以此文件为准。

## run_single

```python
def run_single(
    case: Case,
    ctx: Context,
    client: HTTPClient,
    hooks: Optional[LLMHooks] = None,
    tracer: Any = None,
    gate: Any = None,
    governor: Optional[FailureGovernor] = None,
) -> StepResult
```

`gate` 注入 LayaGate（决策模型前置层，见 `references/llm.md`）；`governor` 注入
失败治理链（`ah.failure.governor.FailureGovernor`）。两者默认 `None` → 字节等价
原流程，`LLMOFF` 安全。

执行单个用例，**永不抛出异常**（异常被捕获写入 `StepResult.error`）。

顺序：
1. 若 `hooks.data_gen` 且 `case.llm_gen`：调 data_gen hook 生成 `extra_json` 注入 `request`。失败只记 `llm_notes["data_gen_error"]`。
2. `_build_request(case, ctx, extra_json)` 做 `${var}`/占位符深渲染，得到 method/path/params/json/data/headers/auth，且透传 `"timeout": case.timeout`（缺省 `None` 用客户端默认）。
3. **Phase A 可配置重试循环**（`run_single` 内、L1 自愈之前）：首个请求失败且命中瞬时错误（`_is_retryable_error`：传输层/超时/连接，或 5xx 响应）时，按 `case.retries` 退避 `case.retry_backoff` 秒重试；成功后 `succeeded=True` 并记 `llm_notes["retried"]`（`attempts/configured/backoff/trigger`）。确定性错误（模板缺失 `${var}`、断言不匹配）不重试。
4. `_execute()` 单次管道：`client.request(...)` → `extract_vars` → `assert_response` → 收集断言详情。legacy 仍先提取并发布变量；严格 v2 使用临时上下文，只有验证成功才发布产出。
5. 软断言：legacy 的 `case.soft_assert=True` 只记录失败，`succeeded` 可仍为真；机器 `verdict` 不把失败断言标为已验证通过。严格 v2 不能借软断言发布不可信输出。
6. 重试用尽后（非瞬时或超限）：若 `hooks.triage` 调 triage 记根因 → **失败治理分支**（仅当 `governor` 注入时）：`governor.decide(...)` 返回 `block`（product_defect）→ 终止并保留失败（等人工）；`observe` → `governor.observe_recover(...)` 无侵入恢复（只重跑原步骤，成功记 `llm_notes["recovered"]` 且 `wrote_script=False`，绝回写脚本）；`heal` → L1 确定性自愈（原样重跑一次，成功记 `llm_notes["healed"]`）→ 若 `hooks.heal` 且 `result.response` 非空进入 L2 LLM 自愈循环（最多 `hooks.max_heal_attempts` 次）；候选通过重跑则 `succeeded=True` 并记 `llm_notes["healed"]`。未注入 governor → 原 heal 分支直行。
## StepResult

```python
@dataclass
class StepResult:
    name: str
    succeeded: bool
    response: Optional[APIResponse] = None
    extracted: dict[str, Any] = dict
    error: Optional[BaseException] = None
    attempts: int = 1
    llm_notes: dict[str, Any] = dict

    @property
    def status_code(self) -> Optional[int]
    def to_dict(self) -> dict                    # asdict
    @classmethod
    def from_dict(cls, data: dict) -> "StepResult"  # 结果持久化反序列化
```

## run_flow

```python
def run_flow(
    steps: list[Case],
    ctx: Context,
    client: HTTPClient,
    hooks: Optional[LLMHooks] = None,
    tracer: Any = None,
) -> list[StepResult]
```

按顺序执行步骤列表，返回每个步骤的 `StepResult`。场景内步骤共享同一 `ctx`，因此上一步 `extract` 出的变量（如 `user_token`）可供下一步 `request` 使用。`on_failure: stop` 的语义在步骤内体现。

## LLMHooks

```python
@dataclass
class LLMHooks:
    data_gen: Optional[Callable[[Case, Context], dict]] = None
    triage: Optional[Callable[[Case, APIResponse, BaseException], dict]] = None
    heal: Optional[Callable[[Case, APIResponse, BaseException, int], tuple[bool, dict]]] = None
    max_heal_attempts: int = 2
    observe: Optional[
        Callable[[Case, APIResponse, BaseException, Context, int], dict]
    ] = None  # observe 无侵入恢复钩子（governor 开启时用于 observe 动作）
```

全部默认 `None` → 隔离模式（无任何 LLM 调用）。真实辅助时把 4 个 agent 的方法/闭包接入对应 hook：

```python
from ah.core.orchestrator import LLMHooks, run_single
from ah.llm.agents import AgentDataGen, AgentTriage, AgentHeal

hooks = LLMHooks(
    data_gen=lambda case, ctx: AgentDataGen(llm=llm).generate_request_body(case.llm_gen),
    triage=lambda case, resp, err: AgentTriage(llm=llm).explain_failure(case, resp, err),
    heal=lambda case, resp, err, attempt: AgentHeal(client, ctx, llm=llm).heal(case, resp, err, attempt),
)
result = run_single(case, ctx, client, hooks=hooks)
```

## HTTPClient

`src/ah/harness/http.py`：

```python
client = HTTPClient(base_url, client=None, trace=None)
resp = client.request(method, path, params=None, json=None, data=None, headers=None, auth=None)
# -> APIResponse（内含 status_code / text / json() 等；validation 用于断言元数据）
```

## 关键约束

- `run_single` / `run_flow` 都不抛异常；检查 `step.succeeded` / `step.error`。
- `AgentHeal` 需要 `client` + `ctx`；`run_single` 内部用 `ctx.copy()` 构建全新上下文旅以重跑候选。

## v2 执行与机器结果

Case 新增 `reference_mode`（legacy/typed）、`strict`、`depends_on`；场景 `schema_version: 2` 由 loader 自动配置，旧 YAML 默认兼容。typed 的整值引用保持原生类型，文本位置保持插值；`data` 递归渲染。

StepResult 新增：

- `status`：executed / blocked / skipped。后两者 `attempts=0`，不发 HTTP。
- `verdict`：passed / failed / unverified。零断言为 unverified；`succeeded` 是兼容执行标志，不能替代验证判定。
- `reason`：未验证/未执行原因；`duration`：逻辑步骤执行时间。

`run_flow` 保留全部计划步骤。stop 后的剩余步骤不再从列表消失；continue 下强依赖失败仍阻塞，独立步骤可执行。pytest 插件、scheduler 串行/并行和 ServiceRegistry 使用相同流程策略。严格场景上下文隔离，不借另一个场景的同名输出闭合依赖。

`summarize_results(results)` 提供 planned/executed/passed/failed/blocked/skipped/unverified，以及实际收集的 assertions/assertions_passed/assertions_failed。该计数不证明关键业务断言已覆盖，不包含方案 C 的完整构建/环境身份契约。