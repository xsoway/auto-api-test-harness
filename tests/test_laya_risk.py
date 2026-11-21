"""接口风险分级 LayaGate 前置打分埋点 hermetic 验证。

只测 `risk_profiles` 的行为：gate=None（默认/off）→ 走原纯算术 risk，零 Laya
调用；gate 非 None 且高置信 AUTO 给出合法 0-1 数值 → 覆盖算术 risk 并写入
``laya_signal``；AUTO 非法值（越界/字符串）/未开闸/异常 → 回退原算术 risk、
不抛。全部使用 fake gate，零网络、零真模型。
"""

from __future__ import annotations

import pytest

from ah.predict.risk import risk_profiles


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


_AUTO_HIGH = {
    "action": "auto",
    "value": 2.0,  # 0-based 期望分：3 个分级带中最高档（2/2=1.0）
    "confidence": 0.95,
    "signal": {"risk_score": {"value": 2.0}},
}


def _rows():
    """一个低算术风险接口：2 样本 1 断言失败 → 算术 risk=0.5。"""
    return [
        {"interface": "GET /api/login", "result": "PASS"},
        {"interface": "GET /api/login", "result": "FAIL", "failure_class": "assertion"},
    ]


def _arith_risk(rows):
    """gate=None 时的算术 risk（与 gate 完全无关的基线）。"""
    return risk_profiles(rows, gate=None)[0].risk


# ── gate=None：走原算术 risk，零 Laya 调用 ─────────────────────────────────

def test_gate_none_matches_plain_arith_path():
    gate = _FakeGate(_AUTO_HIGH)  # 即使 gate 会裁 high，也不应被调用（gate 根本没传）
    with_gate_none = risk_profiles(_rows(), gate=None)
    assert with_gate_none == risk_profiles(_rows())  # 与原签名字节一致
    assert gate.calls == 0  # gate 未传入 → 零网络零调用


def test_gate_none_laya_signal_empty():
    p = risk_profiles(_rows(), gate=None)[0]
    assert p.laya_signal == {}


# ── fake gate AUTO 高风险值：覆盖原 risk 且写入 signal ─────────────────────

def test_auto_high_overrides_risk_and_writes_signal():
    gate = _FakeGate(_AUTO_HIGH)
    profiles = risk_profiles(_rows(), gate=gate)
    assert gate.calls == 1
    p = profiles[0]
    assert p.risk == 1.0  # 2.0/2 归一化 = 1.0，覆盖算术 risk 0.5
    assert p.laya_signal == _AUTO_HIGH["signal"]


def test_auto_midband_normalizes_to_0_5():
    """0-based 中档 1.0（3 带）→ 归一化 0.5。"""
    gate = _FakeGate({"action": "auto", "value": 1.0, "confidence": 0.95})
    p = risk_profiles(_rows(), gate=gate)[0]
    assert p.risk == 0.5  # 1.0/2


def test_auto_zero_valid_value():
    """合法 0 值也应被采用（无失败接口可从 0 加深置信）。"""
    gate = _FakeGate({"action": "auto", "value": 0.0, "confidence": 0.95})
    p = risk_profiles(_rows(), gate=gate)[0]
    assert p.risk == 0.0
    assert p.laya_signal == {}


# ── fake gate AUTO 非法值 / 未开闸 / 异常 → 回退原算术 risk，不抛 ──────────

@pytest.mark.parametrize(
    "decision",
    [
        {"action": "auto", "value": 3.0, "confidence": 0.95},      # 归一化后越界（1.5>1）
        {"action": "auto", "value": -0.5, "confidence": 0.95},     # 负值
        {"action": "auto", "value": "high", "confidence": 0.95},   # 字符串
        {"action": "auto", "value": None, "confidence": 0.95},     # 空值
        {"action": "escalate", "value": 2.0, "confidence": 0.3},   # 未开闸/低置信
        {"action": "human", "value": 2.0, "confidence": 0.5},      # 非 auto
    ],
)
def test_non_auto_or_invalid_value_falls_back_to_arith(decision):
    gate = _FakeGate(decision)
    p = risk_profiles(_rows(), gate=gate)[0]
    assert gate.calls == 1
    assert p.risk == _arith_risk(_rows())  # 回退算术 risk
    assert p.laya_signal == {}


def test_gate_exception_falls_back_and_does_not_raise():
    gate = _FakeGate(error=RuntimeError("laya down"))
    profiles = risk_profiles(_rows(), gate=gate)
    assert gate.calls == 1
    assert profiles[0].risk == _arith_risk(_rows())
    assert profiles[0].laya_signal == {}
