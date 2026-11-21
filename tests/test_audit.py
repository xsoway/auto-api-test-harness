#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/15 16:50
# @Filename : test_audit.py
# @Author   : Alan_Hsu
"""``ah.quality.audit`` 单元测试：确定性动作映射、LLM 注入、失败回退。

Hermetic：LLM 用注入的假 judge 对象，绝不发起真实网络调用。
"""

from ah.harness.loader import Case
from ah.quality.audit import (
    ACTION_CLEAN,
    ACTION_DELETE,
    ACTION_OK,
    ACTION_OPTIMIZE,
    run_audit,
)


def _case(name: str = "c", method: str = "GET", path: str = "/api/items",
          json_body: dict | None = None, assert_: dict | None = None) -> Case:
    req: dict = {"method": method, "path": path}
    if json_body:
        req["json"] = json_body
    return Case(name=name, request=req, assert_=assert_ or {})


# ───────────────────────── 确定性动作映射 ─────────────────────────


def test_audit_delete_on_veto():
    """无断言一票否决 → 建议下线（delete）。"""
    report = run_audit([_case(name="noassert", assert_={})])
    assert report.total == 1
    assert report.to_delete == 1
    assert report.items[0].action == ACTION_DELETE


def test_audit_clean_on_invalid_assert():
    """仅状态码弱断言 → 清理无效断言（clean）。"""
    report = run_audit([_case(name="weak", assert_={"status": 200})])
    assert report.to_clean == 1
    assert report.items[0].action == ACTION_CLEAN


def test_audit_ok_on_healthy():
    """强断言 + 达标 → 健康（ok）。"""
    c = _case(name="create_item", path="/api/items", json_body={"x": 1},
              assert_={"status": 201, "jsonpath": {"$.id": "int"}})
    report = run_audit([c])
    assert report.healthy == 1
    assert report.items[0].action == ACTION_OK


def test_audit_optimize_on_shallow_assert():
    """单条强断言未达标 → 建议补充（optimize）。"""
    c = _case(name="shallow", assert_={"jsonpath": {"$.x": 1}})
    report = run_audit([c])
    assert report.to_optimize == 1
    assert report.items[0].action == ACTION_OPTIMIZE


# ───────────────────────── LLM 注入与回退 ─────────────────────────


class _FakeLLM:
    """本地假 LLM：judge 返回固定 advice，不联网。"""

    def __init__(self, advice: str | None = "建议断言: status: 200", fail: bool = False) -> None:
        self.advice = advice
        self.fail = fail
        self.calls = 0

    def judge(self, task: str, prompt: str):
        self.calls += 1
        if self.fail:
            raise RuntimeError("llm unavailable")
        return {"advice": self.advice}


def test_audit_llm_injects_advisory_on_clean():
    """LLM 可用 → 对 clean 用例注入优化建议并改写 suggestion。"""
    llm = _FakeLLM(advice="应加 jsonpath: $.code == 0")
    report = run_audit([_case(name="weak", assert_={"status": 200})], llm=llm)
    it = report.items[0]
    assert it.action == ACTION_CLEAN
    assert it.llm_advisory == "应加 jsonpath: $.code == 0"
    assert "LLM 建议" in it.suggestion
    assert llm.calls >= 1


def test_audit_llm_failure_falls_back_deterministic():
    """LLM 抛错 → 回退确定性建议，不中断。"""
    llm = _FakeLLM(fail=True)
    report = run_audit([_case(name="shallow", assert_={"jsonpath": {"$.x": 1}})],
                       llm=llm)
    it = report.items[0]
    assert it.action == ACTION_OPTIMIZE
    assert it.llm_advisory is None
    assert "LLM 建议" not in it.suggestion


def test_audit_no_llm_keeps_deterministic():
    """未传 llm → 纯确定性，llm_advisory 恒 None。"""
    report = run_audit([_case(name="noassert", assert_={})])
    assert report.items[0].llm_advisory is None
    assert report.items[0].suggestion.startswith("一票否决")


def test_audit_mixed_suite_counts():
    """混合用例库：delete + clean + ok 计数正确。"""
    cases = [
        _case(name="noassert", assert_={}),                           # delete
        _case(name="weak", assert_={"status": 200}),                  # clean
        _case(name="good", path="/api/items", json_body={"x": 1},
              assert_={"status": 201, "jsonpath": {"$.id": "int"}}),  # ok
    ]
    report = run_audit(cases)
    assert report.total == 3
    assert report.to_delete == 1
    assert report.to_clean == 1
    assert report.healthy == 1
    assert report.to_optimize == 0
