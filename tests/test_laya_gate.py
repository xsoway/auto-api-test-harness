"""LayaGate 前置决策层 hermetic 验证（AH_LAYA 控制，零网络、零真模型）。"""

from __future__ import annotations

import pytest

from ah.evidence.laya_gate import (
    STAGE_ASSERT,
    STAGE_INTENT,
    Action,
    LayaDecision,
    LayaGate,
)


class _FakeBackend:
    """可注入的 fake 后端：返回预置 verdict 或抛异常。"""

    def __init__(self, verdicts: dict | None = None, error: Exception | None = None) -> None:
        self.verdicts = verdicts or {}
        self.error = error
        self.calls = 0

    def grade(self, state: str, questions):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.verdicts


_HIGH = {"intent": {"value": "gen_case", "confidence": 0.95, "probabilities": {"gen_case": 0.95}}}
_LOW = {"intent": {"value": "gen_case", "confidence": 0.30, "probabilities": {"gen_case": 0.30}}}


# ── 默认关：不触发后端（Hermetic） ─────────────────────────────────────────

def test_default_off_escalates_without_backend() -> None:
    backend = _FakeBackend(_HIGH)
    gate = LayaGate(backend=backend)  # AH_LAYA 未设 → 默认关
    dec = gate.decide(STAGE_INTENT, "state", {"intent": {"type": "choice"}})
    assert dec.action is Action.ESCALATE
    assert dec.advisory_only is True
    assert backend.calls == 0  # 未触发 subprocess


def test_explicit_disabled_stage_escalates() -> None:
    backend = _FakeBackend(_HIGH)
    gate = LayaGate(backend=backend, enabled={STAGE_ASSERT})  # 只开 assert
    dec = gate.decide(STAGE_INTENT, "state", {})
    assert dec.action is Action.ESCALATE
    assert backend.calls == 0


# ── 开闸 + 高置信 → AUTO ───────────────────────────────────────────────────

def test_enabled_high_confidence_auto() -> None:
    backend = _FakeBackend(_HIGH)
    gate = LayaGate(backend=backend, enabled={STAGE_INTENT})
    dec = gate.decide(STAGE_INTENT, "state", {"intent": {"type": "choice"}})
    assert dec.action is Action.AUTO
    assert dec.value == {"intent": "gen_case"}
    assert dec.confidence == pytest.approx(0.95)
    assert backend.calls == 1


def test_enabled_low_confidence_escalates_with_signal() -> None:
    backend = _FakeBackend(_LOW)
    gate = LayaGate(backend=backend, enabled={STAGE_INTENT})
    dec = gate.decide(STAGE_INTENT, "state", {"intent": {"type": "choice"}})
    assert dec.action is Action.ESCALATE
    assert dec.signal["intent"]["value"] == "gen_case"  # 信号注记仍在
    assert backend.calls == 1


def test_backend_error_safe_escalate() -> None:
    backend = _FakeBackend(error=RuntimeError("boom"))
    gate = LayaGate(backend=backend, enabled={STAGE_INTENT})
    dec = gate.decide(STAGE_INTENT, "state", {"intent": {"type": "choice"}})
    assert dec.action is Action.ESCALATE  # Laya 故障绝不破坏原流程


# ── 阈值行为 ───────────────────────────────────────────────────────────────

def test_threshold_overridden() -> None:
    backend = _FakeBackend(_LOW)  # conf=0.30
    gate = LayaGate(backend=backend, enabled={STAGE_INTENT}, thresholds={STAGE_INTENT: 0.2})
    assert gate.decide(STAGE_INTENT, "s", {}).action is Action.AUTO  # 0.30>=0.2


def test_high_threshold_rejects_mid_confidence() -> None:
    backend = _FakeBackend(_LOW)  # conf=0.30
    gate = LayaGate(backend=backend, enabled={STAGE_INTENT}, thresholds={STAGE_INTENT: 0.9})
    assert gate.decide(STAGE_INTENT, "s", {}).action is Action.ESCALATE


# ── 环境变量开关 ───────────────────────────────────────────────────────────

def test_env_off_respects_env(monkeypatch) -> None:
    backend = _FakeBackend(_HIGH)
    monkeypatch.setenv("AH_LAYA", "off")
    gate = LayaGate(backend=backend, enabled=None)
    assert gate.stage_enabled(STAGE_INTENT) is False


def test_env_intent_enables_stage(monkeypatch) -> None:
    backend = _FakeBackend(_HIGH)
    monkeypatch.setenv("AH_LAYA", "intent")
    gate = LayaGate(backend=backend, enabled=None)
    assert gate.stage_enabled(STAGE_INTENT) is True
    assert gate.stage_enabled(STAGE_ASSERT) is False


def test_env_all_enables_enabled_defaults(monkeypatch) -> None:
    backend = _FakeBackend(_HIGH)
    monkeypatch.setenv("AH_LAYA", "all")
    gate = LayaGate(backend=backend, enabled=None)
    assert gate.stage_enabled(STAGE_INTENT) is True  # intent 在默认可开集合


# ── 便捷入口 ───────────────────────────────────────────────────────────────

def test_best_returns_serializable_dict() -> None:
    backend = _FakeBackend(_HIGH)
    gate = LayaGate(backend=backend, enabled={STAGE_INTENT})
    out = gate.best(STAGE_INTENT, "state", {"intent": {"type": "choice"}})
    assert out["action"] == "auto"
    assert out["value"] == {"intent": "gen_case"}
    assert out["confidence"] == pytest.approx(0.95)
    assert isinstance(out["signal"], dict)


# ── LayaDecision 结构 ──────────────────────────────────────────────────────

def test_decision_auto_property() -> None:
    auto = LayaDecision(stage=STAGE_INTENT, action=Action.AUTO, value="x", confidence=0.9)
    esc = LayaDecision(stage=STAGE_INTENT, action=Action.ESCALATE)
    assert auto.auto is True
    assert esc.auto is False
