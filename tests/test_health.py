#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/15 16:30
# @Filename : test_health.py
# @Author   : Alan_Hsu
"""``ah.quality.health`` 单元测试：评分权重、一票否决、断言密度、无效断言、聚合。

Hermetic：不依赖真实 LLM / 网络，全部用本地 :class:`Case` 构造。
"""


from ah.harness.loader import Case
from ah.quality.health import (
    DEFAULT_MIN_QUALITY,
    HealthContext,
    HealthReport,
    aggregate_health,
    assert_density,
    assess_case,
    assess_suite,
    list_asserts,
    quality_score,
)


def _case(
    name: str = "c",
    method: str = "GET",
    path: str = "/api/items",
    params: dict | None = None,
    json_body: dict | None = None,
    assert_: dict | None = None,
    priority: str = "P1",
) -> Case:
    req: dict = {"method": method, "path": path}
    if params:
        req["params"] = params
    if json_body:
        req["json"] = json_body
    return Case(name=name, request=req, assert_=assert_ or {}, priority=priority)


# ───────────────────────── 评分权重 ─────────────────────────


def test_quality_score_body_assert_is_max():
    """含 body 强断言的用例得分最高（断言 50 + 参数 30 + 命名 20 = 100）。"""
    c = _case(name="create_item", path="/api/items", json_body={"x": 1},
              assert_={"status": 201, "jsonpath": {"$.id": "int"}})
    assert quality_score(c) == 100.0


def test_quality_score_no_assert_is_low():
    """无断言：断言 0 分，其余权重保留。"""
    c = _case(name="create_item", path="/api/items", json_body={"x": 1})
    # 0（无断言）+ 30（参数）+ 20（命名）= 50
    assert quality_score(c) == 50.0


def test_quality_score_only_status_is_weak():
    """仅有状态码断言：断言只拿 25 + 10（弱，非强断言）。"""
    c = _case(name="get_item", path="/api/items", assert_={"status": 200})
    # 25（status）+ 10（无 body）+ 20（命名）= 55
    assert quality_score(c) == 55.0


def test_assert_density_counts_strong_asserts():
    """断言密度：status 计 1，jsonpath 每条路径计 1。"""
    c = _case(assert_={"status": 200, "jsonpath": {"$.a": 1, "$.b": 2}})
    assert assert_density(c) == 3


def test_list_asserts_returns_items():
    """断言清单返回可读条目。"""
    c = _case(assert_={"status": 200, "equals": {"path": "$.x", "value": 1}})
    out = list_asserts(c)
    kinds = {i["kind"] for i in out}
    assert "status" in kinds
    assert "equals" in kinds


# ───────────────────────── 一票否决 ─────────────────────────


def test_assess_case_veto_no_assert():
    h = assess_case(_case(assert_={}))
    assert not h.approved
    assert h.veto == "no_assert"
    assert h.score == 0.0


def test_assess_case_veto_retired_interface():
    ctx = HealthContext(retired_paths={("GET", "/api/items")})
    h = assess_case(_case(method="GET", path="/api/items", assert_={"status": 200}), ctx)
    assert h.veto == "retired_interface"
    assert not h.approved


def test_assess_case_veto_stale_unmaintained():
    ctx = HealthContext(stale_names={"c"})
    h = assess_case(_case(assert_={"status": 200}), ctx)
    assert h.veto == "stale_unmaintained"


def test_assess_case_veto_broken_script():
    ctx = HealthContext(broken_names={"c"})
    h = assess_case(_case(assert_={"status": 200}), ctx)
    assert h.veto == "broken_script"


def test_assess_case_healthy_meets_gate():
    """强断言用例满足 70 分门禁。"""
    c = _case(name="create_item", path="/api/items", json_body={"x": 1},
              assert_={"status": 201, "jsonpath": {"$.id": "int"}})
    h = assess_case(c)
    assert h.approved
    assert h.veto is None
    assert h.score >= DEFAULT_MIN_QUALITY


def test_assess_case_dead_field_flag_invalid_assert():
    """指向已废弃字段的断言被识别为无效断言（dead field 用字段名匹配）。"""
    ctx = HealthContext(dead_fields={"gone"})
    c = _case(assert_={"jsonpath": {"$.gone": "str"}})
    h = assess_case(c, ctx)
    assert any(
        inv["kind"] == "dead_field" and "gone" in inv["target"]
        for inv in h.invalid_asserts
    )


def test_assess_suite_and_aggregate():
    cases = [
        _case(name="a", assert_={"status": 200, "jsonpath": {"$.x": 1}}),
        _case(name="b", assert_={}),  # 一票否决
        _case(name="c", assert_={"status": 200}),  # 弱断言，低于门禁
    ]
    report: HealthReport = aggregate_health(assess_suite(cases))
    assert report.total == 3
    assert report.approved == 1  # 只有 a 达标
    assert report.vetoed == 1    # b 无断言一票否决
    assert report.by_veto == {"no_assert": 1}
    assert report.avg_score >= 0 and report.avg_score <= 100


def test_aggregate_health_empty():
    """空套件聚合不崩溃。"""
    report = aggregate_health([])
    assert report.total == 0
    assert report.avg_score == 0.0
    assert report.avg_assert_density == 0.0
