#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/10 10:50
# @Filename : test_laya_tool.py
# @Author   : Alan_Hsu
"""LayaGate 工具调用风险路由埋点（orchestrator 前置 advisory 注记）hermetic 验证。

覆盖：
- gate=None（默认/AH_LAYA off）→ 不写 laya_tool_risk，执行流程字节等价、零网络。
- fake gate 高置信 AUTO high/medium → 写 laya_tool_risk（level/confidence/signal），
  且绝不阻断执行（用例照常跑完）。
- AUTO 低风险(low)/非法值/ESCALATE/抛异常 → 不写注记且不破坏原流程。
"""

from __future__ import annotations

import httpx

from ah.core.orchestrator import run_single
from ah.evidence.laya_gate import STAGE_TOOL
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
        return {
            "stage": stage,
            "action": self._action,
            "value": self._value,
            "confidence": self._confidence,
            "advisory_only": self._action != "auto",
            "signal": {"raw": {"tool_risk": {"value": self._value,
                                             "confidence": self._confidence}}},
        }


def _client() -> HTTPClient:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, text='{"ok": true}')

    return HTTPClient("http://api.test",
                      client=httpx.Client(transport=httpx.MockTransport(handler)))


def _run(gate=None):  # noqa: ANN001
    case = Case(name="tool-case",
                request={"method": "DELETE", "path": "/api/orders/1"},
                assert_={"status_code": 200})
    client = _client()
    try:
        return run_single(case, Context(), client, gate=gate)
    finally:
        client.close()


# ── gate=None：埋点完全跳过 ───────────────────────────────────────────────

def test_gate_none_skips_embedding_and_keeps_original_behavior():
    """gate=None（默认）→ 不写 laya_tool_risk，且用例照常执行成功。"""
    result = _run(gate=None)
    assert result.succeeded is True
    assert "laya_tool_risk" not in result.llm_notes


def test_gate_none_never_consults_gate_backend():
    """gate=None 时连 fake gate 都不碰（calls==0），零网络。"""
    gate = _FakeGate(action="auto", value="high", confidence=0.98)
    _run(gate=None)
    assert gate.calls == 0


# ── fake gate 高置信 AUTO → 写入 laya_tool_risk（但不阻断）────────────────

def test_gate_auto_high_writes_risk_and_still_runs():
    """高置信 AUTO high → llm_notes['laya_tool_risk'] 记录 level/confidence/signal，
    且用例照常执行（advisory 不阻断）。"""
    gate = _FakeGate(action="auto", value="high", confidence=0.97)
    result = _run(gate=gate)
    assert result.succeeded is True  # 高风险也绝不阻断执行
    risk = result.llm_notes.get("laya_tool_risk")
    assert risk["level"] == "high"
    assert risk["confidence"] == 0.97
    assert risk["signal"]
    assert gate.calls == 1
    assert gate.last_stage == STAGE_TOOL


def test_gate_auto_medium_writes_risk():
    """AUTO medium 同样写注记。"""
    gate = _FakeGate(action="auto", value="medium", confidence=0.9)
    result = _run(gate=gate)
    assert result.llm_notes["laya_tool_risk"]["level"] == "medium"


# ── AUTO 低风险/非法值 → 不写注记，流程不变 ───────────────────────────────

def test_gate_auto_low_ignored():
    """AUTO low（只读/幂等，低风险）→ 不写注记。"""
    gate = _FakeGate(action="auto", value="low", confidence=0.95)
    result = _run(gate=gate)
    assert "laya_tool_risk" not in result.llm_notes
    assert result.succeeded is True


def test_gate_auto_invalid_value_ignored():
    """AUTO 但 value 非法 → 不写注记（忽略 Laya）。"""
    gate = _FakeGate(action="auto", value="bogus", confidence=0.95)
    result = _run(gate=gate)
    assert "laya_tool_risk" not in result.llm_notes
    assert result.succeeded is True


# ── 低置信 / 抛异常 → 不破坏原流程 ───────────────────────────────────────

def test_gate_escalate_writes_nothing_flow_unchanged():
    """低置信 ESCALATE → 不写注记，流程照常。"""
    gate = _FakeGate(action="escalate", value=None, confidence=0.3)
    result = _run(gate=gate)
    assert "laya_tool_risk" not in result.llm_notes
    assert result.succeeded is True


def test_gate_error_does_not_break_flow():
    """gate.best 抛异常 → 捕获并仅警告，原流程照常，不抛。"""
    gate = _FakeGate(error=RuntimeError("laya down"))
    result = _run(gate=gate)
    assert result.succeeded is True
    assert "laya_tool_risk" not in result.llm_notes
