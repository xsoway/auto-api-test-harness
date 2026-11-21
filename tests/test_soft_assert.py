#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2026-10-09
# @Filename: test_soft_assert.py
# @Author  : Alan_Hsu
"""⑥ 软断言 soft_assert 验证：断言失败可选「只记录不判失败」。

覆盖：assert_response(soft=True) 失败不抛、升 collected；默认(soft=False)照旧抛；
jsonpath 软断言；loader 解析 soft_assert；run_single 软模式 succeeded=True 且记录
soft_failures。全部 hermetic（MockTransport、LLMOFF=1）。
"""

from __future__ import annotations

import httpx
import pytest

from ah.core.orchestrator import run_single
from ah.harness.http import HTTPClient
from ah.harness.loader import Case, load_cases
from ah.metrics.assertions import assert_response
from ah.utils.context import Context


@pytest.fixture(autouse=True)
def _llm_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制关闭 LLM：确保零网络依赖（hermetic）。"""
    monkeypatch.setenv("LLMOFF", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def _resp(status: int, payload: dict) -> HTTPClient:
    raw = httpx.Client(
        transport=httpx.MockTransport(lambda req, s=status, p=payload: httpx.Response(s, json=p))
    )
    return HTTPClient("http://api.test", client=raw, timeout=5.0)


# ───────────────────────── assert_response 软/硬 ─────────────────────────


def test_soft_status_code_failure_records_but_does_not_raise():
    """soft=True：状态码断言失败不抛，collected 记录 passed=False。"""
    client = _resp(404, {})
    collected: list[dict] = []
    assert_response({"status_code": 200}, client.request("GET", "/"), Context(),
                    collected=collected, soft=True)
    assert any(a["type"] == "status_code" and a["passed"] is False
               for a in collected)


def test_hard_status_code_failure_still_raises():
    """默认 hard：状态码断言失败照旧抛 AssertionError。"""
    client = _resp(404, {})
    with pytest.raises(AssertionError):
        assert_response({"status_code": 200}, client.request("GET", "/"), Context())


def test_soft_jsonpath_failure_does_not_raise():
    """soft=True：jsonpath.eq 失败只记录不抛。"""
    client = _resp(200, {"a": 1})
    collected: list[dict] = []
    assert_response({"jsonpath": {"a": {"eq": 2}}}, client.request("GET", "/"), Context(),
                    collected=collected, soft=True)
    assert any(a["passed"] is False for a in collected)


def test_soft_contains_failure_does_not_raise():
    """soft=True：contains 失败只记录不抛。"""
    client = _resp(200, {"x": "y"})
    collected: list[dict] = []
    assert_response({"contains": "NEVER-IN-BODY"}, client.request("GET", "/"), Context(),
                    collected=collected, soft=True)
    assert any(a["logic"] == "substring" and a["passed"] is False for a in collected)


# ───────────────────────── loader 解析 ─────────────────────────


def test_loader_parses_soft_assert(tmp_path):
    """YAML soft_assert: true → case.soft_assert is True。"""
    f = tmp_path / "c.yaml"
    f.write_text(
        "name: soft-ok\n"
        "request:\n  method: GET\n  path: /x\n"
        "soft_assert: true\n"
        "assert:\n  status_code: 200\n",
        encoding="utf-8",
    )
    case = load_cases(f)[0]
    assert case.soft_assert is True


def test_loader_defaults_soft_assert_false(tmp_path):
    """缺省软断言 → case.soft_assert is False（保持硬断言默认）。"""
    f = tmp_path / "c.yaml"
    f.write_text(
        "name: hard\n"
        "request:\n  method: GET\n  path: /x\n"
        "assert:\n  status_code: 200\n",
        encoding="utf-8",
    )
    assert load_cases(f)[0].soft_assert is False


# ───────────────────────── run_single 软/硬判定 ─────────────────────────


def test_run_single_soft_failure_stays_succeeded():
    """软断言用例：断言语义失败仍 succeeded=True 并记录 soft_failures。"""
    client = _resp(500, {})
    case = Case(
        name="soft-500",
        request={"method": "GET", "path": "/"},
        assert_={"status_code": 200},
        soft_assert=True,
    )
    res = run_single(case, Context(), client)
    assert res.succeeded is True
    assert res.llm_notes.get("soft_failures") == 1
    assert res.error is None


def test_run_single_hard_failure_stays_failed():
    """硬断言用例：相同失败场景照旧判失败。"""
    client = _resp(500, {})
    case = Case(
        name="hard-500",
        request={"method": "GET", "path": "/"},
        assert_={"status_code": 200},
        soft_assert=False,
    )
    res = run_single(case, Context(), client)
    assert res.succeeded is False
    assert res.error is not None

# ── assert_llm + LayaGate 断言埋点（hermetic fake gate） ───────────────────

def _gate_stub(action="escalate", satisfies=None, confidence=0.0):
    class _Stub:
        def __init__(self):
            self.calls = 0
        def decide(self, stage, state, questions):
            self.calls += 1
            return type(
                "Dec", (),
                {"action": type("A", (), {"value": action})(), "confidence": confidence,
                 "value": {"satisfies": satisfies}},
            )()
    return _Stub()


def test_assert_llm_no_gate_keeps_semantic_flow():
    """gate=None（默认）→ 原语义判定字节等价：不引入 Laya 分流。"""
    from ah.metrics.assertions import assert_llm
    assert assert_llm("需含北京", "响应：北京门店", llm=None)["verdict"] == "SKIP"


def test_assert_llm_gate_auto_pass_shortcuts():
    from ah.metrics.assertions import assert_llm
    gate = _gate_stub(action="auto", satisfies=0.98)
    res = assert_llm("需含北京", "北京门店列表", llm=None, gate=gate)
    assert res["verdict"] == "PASS"
    assert res.get("pass") is True
    assert gate.calls == 1


def test_assert_llm_gate_auto_fail_returns_unverified():
    from ah.metrics.assertions import assert_llm
    gate = _gate_stub(action="auto", satisfies=0.1)
    res = assert_llm("需含北京", "加载失败", llm=None, gate=gate)
    assert res["verdict"] == "UNVERIFIED"
    assert res.get("pass") is False
    assert res.get("flagged") is True


def test_assert_llm_gate_escalate_goes_to_llm():
    """低置信升级 → 走原 LLM 语义判定。"""
    from ah.llm.mock import MockLLM
    from ah.metrics.assertions import assert_llm
    mock = MockLLM()
    mock.set_json_response({"pass": True, "reason": "含北京"})
    gate = _gate_stub(action="escalate", satisfies=0.4)
    res = assert_llm("需含北京", "北京门店列表", llm=mock, gate=gate)
    assert res["verdict"] == "PASS"


def test_assert_llm_gate_backend_error_falls_back():
    """gate 抛错 → 不进 Laya 分流，走原流程（llm None → SKIP）。"""
    from ah.metrics.assertions import assert_llm
    class _Boom:
        def decide(self, *a, **k):
            raise RuntimeError("boom")
    res = assert_llm("需含北京", "北京门店", llm=None, gate=_Boom())
    assert res["verdict"] == "SKIP"  # 不破坏原流程
