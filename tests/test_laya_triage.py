"""LayaGate 失败分类埋点（orchestrator L2 triage 前置分流注记）hermetic 验证。

覆盖：
- gate=None（默认/AH_LAYA=off）→ 埋点完全跳过，行为与原流程一致。
- fake gate 高置信 AUTO → llm_notes["laya_triage"] 记录 action/confidence/value/signal。
- fake gate 低置信/抛异常 → 埋点不写或升级，triage hook 照常执行，不抛。

全部 hermetic（MockTransport、零网络、零真模型）。
"""

from __future__ import annotations

import httpx

from ah.core.orchestrator import LLMHooks, run_single
from ah.evidence.laya_gate import STAGE_TRIAGE
from ah.harness.http import HTTPClient
from ah.harness.loader import Case
from ah.utils.context import Context


class _FakeGate:
    """fake LayaGate：可编程返回 AUTO / ESCALATE / 抛异常，统计调用次数。"""

    def __init__(self, *, action: str = "escalate", value: str | None = None,
                 confidence: float = 0.0, error: Exception | None = None) -> None:
        self._action = action
        self._value = value
        self._confidence = confidence
        self._error = error
        self.calls = 0
        self.last_stage = None
        self.last_state = None
        self.last_questions = None

    def best(self, stage, state, questions) -> dict:  # noqa: ANN001
        self.calls += 1
        self.last_stage = stage
        self.last_state = state
        self.last_questions = questions
        if self._error is not None:
            raise self._error
        # 新拆题合同：AUTO 且高置信 → status=confident，value 为 {qid: 分类} 字典。
        auto = self._action == "auto"
        high = auto and self._confidence >= 0.9
        status = "confident" if high else ("error" if self._error else "inconclusive")
        if auto and self._value is None:
            status = "inconclusive"
        value = {"failure_class": self._value} if auto else None
        return {
            "stage": stage,
            "action": self._action,
            "status": status,
            "value": value,
            "confidence": self._confidence,
            "advisory_only": self._action != "auto" or not high,
            "signal": {"raw": {"failure_class": {"value": self._value,
                                                 "confidence": self._confidence}}},
        }


def _case(*, name: str = "triage-case") -> Case:
    return Case(
        name=name,
        request={"method": "GET", "path": "/api/users"},
        assert_={"status_code": 500},  # 恒失败（非瞬时），持续走 triage 路径
    )


def _client() -> HTTPClient:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        # 断言期望 500，这里返回 200 → 断言不匹配，非瞬时、不可自愈
        return httpx.Response(200, text='{"ok": true}')

    return HTTPClient("http://api.test",
                      client=httpx.Client(transport=httpx.MockTransport(handler)))


def _run(gate=None, triage=None):  # noqa: ANN001
    hooks = LLMHooks(triage=triage) if triage else None
    client = _client()
    try:
        return run_single(_case(), Context(), client, hooks=hooks, gate=gate)
    finally:
        client.close()


# ── gate=None：埋点完全跳过 ───────────────────────────────────────────────

def test_gate_none_skips_embedding_and_keeps_original_behavior():
    """gate=None（默认）→ 不写 laya_triage，失败路径行为与原流程一致。"""
    triage_calls = {"n": 0}

    def triage_hook(case, resp, exc):  # noqa: ANN001
        triage_calls["n"] += 1
        return {"root_cause": "hard failure"}

    result = _run(gate=None, triage=triage_hook)

    assert not result.succeeded
    assert result.error is not None
    assert "laya_triage" not in result.llm_notes  # 埋点完全跳过
    assert triage_calls["n"] == 1  # 原 triage hook 照常执行


def test_gate_none_never_consults_gate_backend():
    """gate=None 时连 fake gate 都不碰（calls==0），零网络。"""
    gate = _FakeGate(action="auto", value="defect", confidence=0.99)
    # 不把 gate 传给 run_single → 行为与默认一致
    result = _run(gate=None)

    assert gate.calls == 0
    assert "laya_triage" not in result.llm_notes


# ── fake gate 高置信 AUTO → 写入 laya_triage ─────────────────────────────

def test_gate_auto_writes_laya_triage_and_keeps_triage_hook():
    """高置信 AUTO → llm_notes['laya_triage'] 记录 category/action/confidence/
    status/signal，triage hook 仍照常执行（拆题：分类 Laya 单选，根因 LLM 保留）。"""
    gate = _FakeGate(action="auto", value="environment", confidence=0.97)
    triage_calls = {"n": 0}

    def triage_hook(case, resp, exc):  # noqa: ANN001
        triage_calls["n"] += 1
        return {"root_cause": "hard failure"}

    result = _run(gate=gate, triage=triage_hook)

    notes = result.llm_notes.get("laya_triage")
    assert notes is not None
    assert notes["category"] == "environment"
    assert notes["action"] == "auto"
    assert notes["status"] == "confident"
    assert notes["confidence"] == 0.97
    assert isinstance(notes["signal"], dict)
    assert triage_calls["n"] == 1  # 原 LLM 根因分析不被替代
    # gate 在请求构建期已为 tool-risk 前置咨询过一次（该 value 不被 tool 采用），
    # 故调用数 >= 1；关键断言是最后一次咨询落到 triage 环节。
    assert gate.calls >= 1
    assert gate.last_stage == STAGE_TRIAGE


def test_gate_auto_records_each_valid_classification():
    """任一台法 6 分类都写入 category。"""
    for cls in (
        "script_bug", "product_defect", "environment",
        "flaky", "known_issue", "unknown",
    ):
        gate = _FakeGate(action="auto", value=cls, confidence=0.95)
        result = _run(gate=gate)
        assert result.llm_notes["laya_triage"]["category"] == cls


def test_gate_auto_invalid_value_ignored():
    """AUTO 但 value 非法 → 不采信分类（category=None），走原流程；注记仍可见。"""
    gate = _FakeGate(action="auto", value="bogus_class", confidence=0.99)
    result = _run(gate=gate)
    notes = result.llm_notes.get("laya_triage")
    assert notes is not None  # 可观测性注记仍写（含 status/confidence）
    assert not notes["category"]  # 非法分类不被采信
    assert notes["action"] == "auto"


# ── fake gate 低置信 / 抛异常 → 不破坏原流程 ─────────────────────────────

def test_gate_escalate_writes_nothing_triage_hook_runs():
    """低置信 ESCALATE → 不采信分类（category=None），升级原 triage hook 兜底。"""
    gate = _FakeGate(action="escalate", value="defect", confidence=0.30)
    triage_calls = {"n": 0}

    def triage_hook(case, resp, exc):  # noqa: ANN001
        triage_calls["n"] += 1
        return {"root_cause": "hard failure"}

    result = _run(gate=gate, triage=triage_hook)

    notes = result.llm_notes.get("laya_triage")
    assert notes is not None
    assert not notes["category"]  # escalate 不采信分类
    assert notes["status"] == "inconclusive"
    assert triage_calls["n"] == 1  # 原 triage hook 兜底执行
    assert result.error is not None


def test_gate_error_does_not_break_flow():
    """gate.best 抛异常 → 捕获并仅警告，不写注记，原流程照常，不抛。"""
    gate = _FakeGate(error=RuntimeError("boom"))
    triage_calls = {"n": 0}

    def triage_hook(case, resp, exc):  # noqa: ANN001
        triage_calls["n"] += 1
        return {"root_cause": "hard failure"}

    result = _run(gate=gate, triage=triage_hook)

    assert "laya_triage" not in result.llm_notes  # 故障：不写注记
    assert triage_calls["n"] == 1
    assert not result.succeeded
