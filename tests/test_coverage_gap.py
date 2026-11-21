#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/15 17:00
# @Filename : test_coverage_gap.py
# @Author   : Alan_Hsu
"""``ah.coverage.gap`` 单元测试：接口/参数差集、模板匹配、覆盖率计算。

直接构造 catalog dict（与 normalize_catalog 输出同构），不依赖真实 OpenAPI 文件。
"""

from ah.coverage.gap import (
    _template_match,
    compute_gaps,
    load_operations,
)
from ah.harness.loader import Case


def _catalog(ops: list[dict]) -> dict:
    return {"operations": ops}


def _op(method: str, path: str, params: list[str] | None = None,
        request_props: list[str] | None = None) -> dict:
    op: dict = {"method": method, "path": path,
                "parameters": [{"name": p, "in": "query"} for p in (params or [])]}
    if request_props:
        op["request_schema"] = {"type": "object",
                                "properties": {p: {"type": "string"} for p in request_props}}
    return op


def _case(name: str, method: str, path: str, params: dict | None = None,
          json_body: dict | None = None) -> Case:
    req: dict = {"method": method, "path": path}
    if params:
        req["params"] = params
    if json_body:
        req["json"] = json_body
    return Case(name=name, request=req)


# ───────────────────────── 模板匹配 ─────────────────────────


def test_template_match_exact_and_numeric():
    assert _template_match("/api/users/123", "/api/users/{id}") is True
    assert _template_match("/api/users", "/api/users") is True
    assert _template_match("/api/users", "/api/orders") is False


def test_template_match_dollar_var_syntax():
    """框架自身 ${user_id} 变量语法与契约 {id} 模板等价。"""
    assert _template_match("/users/${user_id}", "/users/{id}") is True


# ───────────────────────── 接口 / 参数缺口 ─────────────────────────


def test_compute_gaps_interface_and_param():
    """未覆盖接口 → interface 缺口；已覆盖但缺声明参数 → param 缺口。"""
    catalog = _catalog([
        _op("GET", "/users", params=["page"]),            # 已覆盖，参数未覆盖
        _op("POST", "/orders", request_props=["amount"]),  # 已覆盖，参数未覆盖
        _op("DELETE", "/legacy", params=["uid"]),          # 完全未覆盖
    ])
    cases = [
        _case("u", "GET", "/users"),
        _case("o", "POST", "/orders"),
    ]
    report = compute_gaps(catalog, cases)
    assert report.total_operations == 3
    assert report.covered_interfaces == 2
    assert report.coverage_ratio == round(2 / 3 * 100, 1)  # 66.7
    kinds = {g.kind for g in report.gaps}
    assert "interface" in kinds
    assert "param" in kinds
    assert any(g.path == "/legacy" and g.kind == "interface" for g in report.gaps)
    assert any(g.path == "/users" and g.kind == "param" and "page" in g.detail
               for g in report.gaps)


def test_compute_gaps_full_coverage_no_gaps():
    """用例覆盖全部契约接口且参数齐 → 无缺口。"""
    catalog = _catalog([
        _op("GET", "/users", params=["page"]),
    ])
    cases = [_case("u", "GET", "/users", params={"page": 1})]
    report = compute_gaps(catalog, cases)
    assert report.gaps == []
    assert report.coverage_ratio == 100.0


def test_compute_gaps_include_params_false():
    """include_params=False 时忽略参数缺口，只报接口缺口。"""
    catalog = _catalog([
        _op("GET", "/users", params=["page"]),
    ])
    cases = [_case("u", "GET", "/users")]
    report = compute_gaps(catalog, cases, include_params=False)
    assert report.gaps == []


def test_compute_gaps_template_path_matches_case():
    """契约模板路径与用例具体路径匹配（/users/123 ↔ /users/{id}）。"""
    catalog = _catalog([
        _op("GET", "/users/{id}", params=[]),
    ])
    cases = [_case("u", "GET", "/users/123")]
    report = compute_gaps(catalog, cases)
    assert report.covered_interfaces == 1
    assert report.gaps == []


# ───────────────────────── 辅助函数 ─────────────────────────


def test_load_operations_extracts_methods_and_params():
    """load_operations 提取 method/path/params（含 request_schema 字段）。"""
    ops = load_operations(_catalog([
        _op("POST", "/orders", request_props=["amount", "item"]),
        _op("get", "/health"),
    ]))
    assert len(ops) == 2
    # 排序：GET 在 POST 前。
    assert ops[0].method == "GET"
    assert [o.method for o in ops] == ["GET", "POST"]
    post = ops[1]
    assert post.path == "/orders"
    assert post.params == {"amount", "item"}


def test_coverage_report_to_dict_and_empty():
    """CoverageReport.to_dict 结构稳定；空契约覆盖率 0。"""
    empty = compute_gaps(_catalog([]), [])
    assert empty.total_operations == 0
    assert empty.coverage_ratio == 0.0
    d = empty.to_dict()
    assert d["total_operations"] == 0
    assert d["coverage_ratio"] == 0.0
    assert d["gaps"] == []
