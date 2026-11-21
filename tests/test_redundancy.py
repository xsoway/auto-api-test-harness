#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/15 16:40
# @Filename : test_redundancy.py
# @Author   : Alan_Hsu
"""``ah.quality.redundancy`` 单元测试：等价指纹、重复聚类、语义分支回退、低价值筛排。

Hermetic：语义分支 default 回退到指纹聚类，不依赖真实 Embedding/LLM。
"""

from ah.harness.loader import Case
from ah.quality.redundancy import (
    case_fingerprint,
    group_duplicates,
    screen_low_value,
    semantic_similar_groups,
)


def _case(name: str = "c", method: str = "GET", path: str = "/api/items",
          params: dict | None = None, json_body: dict | None = None,
          assert_: dict | None = None, priority: str = "P1") -> Case:
    req: dict = {"method": method, "path": path}
    if params:
        req["params"] = params
    if json_body:
        req["json"] = json_body
    return Case(name=name, request=req, assert_=assert_ or {}, priority=priority)


# ───────────────────────── 等价指纹 ─────────────────────────


def test_fingerprint_ignores_param_values():
    """参数值不同但参数空间 + 断言集合相同 ⇒ 同指纹（业务等价重复候选）。"""
    a = _case(name="a1", path="/api/items", params={"page": 1, "size": 20},
              assert_={"status": 200})
    b = _case(name="a2", path="/api/items", params={"page": 2, "size": 50},
              assert_={"status": 200})
    assert case_fingerprint(a) == case_fingerprint(b)


def test_fingerprint_differs_by_assert_keys():
    """断言键集合不同 ⇒ 指纹不同（覆盖不同业务规则）。"""
    a = _case(name="a1", path="/api/items", assert_={"status": 200})
    b = _case(name="a2", path="/api/items", assert_={"status": 200, "jsonpath": {"$.x": 1}})
    assert case_fingerprint(a) != case_fingerprint(b)

def test_fingerprint_normalizes_template_params():
    """模板参数命名不同（{id} vs {other}）视为同路径；具体数字段归一。"""
    a = _case(name="a1", path="/api/items/{id}", assert_={"status": 200})
    b = _case(name="a2", path="/api/items/{other}", assert_={"status": 200})
    assert case_fingerprint(a) == case_fingerprint(b)
    # 具体数字段 123 与 456 也归一为同一指纹。
    c = _case(name="a3", path="/api/items/123", assert_={"status": 200})
    d = _case(name="a4", path="/api/items/456", assert_={"status": 200})
    assert case_fingerprint(c) == case_fingerprint(d)
# ───────────────────────── 重复聚类 ─────────────────────────


def test_group_duplicates_clusters_equivalent():
    """同指纹用例聚成一组，主版本为质量最优（scorer 注入）。"""
    cases = [
        _case(name="a1", path="/api/items", params={"page": 1},
              assert_={"status": 200}, priority="P2"),
        _case(name="a3", path="/api/items", params={"page": 2},
              assert_={"status": 200}, priority="P1"),
        _case(name="b", path="/api/other", assert_={"status": 200}),
    ]
    groups = group_duplicates(cases)
    assert len(groups) == 1
    g = groups[0]
    assert len(g.members) == 2
    assert g.primary.name == "a3"  # P1 优先于 P2
    assert {m.name for m in g.redundant} == {"a1"}


def test_group_duplicates_exact_duplicate_flag():
    """文本完全一致的重复用例标记 is_exact_duplicate。"""
    cases = [
        _case(name="x", path="/api/items", assert_={"status": 200}),
        _case(name="x", path="/api/items", assert_={"status": 200}),
        _case(name="y", path="/api/other", assert_={"status": 200}),
    ]
    groups = group_duplicates(cases)
    assert len(groups) == 1
    assert groups[0].is_exact_duplicate is True


def test_group_duplicates_empty_when_no_dup():
    """无重复时返回空列表。"""
    cases = [
        _case(name="a", path="/api/items", assert_={"status": 200}),
        _case(name="b", path="/api/other", assert_={"status": 200}),
    ]
    assert group_duplicates(cases) == []


def test_semantic_similar_groups_falls_back_to_fingerprint():
    """未注入 embed/llm ⇒ 语义分支回退到指纹聚类（Hermetic）。"""
    cases = [
        _case(name="a1", path="/api/items", assert_={"status": 200}),
        _case(name="a2", path="/api/items", assert_={"status": 200}),
        _case(name="b", path="/api/other", assert_={"status": 200}),
    ]
    groups = semantic_similar_groups(cases)
    assert len(groups) == 1
    assert {m.name for m in groups[0].members} == {"a1", "a2"}


# ───────────────────────── 低价值筛排 ─────────────────────────


def test_screen_low_value_no_effective_assert():
    """无有效断言（弱/空）⇒ no_effective_assert。"""
    cases = [
        _case(name="weak", assert_={"status": 200}),
        _case(name="strong", assert_={"status": 200, "jsonpath": {"$.x": 1}}),
    ]
    report = screen_low_value(cases)
    reasons = {i.reason for i in report.items}
    assert reasons == {"no_effective_assert"}
    assert [i.case.name for i in report.items] == ["weak"]


def test_screen_low_value_retired_interface():
    """覆盖已下线接口 ⇒ retired_interface。"""
    cases = [_case(name="gone", method="DELETE", path="/api/items", assert_={"status": 200})]
    report = screen_low_value(cases, retired_paths={("DELETE", "/api/items")})
    assert report.items[0].reason == "retired_interface"


def test_screen_low_value_always_pass_no_defect():
    """连续全 PASS 且无缺陷 ⇒ always_pass_no_defect（需达到 min_success_count）。"""
    cases = [
        _case(name="passer", path="/api/items",
              assert_={"status": 200, "jsonpath": {"$.x": 1}}),
    ]
    recent = {"passer": ["PASS", "PASS", "PASS"]}
    report = screen_low_value(cases, recent_results=recent)
    assert report.items[0].reason == "always_pass_no_defect"


def test_screen_low_value_defect_breaks_no_defect():
    """出现 DEFECT 则不算"无缺陷"，不判低价值（若断言有效）。"""
    cases = [
        _case(name="passer", path="/api/items",
              assert_={"status": 200, "jsonpath": {"$.x": 1}}),
    ]
    recent = {"passer": ["PASS", "PASS", "DEFECT"]}
    report = screen_low_value(cases, recent_results=recent)
    assert report.items == []


def test_screen_low_value_requires_min_success_count():
    """运行次数不足 min_success_count 不判 always_pass。"""
    cases = [
        _case(name="fresh", path="/api/items",
              assert_={"status": 200, "jsonpath": {"$.x": 1}}),
    ]
    recent = {"fresh": ["PASS"]}
    report = screen_low_value(cases, recent_results=recent, min_success_count=3)
    assert report.items == []
