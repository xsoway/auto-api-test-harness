#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/10
# @Filename : test_laya_report.py
# @Author   : Alan_Hsu
"""报告失败严重度分级 LayaGate 前置分流埋点 hermetic 验证。

聚焦 `severity_classify`：gate=None（默认/off）→ 走确定性规则（原规则 severity
不变）、零 Laya 调用；gate 非 None 且高置信 AUTO value∈{high,medium,low} →
直接采用裁决（signal=True）；未开闸/低置信/非法值/异常 → 回退确定性规则
（signal=False，不抛）。全部使用 fake gate，零网络、零真模型。
"""

from __future__ import annotations

import pytest

from ah.reporting.writer import (
    ResultRecord,
    _capability_matrix,
    _rule_severity,
    build_html_report,
    severity_classify,
)


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


_AUTO_HIGH = {"action": "auto", "value": "high", "confidence": 0.95}
_AUTO_MEDIUM = {"action": "auto", "value": "medium", "confidence": 0.95}
_AUTO_LOW = {"action": "auto", "value": "low", "confidence": 0.95}


# ── gate=None：走原确定性规则，零 Laya 调用 ───────────────────────────────

def test_gate_none_returns_rule_severity_zero_laya_calls():
    gate = _FakeGate(_AUTO_HIGH)  # 即使 gate 会裁 high，也不应被调用（gate 根本没传）
    assert severity_classify("{status_code} 500", "GET", "/api", gate=None) == ("high", False)
    assert gate.calls == 0  # gate 未传入 → 零网络零调用


def test_gate_none_medium_assert():
    assert severity_classify("expected 200, got 400", "GET", "/api") == ("medium", False)


def test_rule_severity_write_method_high():
    assert _rule_severity("assert fail", "POST", "/users") == "high"


def test_rule_severity_empty_low():
    assert _rule_severity("", "GET", "/health") == "low"


def test_rule_severity_status_code_high():
    assert _rule_severity("{status_code} 503", "GET", "/api") == "high"


# ── fake gate AUTO 合法 value → 覆盖确定性规则的严重度 ─────────────────────

def test_auto_high_overrides_medium_rule():
    gate = _FakeGate(_AUTO_HIGH)
    # 确定性规则本判 medium（普通断言），Laya 高置信高 → 采用 high。
    sev, signal = severity_classify("expected 200, got 400", "GET", "/api", gate=gate)
    assert sev == "high"
    assert signal is True
    assert gate.calls == 1


def test_auto_low_overrides_high_rule():
    gate = _FakeGate(_AUTO_LOW)
    # 破坏性方法本判 high，Laya 高置信低 → 采用 low。
    sev, signal = severity_classify("boom", "DELETE", "/users", gate=gate)
    assert sev == "low"
    assert signal is True
    assert gate.calls == 1


def test_auto_medium_same_as_rule():
    gate = _FakeGate(_AUTO_MEDIUM)
    sev, signal = severity_classify("expected 200, got 400", "GET", "/api", gate=gate)
    assert sev == "medium"
    assert signal is True
    assert gate.calls == 1


# ── 低置信 / 未开闸 / 非法值 / 异常 → 回退确定性规则，不抛 ─────────────────

@pytest.mark.parametrize(
    "decision",
    [
        {"action": "escalate", "value": None, "confidence": 0.4},  # 低置信/未开闸
        {"action": "auto", "value": "unknown", "confidence": 0.95},  # 非法值
        {"action": "auto", "value": "other", "confidence": 0.95},  # other 逃生口
        {"action": "noul", "value": "high"},  # 非 auto 动作
    ],
)
def test_non_auto_or_invalid_falls_back_to_rule(decision):
    gate = _FakeGate(decision)
    sev, signal = severity_classify("expected 200, got 400", "GET", "/api", gate=gate)
    assert sev == "medium"  # 回退到确定性规则 → 中等
    assert signal is False
    assert gate.calls == 1


def test_gate_exception_falls_back_and_does_not_raise():
    gate = _FakeGate(error=RuntimeError("laya down"))
    sev, signal = severity_classify("boom", "POST", "/users", gate=gate)
    assert sev == "high"  # 回退到确定性规则 → 高（写方法）
    assert signal is False
    assert gate.calls == 1


# ── 报告 HTML 渲染：决策模型前置层段落 / 能力矩阵 / severity 注入 ──────────

def _rec(name: str, msg: str | None) -> ResultRecord:
    rec = ResultRecord(
        name=name,
        result="FAIL" if msg else "PASS",
        failure_msg=msg,
    )
    rec.request = {"method": "GET", "path": f"/api/{name}"}
    return rec


def _report_html(tmp_path, results, *, gate=None) -> str:
    return build_html_report(
        results,
        report_dir=str(tmp_path),
        basename="r",
        gate=gate,
    ).read_text(encoding="utf-8")


def test_report_with_laya_signal_renders_decision_section(tmp_path):
    """结果含 Laya 信号注记 → 报告渲染「决策模型前置层」区块 + 能力行 23。"""
    rec = _rec("a", "expected 200 got 500")
    rec.llm_notes["laya_triage"] = {"action": "auto", "value": "defect"}
    html = _report_html(tmp_path, [rec])
    assert 'id="laya-gate"' in html        # 决策模型前置层区块
    assert "决策模型 Laya" in html          # hero 指标 chip
    assert "决策模型前置层 Laya" in html    # 能力矩阵行 23
    assert "laya_triage" in html            # 具体信号环节


def test_report_none_no_laya_section_and_row_unused(tmp_path):
    """无 Laya 信号 + gate=None → 不渲染决策模型区块、能力行 23 未被误标。"""
    html = _report_html(tmp_path, [_rec("a", "expected 200 got 500")])
    assert 'id="laya-gate"' not in html
    assert "决策模型 Laya" not in html
    cap = _capability_matrix(None, None, results=[_rec("a", "expected 200 got 500")])
    row23 = next(r for r in cap["rows"] if r["id"] == 23)
    assert row23["used"] is False


def test_report_gate_adopted_passes_to_severity_and_renders(tmp_path):
    """注入 gate 且高置信 AUTO high → 严重度采纳；报告区块计数存在。"""
    gate = _FakeGate(_AUTO_HIGH)
    recs = [_rec(f"c{i}", f"expected 200 got 5{i}") for i in range(2)]
    html = _report_html(tmp_path, recs, gate=gate)
    assert "严重度采纳" in html
    assert "决策模型前置层 Laya" in html
    assert gate.calls == 2  # 每个失败用例各咨询一次 STAGE_REPORT
