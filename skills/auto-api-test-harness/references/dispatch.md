# 意图识别调度：ah.core.dispatch

`src/ah/core/dispatch.py` 是 **多 agent 意图识别调度**入口：把一句用户自然语言映射到规范 `Intent`，再分发给对应 helper agent 或编排器。这是本 skill 的核心 API 手册。

## 公共符号

```python
from ah.core.dispatch import (
    Intent, DispatchResult, IntentRouter,
    get_router, route_intent,
    GENERATE_DATA, DIAGNOSE, HEAL, ANALYZE_REPORT, PREDICT, ASSIST, RUN_CASE, RUN_FLOW, UNKNOWN,
)
# 顶层也可：from ah import IntentRouter, route_intent, get_router, Intent, DispatchResult
```

## 规范意图标签

| 标签 | 分派目标 | 含义 |
|---|---|---|
| `GENERATE_DATA` | `AgentDataGen.generate_request_body` | 生成请求体/造数 |
| `DIAGNOSE` | `AgentTriage.explain_failure` | 失败原因诊断 |
| `HEAL` | `AgentHeal.heal` | 自愈修复并重跑验证 |
| `ANALYZE_REPORT` | `AgentReport.analyze_results` | 报告洞察 |
| `PREDICT` | `Predictor.propose` | 基于历史结果推断风险并生成新用例（LLM） |
| `ASSIST` | `ah.predict.assist`（AgentImpact/NL2Case/Cluster/Coverage/Quality） | AI 扩展助手：影响分析 / NL→用例 / 失败聚类 / 覆盖缺口 / 质量评分 |
| `RUN_CASE` | `orchestrator.run_single` | 跑单个接口用例 |
| `RUN_FLOW` | `orchestrator.run_flow` | 跑场景流程 |
| `UNKNOWN` | — | 无法识别，回退 |

## 数据类

```python
@dataclass
class Intent:
    label: str
    agent: str                       # helper 类名 或 "orchestrator"
    description: str
    confidence: float = 1.0          # 规则命中时 0.8+；UNKNOWN 为 0.0
    matched_keyword: Optional[str] = None

@dataclass
class DispatchResult:
    intent: Intent
    action: str                      # 实际调用的方法/函数描述
    output: Any = None               # agent/编排器返回值
    skipped: bool = False            # True = 依赖缺失，未执行
    reason: Optional[str] = None     # skipped 时说明原因
    metadata: dict = field(default_factory=dict)
```

## IntentRouter

```python
router = IntentRouter(*, agents=None, keyword_overrides=None, llm=None)
```

- `keyword_overrides`：按意图标签整体替换关键词元组。
- `agents`：注入现成 agent 实例（`{类名: 实例}`）；否则惰性创建。
- `llm`：可选 LLM 实例，用于 `use_llm=True` 且规则未命中时的兜底。

### route —— 识别意图

```python
intent = router.route(text, *, use_llm=True) -> Intent
```

- 大小写无关的子串关键词匹配，**最长关键词得分最高**（`len(kw)`），置信度 `0.8 + min(0.2, score/40)`。
- 全未命中：若 `use_llm and llm is not None`，让强模型把文本归入规范意图之一（best-effort）；否则返回 `UNKNOWN`（confidence=0.0）。
- **隔离保证**：`use_llm=False` 或 `llm is None` 时零网络。

### dispatch —— 识别 + 分派

```python
res = router.dispatch(
    text, *, use_llm=True,
    client=None, ctx=None, case=None,
    results=None, error=None, attempt=1, **agent_kwargs,
) -> DispatchResult
```

各意图分派与依赖：

| 意图 | 需要 | 结果 |
|---|---|---|
| `GENERATE_DATA` | `case_schema`, 可选 `example` | `AgentDataGen.generate_request_body` |
| `DIAGNOSE` | `case`, 可选 `resp`/`error` | `AgentTriage.explain_failure`；无 case → skipped |
| `HEAL` | `case`, `client`, `ctx`, 可选 `resp`/`error`/`attempt` | 按需构造 `AgentHeal(client, ctx, llm=self._llm).heal(...)`；无 case → skipped |
| `ANALYZE_REPORT` | `results` | `AgentReport.analyze_results`；空 → skipped |
| `PREDICT` | `cases`（或 `case`）, `client`, `ctx`；可选 `history_rows`/`limit`/`max_heal_attempts` | 按需构造 `Predictor(llm=self._llm, client, ctx).propose(...)`；缺任一 → skipped。注入 `llm` 才做真实生成，否则空列表 |
| `ASSIST` | 子助手由 `assist_agent`（或匹配关键词）推断；按需 `cases`/`history_rows`/`text`/`client`/`ctx` | 分派 `AgentImpact.analyze` / `AgentNL2Case.to_case` / `AgentCluster.cluster` / `AgentCoverage.propose` / `AgentQuality.score`（`ah.predict.assist`）；缺依赖 → skipped；`llm=None` → 空/None |
| `RUN_CASE` | `case`, `client`, `ctx`；可选 `hooks`/`tracer` | `run_single(...)`；缺任一 → skipped |
| `RUN_FLOW` | `steps`, `client`, `ctx` | `run_flow(...)`；缺任一 → skipped |
| `UNKNOWN` | — | skipped + 提示明确关键词 |

**dispatch 永不抛错**：依赖缺失 → `skipped=True` + `reason`。总是先查 `res.skipped` 再取 `res.output`。

```python
res = router.dispatch("帮我生成请求体", use_llm=False, case_schema={"type": "object"})
if not res.skipped:
    body = res.output   # hermetic: {}
```

### 便捷函数 / 单例

```python
router = get_router(*, agents=None, keyword_overrides=None, llm=None)  # 进程级单例
intent = route_intent(text)             # = get_router().route(text, use_llm=False)
```

### agent 工厂注意

- `router.agent("AgentDataGen"|"AgentTriage"|"AgentReport")` 惰性创建（hermetic）。
- `router.agent("AgentHeal")` / `router.agent("Predictor")` **抛 `KeyError`**——它们需要 client+ctx，只应在 dispatch 对应分支（HEAL/PREDICT）构造。别绕开 dispatch 手动建。
- `ah.predict.assist` 的 5 个 agent 同样只在 `ASSIST` 分支按需构造，未注册进 `agent()` 工厂（`agent("AgentImpact")` 等也会 KeyError）。

## 关键词表（默认）

```python
GENERATE_DATA: ("生成","造数","造数据","请求体","请求参数","body","payload","样例","example")
DIAGNOSE:      ("诊断","根因","失败原因","为什么失败","triage","原因","排查","定位","失败","没通过")
HEAL:          ("修复","自愈","heal","改对","更正","修正","重试后","修 bug","修一修")
ANALYZE_REPORT:("分析报告","报告","总结","洞察","analyze","分析结果","results","复盘")
PREDICT:       ("预测","predict","推断","预判","风险分析","哪些会挂","出问题概率","回归建议")
ASSIST:        ("影响分析","变更影响","覆盖缺口","质量评分","用例质量","失败聚类","聚类","nl2case","自然语言生成用例","生成用例","assist")
RUN_CASE:      ("跑用例","单接口","单个用例","跑一下","执行用例","验证这个","run single","测试这个")
RUN_FLOW:      ("场景","流程","多步","flow","scenario","串联","整条链路","全流程")
```

## 关键约束

- 默认走规则分支（hermetic）；LLM 兜底只在 `use_llm=True` 且显式注入 `llm` 时触发。
- 同分关键词依赖 dict 顺序，规则只是初筛；边角歧义（如"修复失败"）可接受，留给 LLM 精修。
- 调用后检查 `res.skipped`，别假定 `res.output` 始终非空。