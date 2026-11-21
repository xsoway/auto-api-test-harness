"""演进停止条件 LayaGate 前置分流埋点 hermetic 验证。

只测 :meth:`AutoEvolutionLoop.stop_reason` 的行为：gate=None（默认/off）→ 走原
确定性停车判定、Laya 零调用；gate 非 None 且高置信 AUTO 判停 → 当原判定为“继续”
时注入提前停；AUTO 判继续 / 非法值 / 低置信（ESCALATE）/ 异常 → 回退原判定。
关键约束：Laya 只能“提前停”，绝不能“延后停”——原判定已停时 Laya 的继续意见被忽略。
全部使用 fake gate，零网络、零真模型。
"""

from __future__ import annotations

import pytest

from ah.evolution import AutoEvolutionLoop, RoundOutcome


class _FakeGate:
    """可注入的 fake gate：返回预置 best() dict 或抛异常，并计数调用。"""

    def __init__(self, decision: dict | None = None, error: Exception | None = None) -> None:
        self.decision = decision
        self.error = error
        self.calls = 0
        self.seen_state = None
        self.seen_stage = None

    def best(self, stage, state, questions):
        self.calls += 1
        self.seen_stage = stage
        self.seen_state = state
        if self.error is not None:
            raise self.error
        return dict(self.decision or {})


# ── 构造一个原确定性判定为“继续”的 loop ──────────────────────────────────
def _continuing_loop(tmp_path) -> AutoEvolutionLoop:
    proj = tmp_path / "proj"
    data = proj / "data"
    data.mkdir(parents=True)
    # round_no=1 < rounds；_total_adopted=0 < budget；无历史轮；_last_green=0
    loop = AutoEvolutionLoop(data, proj, rounds=5, budget=10, run_suite=False)
    return loop


# ── gate=None：走原确定性判定，Laya 零调用，字节等价 ───────────────────────

def test_gate_none_continue_returns_none_zero_laya_calls(tmp_path):
    loop = _continuing_loop(tmp_path)
    gate = _FakeGate({"action": "auto", "value": "no", "confidence": 0.95})
    loop._gate = None
    assert loop.stop_reason(1) is None  # 原判定继续 → 保持 None
    assert gate.calls == 0  # gate 未传入（None）→ 零 Laya 调用


def test_gate_none_still_stops_on_budget(tmp_path):
    loop = _continuing_loop(tmp_path)
    loop._total_adopted = 10  # 达到 budget=10
    assert loop.stop_reason(1) == "budget(10) exhausted"  # 与既有确定性逻辑一致


def test_gate_none_still_stops_on_max_rounds(tmp_path):
    loop = _continuing_loop(tmp_path)
    assert loop.stop_reason(5) == "max_rounds(5) reached"


def test_gate_none_still_stops_on_no_new(tmp_path):
    loop = _continuing_loop(tmp_path)
    loop.report.rounds.append(RoundOutcome(round_no=1, persisted=0, dedup_skipped=0))
    assert loop.stop_reason(2) == "no_new_candidate_adopted"


def test_gate_none_all_green_two_rounds(tmp_path):
    loop = _continuing_loop(tmp_path)
    loop._last_green = 2
    assert loop.stop_reason(1) == "all_green_two_rounds_unchanged"


# ── fake gate AUTO "no" + 原判定继续 → 注入提前停 ──────────────────────────

def test_auto_no_early_stops_when_continuing(tmp_path):
    loop = _continuing_loop(tmp_path)
    gate = _FakeGate({"action": "auto", "value": "no", "confidence": 0.95})
    loop._gate = gate
    assert loop.stop_reason(1) == "laya_no_more_value"
    assert gate.calls == 1
    assert gate.seen_stage == "stop"
    # state 语义丰富：含轮数/已采纳/预算/最近失败数
    assert gate.seen_state["round"] == 1
    assert gate.seen_state["adopted"] == 0
    assert gate.seen_state["budget"] == 10


def test_auto_false_bool_stops(tmp_path):
    loop = _continuing_loop(tmp_path)
    gate = _FakeGate({"action": "auto", "value": False, "confidence": 0.95})
    loop._gate = gate
    assert loop.stop_reason(1) == "laya_no_more_value"
    assert gate.calls == 1


# ── fake gate AUTO "yes" / True + 原判定继续 → 保持继续（None）──────────────

def test_auto_yes_keeps_going_when_continuing(tmp_path):
    loop = _continuing_loop(tmp_path)
    gate = _FakeGate({"action": "auto", "value": "yes", "confidence": 0.95})
    loop._gate = gate
    assert loop.stop_reason(1) is None
    assert gate.calls == 1


def test_auto_true_bool_keeps_going(tmp_path):
    loop = _continuing_loop(tmp_path)
    gate = _FakeGate({"action": "auto", "value": True, "confidence": 0.95})
    loop._gate = gate
    assert loop.stop_reason(1) is None
    assert gate.calls == 1


# ── 低置信（ESCALATE）/ 非 AUTO / 非法值 → 回退原判定不抛 ──────────────────

@pytest.mark.parametrize(
    "decision",
    [
        {"action": "escalate", "value": None, "confidence": 0.3},
        {"action": "manual", "value": "no", "confidence": 0.5},
        {"action": "auto", "value": "other", "confidence": 0.95},
        {"action": "auto", "value": "maybe", "confidence": 0.95},
        {"action": "auto", "value": None, "confidence": 0.95},
        {},
    ],
)
def test_non_stop_or_invalid_decision_falls_back(decision, tmp_path):
    loop = _continuing_loop(tmp_path)
    gate = _FakeGate(decision)
    loop._gate = gate
    assert loop.stop_reason(1) is None  # 回退原判定“继续”
    assert gate.calls == 1


def test_gate_exception_falls_back_and_does_not_raise(tmp_path):
    loop = _continuing_loop(tmp_path)
    gate = _FakeGate(error=RuntimeError("laya down"))
    loop._gate = gate
    assert loop.stop_reason(1) is None  # 异常 → 回退继续，不抛
    assert gate.calls == 1


# ── 关键约束：原判定已停时 Laya 的“继续”意见被忽略（绝不延后停）─────────────

def test_auto_yes_never_defers_a_deterministic_stop(tmp_path):
    loop = _continuing_loop(tmp_path)
    loop._total_adopted = 10  # 原判定 budget 已停
    gate = _FakeGate({"action": "auto", "value": "yes", "confidence": 0.95})
    loop._gate = gate
    assert loop.stop_reason(1) == "budget(10) exhausted"  # Laya 的继续意见被忽略
    assert gate.calls == 0  # 原判定已停 → 不再询问 Laya
