"""意图识别调度器：把用户自然语言意图路由到对应 agent / 编排器。

这是 harness 的 **多 agent 意图识别调度** 入口。它把一句话（例如"帮我生成 /
诊断 / 修复 / 分析 / 跑这个用例"）映射到一个规范 :class:`Intent`，再分发给
对应的 helper agent 或编排函数：

- ``generate_data``  → :class:`~ah.llm.agents.AgentDataGen`
- ``diagnose``       → :class:`~ah.llm.agents.AgentTriage`
- ``heal``           → :class:`~ah.llm.agents.AgentHeal`
- ``analyze_report`` → :class:`~ah.llm.agents.AgentReport`
- ``run_case``       → :func:`ah.core.orchestrator.run_single`
- ``run_flow``       → :func:`ah.core.orchestrator.run_flow`

调度策略（分层）：
1. **确定性规则**：对文本做大小写无关的关键词匹配，命中即给出高置信度意图。
2. **LLM 兜底（可选）**：当规则置信度低且 LLM 可用时，让强模型把文本归入上述
   规范意图之一（best-effort，不可用则回退到 :data:`UNKNOWN` 意图）。

隔离保证：当 ``LLMOFF`` 或缺少 key 时，:class:`IntentRouter` 只走规则分支，
绝不发起任何网络调用——与全仓隔离原则一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ah.llm.agents import AgentDataGen, AgentHeal, AgentReport, AgentTriage
from ah.utils.logging import get_logger

log = get_logger("dispatch")

# 规范意图标签（路由目标）
GENERATE_DATA = "generate_data"
DIAGNOSE = "diagnose"
HEAL = "heal"
ANALYZE_REPORT = "analyze_report"
PREDICT = "predict"
RUN_CASE = "run_case"
RUN_FLOW = "run_flow"
ASSIST = "assist"
UNKNOWN = "unknown"

_DEFAULT_KEYWORDS: dict[str, tuple[str, ...]] = {
    GENERATE_DATA: (
        "生成", "造数", "造数据", "请求体", "请求参数", "body", "payload",
        "样例", "example",
    ),
    DIAGNOSE: (
        "诊断", "根因", "失败原因", "为什么失败", "triage", "原因", "排查",
        "定位", "失败", "没通过",
    ),
    HEAL: ("修复", "自愈", "heal", "改对", "更正", "修正", "重试后", "修 bug", "修一修"),
    ANALYZE_REPORT: ("分析报告", "报告", "总结", "洞察", "analyze", "分析结果", "results", "复盘"),
    PREDICT: (
        "预测", "predict", "推断", "预判", "风险分析",
        "哪些会挂", "出问题概率", "回归建议",
    ),
    ASSIST: (
        "影响分析", "变更影响", "影响", "覆盖缺口", "质量评分", "用例质量",
        "失败聚类", "聚类", "nl2case", "自然语言生成用例", "生成用例", "assist",
    ),
    RUN_CASE: (
        "跑用例", "单接口", "单个用例", "跑一下", "执行用例", "验证这个",
        "run single", "测试这个",
    ),
    RUN_FLOW: ("场景", "流程", "多步", "flow", "scenario", "串联", "整条链路", "全流程"),
}

_INTENT_DESCRIPTIONS: dict[str, str] = {
    GENERATE_DATA: "为 llm_gen 用例合成请求参数（AgentDataGen）",
    DIAGNOSE: "解释一次失败的根本原因（AgentTriage）",
    HEAL: "提出修正后的用例并重跑验证（AgentHeal）",
    ANALYZE_REPORT: "把运行结果分析为 markdown 摘要（AgentReport）",
    PREDICT: "基于历史结果推断高风险接口并生成新用例（Predictor.propose）",
    ASSIST: "AI 扩展助手：影响分析 / NL→用例 / 失败聚类 / 覆盖缺口 / 质量评分（ah.predict.assist）",
    RUN_CASE: "运行单个接口用例（run_single）",
    RUN_FLOW: "按顺序运行场景流程（run_flow）",
    UNKNOWN: "无法识别的意图——回退到确定性默认",
}


@dataclass
class Intent:
    """一次路由的规范结果。

    ``label`` 是对应的规范意图标识；``agent`` 是目标分派（helper agent 类名或
    "orchestrator"）；``confidence`` 表示规则判定的置信度（0~1）。
    """

    label: str
    agent: str
    description: str
    confidence: float = 1.0
    matched_keyword: Optional[str] = None


@dataclass
class DispatchResult:
    """一次 ``dispatch`` 的产物。

    为避免破坏调用方，当必要依赖缺失时不会抛出，而是设置 ``skipped=True`` 与
    说明性的 ``reason``；真正执行成功时 ``output`` 持有 agent/编排器的返回值。
    """

    intent: Intent
    action: str
    output: Any = None
    skipped: bool = False
    reason: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _default_intents() -> dict[str, Intent]:
    """构建默认意图目录（规范 → 已定义元数据）。"""
    base: dict[str, Intent] = {}
    all_labels = (
        GENERATE_DATA, DIAGNOSE, HEAL, ANALYZE_REPORT, PREDICT,
        ASSIST, RUN_CASE, RUN_FLOW,
    )
    for label in all_labels:
        base[label] = Intent(
            label=label,
            agent="orchestrator" if label in (RUN_CASE, RUN_FLOW) else {
                GENERATE_DATA: "AgentDataGen",
                DIAGNOSE: "AgentTriage",
                HEAL: "AgentHeal",
                ANALYZE_REPORT: "AgentReport",
                PREDICT: "Predictor",
                ASSIST: "Assist",
            }[label],
            description=_INTENT_DESCRIPTIONS[label],
            confidence=0.0,
        )
    return base

# 意图原子题（出题铁律：描述不写标签 + other 逃生口）
_LAYANTENT_QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": "用户想让测试系统做什么？",
        "criteria": {
            "generate_data": "用户想生成测试数据或请求参数",
            "diagnose": "用户想诊断某个测试为什么失败、找出原因",
            "heal": "用户想修复或让失败的测试通过",
            "analyze_report": "用户想对测试结果做分析、总结或复盘",
            "run_case": "用户想运行某个测试用例",
            "run_flow": "用户想按顺序跑完整场景或流程",
            "predict": "用户想预测哪些测试风险高或给出回归建议",
            "assist": "用户想做影响分析、聚类、覆盖缺口、质量评分",
            "other": "以上都不是",
        },
    }
}

class IntentRouter:
    """意图识别 + 多 agent 调度器。

    >>> router = IntentRouter()
    >>> intent = router.route("这个用例为什么失败？")
    >>> intent.label == "diagnose"
    True

    构造时可注入 agent 实例（用于替换 LLM 后端或注入 fixture）；默认惰性创建
    各自无模型的 agent 实例，保证 hermetic。例外：AgentHeal 需要 ``client``/
    ``ctx`` 运行时依赖，故在 :meth:`dispatch` 的 HEAL 分支内按需构造。
    """

    def __init__(
        self,
        *,
        agents: Optional[dict[str, Any]] = None,
        keyword_overrides: Optional[dict[str, tuple[str, ...]]] = None,
        llm: Any = None,
        gate: Any = None,
        tracer: Any = None,
    ) -> None:
        self._intents = _default_intents()
        self._keywords = dict(_DEFAULT_KEYWORDS)
        if keyword_overrides:
            for label, kws in keyword_overrides.items():
                self._keywords[label] = tuple(kws)
        self._llm = llm
        self._gate = gate
        self._tracer = tracer
        self._agents = agents or {}
        self._agent_instances: dict[str, Any] = {}

    # ---- 意图注册 / 覆盖 ----
    def add_intent(self, intent: Intent, keywords: tuple[str, ...] = ()) -> None:
        """注册或覆盖一个规范意图及其关键词。"""
        self._intents[intent.label] = intent
        if keywords:
            self._keywords[intent.label] = tuple(keywords)

    def intents(self) -> list[str]:
        """返回当前可路由的规范意图标识（不含 UNKNOWN）。"""
        return [k for k in self._intents if k != UNKNOWN]

    # ---- 识别 ----
    def route(
        self,
        text: str,
        *,
        use_llm: bool = True,
        gate: Any = None,
    ) -> Intent:
        """识别文本意图；规则失败且 LLM 可用时可选 LLM 兜底。

        返回的 ``Intent`` 若未命中，则 ``label`` 为 :data:`UNKNOWN`、置信度为 0。

        ``gate`` 缺省（None 且 self._gate 有值）时采用构造期注入的进程级
        LayaGate；显式传 gate=None、self._gate 亦为 None → 不回退，保持原流程。
        """
        if gate is None:
            gate = getattr(self, "_gate", None)
        hay = (text or "").lower()
        best: Optional[tuple[float, Intent]] = None
        for label, kws in self._keywords.items():
            for kw in kws:
                if kw.lower() in hay:
                    cand = self._intents.get(label)
                    if cand is None:
                        continue
                    score = float(len(kw))  # 越长越具体，权重越高
                    if best is None or score > best[0]:
                        best = (
                            score,
                            Intent(
                                label=label,
                                agent=cand.agent,
                                description=cand.description,
                                confidence=0.8 + min(0.2, score / 40.0),
                                matched_keyword=kw,
                            ),
                        )
        if best is not None:
            return best[1]
        # LayaGate 前置：规则未命中时先问 Laya（intent 环节，默认关）。
        # 高置信 AUTO → 直接采用；否则落回 LLM 兜底（字节等价护栏）。
        if gate is not None:
            try:
                dec = gate.decide("intent", text, _LAYANTENT_QUESTIONS)
                if dec.action.value == "auto" and isinstance(dec.value, dict):
                    laya_label = dec.value.get("intent")
                    if laya_label in self._intents:
                        base = self._intents[laya_label]
                        return Intent(
                            label=laya_label,
                            agent=base.agent,
                            description=base.description,
                            confidence=dec.confidence,
                            matched_keyword=None,
                        )
            except Exception as exc:  # Laya 故障绝不破坏原流程
                log.debug("intent laya gate failed: %s", exc)
        # LLM 兜底：仅在显式启用且 LLM 可用时。
        if use_llm and self._llm is not None:
            try:
                result = self._llm.judge(
                    "intent-dispatch",
                    "Classify the user request into exactly one of: "
                    + ", ".join(self.intents())
                    + ".\nReturn JSON: {\"intent\": \"<label>\", \"reason\": str}\n"
                    f"REQUEST: {text}",
                    model="strong",
                )
                label = str(result.get("intent", "")).strip()
                if label in self._intents:
                    base = self._intents[label]
                    return Intent(
                        label=label,
                        agent=base.agent,
                        description=base.description,
                        confidence=0.6,
                        matched_keyword=None,
                    )
            except Exception as exc:  # LLM 兜底绝不破坏 hermetic
                log.debug("intent LLM fallback failed: %s", exc)
        return Intent(
            label=UNKNOWN,
            agent="unknown",
            description=_INTENT_DESCRIPTIONS[UNKNOWN],
            confidence=0.0,
        )

    # ---- agent 实例（惰性，hermetic） ----
    def agent(self, name: str) -> Any:
        """按类名取 helper agent 实例；未注入则惰性创建（hermetic）。

        注意：AgentHeal 需要 client+ctx，不在本工厂惰性创建——见 HEAL 分支。
        """
        if name in self._agent_instances:
            return self._agent_instances[name]
        provided = self._agents.get(name)
        if provided is not None:
            self._agent_instances[name] = provided
            return provided
        factory: dict[str, Any] = {
            "AgentDataGen": lambda: AgentDataGen(llm=self._llm),
            "AgentTriage": lambda: AgentTriage(llm=self._llm),
            "AgentReport": lambda: AgentReport(llm=self._llm),
        }
        if name not in factory:
            raise KeyError(f"未知 agent: {name}（AgentHeal 需在 dispatch HEAL 分支按需构造）")
        inst = factory[name]()
        self._agent_instances[name] = inst
        return inst

    # ---- 调度 ----
    def dispatch(
        self,
        text: str,
        *,
        use_llm: bool = True,
        client: Any = None,
        ctx: Any = None,
        case: Any = None,
        results: Optional[list[Any]] = None,
        error: Optional[BaseException] = None,
        attempt: int = 1,
        **agent_kwargs: Any,
    ) -> DispatchResult:
        """识别意图并按需分发给对应 agent / 编排器。

        依赖不足时安全跳过（``skipped=True`` + ``reason``），绝不向外抛出。
        """
        # 先解析意图，再用其 label 命名调度跨度，使过程链路呈现
        # agent→意图识别(dispatch:diagnose)→分发 的具体路由决策。
        _t = self._tracer
        _dspan = None
        if _t is not None:
            intent = self.route(text, use_llm=use_llm)
            _dspan = _t.start_dispatch(text, label=intent.label)
        else:
            intent = self.route(text, use_llm=use_llm)
        label = intent.label
        if _dspan is not None:
            _dspan.finish()

        if label == UNKNOWN:
            return DispatchResult(
                intent=intent,
                action=UNKNOWN,
                output=None,
                skipped=True,
                reason="无法识别意图；请使用明确的关键词（生成/诊断/修复/分析/运行）。",
            )

        if label == GENERATE_DATA:
            schema = agent_kwargs.get("case_schema", {})
            example = agent_kwargs.get("example")
            return DispatchResult(
                intent=intent,
                action="AgentDataGen.generate_request_body",
                output=self.agent("AgentDataGen").generate_request_body(schema, example),
            )

        if label == DIAGNOSE:
            if case is None:
                return DispatchResult(
                    intent=intent, action="AgentTriage.explain_failure",
                    skipped=True, reason="缺少 case 参数。",
                )
            resp = agent_kwargs.get("resp")
            return DispatchResult(
                intent=intent,
                action="AgentTriage.explain_failure",
                output=self.agent("AgentTriage").explain_failure(
                    case, resp, error if error is not None else RuntimeError("unknown")
                ),
            )

        if label == HEAL:
            if case is None:
                return DispatchResult(
                    intent=intent, action="AgentHeal.heal",
                    skipped=True, reason="缺少 case 参数。",
                )
            heal_client = agent_kwargs.get("client", client)
            resp = agent_kwargs.get("resp")
            heal = AgentHeal(heal_client, ctx, llm=self._llm)
            try:
                ok, meta = heal.heal(
                    case, resp, error if error is not None else RuntimeError("unknown"),
                    attempt,
                )
                return DispatchResult(
                    intent=intent, action="AgentHeal.heal",
                    output=(ok, meta),
                    metadata={"ok": ok, "validated": ok},
                )
            except Exception as exc:
                return DispatchResult(
                    intent=intent, action="AgentHeal.heal",
                    output=False, skipped=True, reason=f"heal 失败: {exc}",
                )

        if label == ANALYZE_REPORT:
            if not results:
                return DispatchResult(
                    intent=intent, action="AgentReport.analyze_results",
                    skipped=True, reason="缺少 results 列表。",
                )
            return DispatchResult(
                intent=intent,
                action="AgentReport.analyze_results",
                output=self.agent("AgentReport").analyze_results(results),
            )

        if label == PREDICT:
            cases = agent_kwargs.get("cases") or ([case] if case is not None else None)
            if cases is None or client is None or ctx is None:
                return DispatchResult(
                    intent=intent, action="Predictor.propose",
                    skipped=True,
                    reason="predict 需要 cases（或 case）+ client + ctx。",
                )
            from ah.predict.generator import Predictor
            predictor = Predictor(llm=self._llm, client=client, ctx=ctx)
            history = agent_kwargs.get("history_rows") or []
            kwargs = {"limit": agent_kwargs.get("limit", 3)}
            if agent_kwargs.get("max_heal_attempts") is not None:
                kwargs["max_heal_attempts"] = agent_kwargs["max_heal_attempts"]
            returned = predictor.propose(cases, history, **kwargs)
            return DispatchResult(
                intent=intent,
                action="Predictor.propose",
                output=returned,
                metadata={"validated": [g.validated for g in returned]},
            )

        if label == ASSIST:
            from ah.predict.assist import (
                AgentCluster,
                AgentCoverage,
                AgentImpact,
                AgentNL2Case,
                AgentQuality,
            )

            _assist_agent_kws: dict[str, tuple[str, ...]] = {
                "impact": ("影响分析", "变更影响", "影响"),
                "nl2case": ("自然语言生成用例", "nl2case", "生成用例"),
                "cluster": ("失败聚类", "聚类"),
                "coverage": ("覆盖缺口", "覆盖"),
                "quality": ("质量评分", "用例质量", "质量"),
            }
            agent_kw = agent_kwargs.get("assist_agent")
            if not agent_kw:
                kw = intent.matched_keyword or ""
                for sub, kws in _assist_agent_kws.items():
                    if any(k in kw for k in kws):
                        agent_kw = sub
                        break
                else:
                    agent_kw = "impact"
            cases = agent_kwargs.get("cases") or ([case] if case is not None else None)
            history = agent_kwargs.get("history_rows") or []
            nl_text = text

            if agent_kw == "nl2case":
                if not nl_text:
                    return DispatchResult(
                        intent=intent, action="AgentNL2Case.to_case",
                        skipped=True, reason="nl2case 需要输入文本。",
                    )
                nl = AgentNL2Case(llm=self._llm, client=client, ctx=ctx)
                out = nl.to_case(
                    nl_text,
                    existing=cases or None,
                    validate=bool(agent_kwargs.get("validate", True)),
                    gate=getattr(self, "_gate", None),
                )
                return DispatchResult(
                    intent=intent, action="AgentNL2Case.to_case",
                    output=out,
                    metadata={
                        "agent": "AgentNL2Case",
                        "needs_human_review": getattr(out, "needs_human_review", None),
                    },
                )

            if agent_kw == "cluster":
                if not history:
                    return DispatchResult(
                        intent=intent, action="AgentCluster.cluster",
                        skipped=True, reason="cluster 需要历史失败记录 history_rows。",
                    )
                out = AgentCluster(llm=self._llm).cluster(
                    history, max_clusters=int(agent_kwargs.get("max_clusters", 8))
                )
                return DispatchResult(
                    intent=intent, action="AgentCluster.cluster",
                    output=out, metadata={"agent": "AgentCluster", "clusters": len(out)},
                )

            if agent_kw == "coverage":
                if not cases or client is None:
                    return DispatchResult(
                        intent=intent, action="AgentCoverage.propose",
                        skipped=True,
                        reason="coverage 需要 cases + client（验真闭环）。",
                    )
                cov = AgentCoverage(llm=self._llm, client=client, ctx=ctx)
                cov_opts: dict[str, Any] = {}
                if isinstance(agent_kwargs.get("spec"), str):
                    cov_opts["spec"] = agent_kwargs["spec"]
                out = cov.propose(
                    list(cases),
                    limit=int(agent_kwargs.get("limit", 3)),
                    **cov_opts,
                )
                return DispatchResult(
                    intent=intent, action="AgentCoverage.propose",
                    output=out,
                    metadata={
                        "agent": "AgentCoverage",
                        "validated": [g.validated for g in out],
                    },
                )

            if agent_kw == "quality":
                if not cases:
                    return DispatchResult(
                        intent=intent, action="AgentQuality.score",
                        skipped=True, reason="quality 需要 cases。",
                    )
                out = AgentQuality(llm=self._llm).score(
                    list(cases), max_cases=int(agent_kwargs.get("max_cases", 100))
                )
                return DispatchResult(
                    intent=intent, action="AgentQuality.score",
                    output=out, metadata={"agent": "AgentQuality", "reviews": len(out)},
                )

            # 默认 impact
            if not cases and not history:
                return DispatchResult(
                    intent=intent, action="AgentImpact.analyze",
                    skipped=True, reason="impact 需要 cases 或 history_rows。",
                )
            out = AgentImpact(llm=self._llm).analyze(
                cases=cases or [],
                history=history,
                changes=agent_kwargs.get("changes", ""),
                top_k=int(agent_kwargs.get("top_k", 10)),
            )
            return DispatchResult(
                intent=intent, action="AgentImpact.analyze",
                output=out,
                metadata={"agent": "AgentImpact", "interfaces": len(out)},
            )

        if label == RUN_CASE:
            if case is None or client is None or ctx is None:
                return DispatchResult(
                    intent=intent, action="orchestrator.run_single",
                    skipped=True, reason="run_single 需要 case/client/ctx。",
                )
            from ah.core.orchestrator import run_single as _run_single
            return DispatchResult(
                intent=intent,
                action="orchestrator.run_single",
                output=_run_single(
                    case, ctx, client,
                    **{k: v for k, v in agent_kwargs.items() if k in ("hooks", "tracer")},
                ),
            )

        if label == RUN_FLOW:
            steps = agent_kwargs.get("steps")
            if steps is None or client is None or ctx is None:
                return DispatchResult(
                    intent=intent, action="orchestrator.run_flow",
                    skipped=True, reason="run_flow 需要 steps/client/ctx。",
                )
            from ah.core.orchestrator import run_flow as _run_flow
            return DispatchResult(
                intent=intent,
                action="orchestrator.run_flow",
                output=_run_flow(list(steps), ctx, client),
            )

        return DispatchResult(
            intent=intent, action="noop", skipped=True, reason=f"未处理意图 {label}"
        )


_router: Optional[IntentRouter] = None


def get_router(
    *,
    agents: Optional[dict[str, Any]] = None,
    keyword_overrides: Optional[dict[str, tuple[str, ...]]] = None,
    llm: Any = None,
    gate: Any = None,
) -> IntentRouter:
    """获取进程级共享的 :class:`IntentRouter`（惰性构建一次）。

    ``gate`` 仅在首次构建时生效（后续调用忽略，避免覆盖已注入的进程级
    LayaGate）。Hermetic 下单例不触发任何网络。
    """
    global _router
    if _router is None:
        from ah.core.trace import get_tracer

        # 注入共享 tracer：本地链路开启时，dispatch 的意图调度环节也写进
        # 过程链路（run→dispatch→…）。空跟踪器时 start_dispatch 为无操作。
        _router = IntentRouter(
            agents=agents, keyword_overrides=keyword_overrides, llm=llm,
            gate=gate, tracer=get_tracer(),
        )
    return _router


def route_intent(text: str) -> Intent:
    """便捷函数：识别一句话的规范意图（使用共享 router 的规则分支）。"""
    return get_router().route(text, use_llm=False)
