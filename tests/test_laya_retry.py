"""重试-回退决策 LayaGate 前置分流埋点 hermetic 验证。

_only_ `_is_retryable_error` 的行为：gate=None（默认/off）→ 走原确定性判定、
零 Laya 调用；gate 非 None 且高置信 AUTO → 直接采用裁决；低置信/未开闸/异常 →
回退原确定性判定。全部使用 fake gate，零网络、零真模型。
"""

from __future__ import annotations

import httpx
import pytest

from ah.core.orchestrator import _is_retryable_error


class _FakeGate:
    """可注入的 fake gate：返回预置 best() dict 或抛异常，并计数调用。"""

    def __init__(self, decision: dict | None = None, error: Exception | None = None) -> None:
        self.decision = decision
        self.error = error
        self.calls = 0

    def best(self, stage, state, questions):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.decision or {})


_AUTO_YES = {"action": "auto", "value": "yes", "confidence": 0.95}
_AUTO_NO = {"action": "auto", "value": "no", "confidence": 0.95}


def _resp(status: int):
    return httpx.Response(status_code=status)


# ── gate=None：走原确定性判定，零 Laya 调用 ─────────────────────────────────

def test_gate_none_5xx_retryable_zero_laya_calls():
    gate = _FakeGate(_AUTO_NO)  # 即使 gate 会裁 no，也不应被调用（gate 根本没传）
    assert _is_retryable_error(RuntimeError("boom"), _resp(503)) is True
    assert gate.calls == 0  # gate 未传入 → 零网络零调用


def test_gate_none_transport_retryable():
    assert _is_retryable_error(httpx.TimeoutException("timeout"), None) is True


def test_gate_none_4xx_not_retryable():
    assert _is_retryable_error(RuntimeError("assert"), _resp(400)) is False


# ── fake gate AUTO "yes"：即使确定性判定为 False，也返回 True ──────────────

def test_auto_yes_overrides_4xx_false():
    gate = _FakeGate(_AUTO_YES)
    assert _is_retryable_error(RuntimeError("assert mismatch"), _resp(400), gate=gate) is True
    assert gate.calls == 1


def test_auto_yes_on_5xx_true():
    gate = _FakeGate(_AUTO_YES)
    assert _is_retryable_error(RuntimeError("boom"), _resp(500), gate=gate) is True
    assert gate.calls == 1


# ── fake gate AUTO "no"：即使确定性判定为 True，也返回 False ───────────────

def test_auto_no_overrides_5xx_true():
    gate = _FakeGate(_AUTO_NO)
    assert _is_retryable_error(RuntimeError("boom"), _resp(500), gate=gate) is False
    assert gate.calls == 1


def test_auto_no_on_4xx_false():
    gate = _FakeGate(_AUTO_NO)
    assert _is_retryable_error(RuntimeError("assert"), _resp(400), gate=gate) is False
    assert gate.calls == 1


# ── 低置信 / 未开闸 / 异常 → 回退原确定性判定，不抛 ────────────────────────

@pytest.mark.parametrize(
    "decision",
    [
        {"action": "escalate", "value": {"retryable": "yes"}, "confidence": 0.3},  # 低置信升级
        {"action": "escalate", "value": None, "confidence": 0.0},  # 未开闸
        {"action": "auto", "value": "maybe", "confidence": 0.9},  # value 不在 yes/no
    ],
)
def test_non_auto_falls_back_to_deterministic(decision):
    gate = _FakeGate(decision)
    # 5xx 确定性 True，尽管 Laya 未裁决 → 仍 True（回退）
    assert _is_retryable_error(RuntimeError("boom"), _resp(500), gate=gate) is True
    assert gate.calls == 1


def test_gate_exception_falls_back_and_does_not_raise():
    gate = _FakeGate(error=RuntimeError("laya down"))
    assert _is_retryable_error(RuntimeError("boom"), _resp(500), gate=gate) is True
    assert _is_retryable_error(RuntimeError("assert"), _resp(400), gate=gate) is False
    assert gate.calls == 2


def test_answer_other_falls_back():
    gate = _FakeGate({"action": "auto", "value": "other", "confidence": 0.95})
    assert _is_retryable_error(RuntimeError("boom"), _resp(503), gate=gate) is True
    assert gate.calls == 1
