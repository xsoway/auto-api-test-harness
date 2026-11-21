"""意图识别调度器验证（hermetic）：意图→agent/编排 的路由与分发。

覆盖 :mod:`ah.core.dispatch`：6 个规范意图的关键词路由、未知意图回退、以及
``dispatch`` 在各意图下对 helper agent / 编排器的正确分发（LLM 关闭时走
确定性分支，零网络）。
"""

from ah.core.dispatch import (
    ANALYZE_REPORT,
    ASSIST,
    DIAGNOSE,
    GENERATE_DATA,
    HEAL,
    PREDICT,
    RUN_CASE,
    RUN_FLOW,
    UNKNOWN,
    IntentRouter,
    route_intent,
)


def _router():
    return IntentRouter()


def test_routes_keyword_intents():
    cases = {
        "帮我生成一个请求体": GENERATE_DATA,
        "生成测试数据": GENERATE_DATA,
        "为什么这个用例失败": DIAGNOSE,
        "诊断一下根因": DIAGNOSE,
        "帮我自愈修复这个用例": HEAL,
        "分析一下运行报告": ANALYZE_REPORT,
        "总结一下测试结果": ANALYZE_REPORT,
        "预测一下哪个接口会出问题": PREDICT,
        "推断接口风险": PREDICT,
        "跑一下这个单接口": RUN_CASE,
        "执行单个用例": RUN_CASE,
        "把整条流程走一遍": RUN_FLOW,
        "跑一遍场景多步流程": RUN_FLOW,
    }
    for text, expected in cases.items():
        got = _router().route(text).label
        assert got == expected, f"{text!r} -> {got}, want {expected}"


def test_unknown_intent_falls_back():
    intent = route_intent("今天天气怎么样")
    assert intent.label == UNKNOWN
    assert intent.confidence == 0.0


def test_route_is_case_insensitive_for_ascii():
    got = _router().route("TRIAGE this failure").label
    assert got == DIAGNOSE


def test_keyword_overrides():
    r = _router()
    r = IntentRouter(keyword_overrides={HEAL: ("修复", "照顾")})
    assert r.route("照顾一下这个用例").label == HEAL
    assert r.route("自愈").label == UNKNOWN  # 覆盖后旧关键词不再命中


def test_dispatch_generate_data_returns_empty_when_llm_off():
    r = _router()
    res = r.dispatch("生成请求体", use_llm=False, case_schema={"type": "object"}, example={"a": 1})
    assert not res.skipped
    assert res.output == {}


def test_dispatch_diagnose_skips_without_case():
    res = _router().dispatch("诊断失败原因", use_llm=False)
    assert res.skipped
    assert res.reason
    assert "case" in res.reason


def test_dispatch_diagnose_hermetic_with_case():
    from ah.harness.loader import Case

    case = Case(name="x", request={"method": "GET", "path": "/users/1"})
    res = _router().dispatch(
        "诊断失败原因", use_llm=False, case=case,
        error=AssertionError("{p} expected 1, got 2"),
    )
    assert not res.skipped
    assert res.output["llm_off"] is True
    assert "hard failure" in res.output["root_cause"]




def test_dispatch_analyze_report_skips_without_results():
    res = _router().dispatch("分析报告", use_llm=False)
    assert res.skipped
    assert res.reason
    assert "results" in res.reason


def test_dispatch_run_case_skips_without_deps():
    res = _router().dispatch("跑一下单接口", use_llm=False)
    assert res.skipped
    assert res.reason
    assert "case/client/ctx" in res.reason


def test_dispatch_heal_hermetic():
    import httpx

    from ah.harness.http import HTTPClient
    from ah.harness.loader import Case
    from ah.utils.context import Context

    def handler(request):
        return httpx.Response(500, json={"err": "boom"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as raw:
        client = HTTPClient("http://api.test", client=raw)
        case = Case(name="h", request={"method": "GET", "path": "/x"})
        res = _router().dispatch(
            "帮我修复", use_llm=False, case=case, client=client, ctx=Context(),
            error=AssertionError("boom"),
        )
    assert not res.skipped
    ok, meta = res.output
    assert ok is False
    assert meta.get("llm_off") is True


def test_dispatch_heal_skips_without_case():
    res = _router().dispatch("修复", use_llm=False)
    assert res.skipped


def test_dispatch_predict_skips_without_deps():
    res = _router().dispatch("预测接口风险", use_llm=False)
    assert res.skipped
    assert res.reason
    assert "cases" in res.reason


def test_intent_enumeration():
    r = _router()
    labels = r.intents()
    for want in (
        GENERATE_DATA, DIAGNOSE, HEAL, ANALYZE_REPORT,
        PREDICT, ASSIST, RUN_CASE, RUN_FLOW,
    ):
        assert want in labels
    assert UNKNOWN not in labels


# ── ASSIST（AI 扩展助手）───────────────────────────────────────────────────

def _assist_cases():
    from ah.harness.loader import Case
    return [Case(name="a", request={"method": "GET", "path": "/x"})]


def test_assist_keywords_route_to_label():
    for text in ("影响分析", "覆盖缺口", "质量评分", "失败聚类", "用自然语言生成用例"):
        assert _router().route(text, use_llm=False).label == ASSIST


def test_assist_impact_hermetic_skips_without_deps():
    res = _router().dispatch("影响分析", use_llm=False)
    assert res.skipped
    assert res.reason
    assert "cases" in res.reason or "history" in res.reason


def test_assist_impact_parses_interfaces():
    from ah.llm.mock import MockLLM
    mock = MockLLM()
    mock.set_json_response(
        {"interfaces": [{"interface": "/x", "impact_score": 0.8,
                        "affected_assert_paths": ["status_code"], "rationale": "r"}]}
    )
    res = IntentRouter(llm=mock).dispatch(
        "这个改动影响哪些接口", use_llm=False, cases=_assist_cases()
    )
    assert not res.skipped
    assert res.metadata["agent"] == "AgentImpact"
    assert res.output[0]["interface"] == "/x"


def test_assist_quality_routes_by_keyword():
    from ah.llm.mock import MockLLM
    mock = MockLLM()
    mock.set_json_response(
        {"reviews": [{"case": "a", "score": 0.5, "assertion_effectiveness": 0.4,
                      "param_coverage": 0.3, "maintainability": 0.8,
                      "issues": [], "suggestion": ""}]}
    )
    res = IntentRouter(llm=mock).dispatch(
        "这几个用例质量评分", use_llm=False, cases=_assist_cases()
    )
    assert not res.skipped
    assert res.metadata["agent"] == "AgentQuality"


def test_assist_cluster_skips_without_history():
    res = _router().dispatch("失败聚类", use_llm=False)
    assert res.skipped
    assert res.reason
    assert "history" in res.reason


def test_assist_coverage_skips_without_client():
    res = _router().dispatch("覆盖缺口", use_llm=False, cases=_assist_cases())
    assert res.skipped
    assert res.reason
    assert "client" in res.reason


def test_assist_nl2case_returns_reviewable():
    from ah.llm.mock import MockLLM
    mock = MockLLM()
    mock.set_json_response(
        {"name": "n", "request": {"method": "GET", "path": "https://m.example.com/x"}}
    )
    res = IntentRouter(llm=mock).dispatch(
        "生成用例：查询详情应返回200", use_llm=False
    )
    assert not res.skipped
    assert res.metadata["agent"] == "AgentNL2Case"
    assert res.metadata["needs_human_review"] is True

# ── LayaGate 意图埋点（hermetic fake gate） ────────────────────────────────

class _FakeGate:
    """fake LayaGate：可编程返回 AUTO 或 ESCALATE。"""

    def __init__(
        self,
        action: str = "escalate",
        value: dict | None = None,
        confidence: float = 0.0,
    ) -> None:
        self._action = action
        self._value = value or {}
        self._confidence = confidence
        self.calls = 0

    def decide(self, stage, state, questions):
        self.calls += 1
        return type(
            "Dec",
            (),
            {"action": type("A", (), {"value": self._action})(), "value": self._value,
             "confidence": self._confidence},
        )()


def test_route_no_gate_keeps_keyword_behavior():
    # gate=None（默认）→ 原流程字节等价：关键词路由不受 Laya 影响。
    assert _router().route("为什么这个用例失败").label == DIAGNOSE


def test_route_gate_auto_shortcuts_to_laya_intent():
    router = _router()
    gate = _FakeGate(action="auto", value={"intent": HEAL}, confidence=0.9)
    intent = router.route("这句没有命中关键词", gate=gate)
    assert intent.label == HEAL  # Laya AUTO 直接采用
    assert gate.calls == 1


def test_route_gate_escalate_falls_back_to_llm():
    from ah.llm.mock import MockLLM
    mock = MockLLM()
    mock.set_json_response({"intent": DIAGNOSE, "reason": "x"})
    gate = _FakeGate(action="escalate", confidence=0.3)
    intent = IntentRouter(llm=mock).route("无关键词文本", gate=gate)
    assert intent.label == DIAGNOSE  # 低置信升级 → LLM 兜底


def test_route_gate_unknown_label_ignored():
    router = _router()
    gate = _FakeGate(action="auto", value={"intent": "not_a_label"}, confidence=0.9)
    intent = router.route("无关键词", gate=gate)
    assert intent.label == UNKNOWN  # 非法标签 → 忽略 Laya，走默认回退


def test_route_gate_backend_error_does_not_break():
    class _BoomGate:
        def decide(self, *a, **k):
            raise RuntimeError("laya boom")
    # 即便 gate 抛错，原流程 keep（LLM 兜底不可用 → UNKNOWN，不抛）。
    assert _router().route("无关键词文本", gate=_BoomGate()).label == UNKNOWN
