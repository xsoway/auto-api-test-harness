#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/17 12:30
# @Filename : test_skill_executor.py
# @Author   : Alan_Hsu
"""阶段 B：skill 组装（flow:）执行器单测。

覆盖：内联步骤 / 单次 call+assign / 嵌套 call / 作用域隔离（未 assign 不泄漏）/
未知 skill 转失败结果（不抛穿）/ 深度守卫。全程 hermetic（MockTransport 返回真实
JSON 体以驱动 extract/assign 传播），零网络、零 LLM。
"""

from __future__ import annotations

from typing import Any, Dict

import httpx

from ah.harness.http import HTTPClient
from ah.skill.executor import execute_flow
from ah.skill.model import load_skill_yaml
from ah.utils.context import Context


def _live_client(routes: Dict[str, httpx.Response]) -> HTTPClient:
    """构造按 path 命中（含前缀匹配）的隔离 HTTP 客户端，返回真实 JSON 体。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        for prefix, resp in routes.items():
            if request.url.path.startswith(prefix):
                return resp
        return httpx.Response(404, json={"error": "no route"})

    raw = httpx.Client(transport=httpx.MockTransport(_handler))
    return HTTPClient("http://api.test", client=raw)


def _registry(skills: Dict[str, Dict[str, Any]]) -> "Any":
    """从一个 skill 映射组合构建内存注册表（含嵌套 target）。"""
    reg = {}
    for name, data in skills.items():
        loaded = load_skill_yaml(data, source=f"<test:{name}>")
        reg[loaded.name] = loaded
    from ah.skill.model import SkillRegistry

    return SkillRegistry(items=reg)


def _login_skill() -> Dict[str, Any]:
    return {
        "name": "login",
        "outputs": ["user_token"],
        "steps": [
            {
                "name": "login",
                "request": {"method": "POST", "path": "/login"},
                "extract": {"user_token": {"path": "token", "extract_type": "jsonpath"}},
            }
        ],
    }


def _create_order_skill() -> Dict[str, Any]:
    return {
        "name": "create-order",
        "inputs": ["item_id"],
        "outputs": ["order_id"],
        "defaults": {"item_id": 7},
        "steps": [
            {
                "name": "create",
                "request": {"method": "POST", "path": "/orders", "json": {"item_id": "${item_id}"}},
                "extract": {"order_id": {"path": "order.id", "extract_type": "jsonpath"}},
            }
        ],
    }


def test_single_call_with_assign_propagates_to_flow_level():
    """单一 call + assign：被调 skill 产出上提到流程作用域，供后续消费。"""
    client = _live_client(
        {
            "/login": httpx.Response(200, json={"token": "tok-1"}),
            "/orders": httpx.Response(201, json={"order": {"id": 100}}),
        }
    )
    flow = [
        {"call": "login", "assign": {"user_token": "${user_token}"}},
        {"call": "create-order", "with": {"item_id": 5}, "assign": {"order_id": "${order_id}"}},
    ]
    ctx = Context()
    results = execute_flow(
        flow,
        _registry({"login": _login_skill(), "create-order": _create_order_skill()}),
        client,
        base_ctx=ctx,
    )
    # 两步都通过；流程作用域持有被上提的 user_token 与 order_id。
    assert len(results) == 2
    assert all(r.succeeded for r in results)
    assert ctx.get("user_token") == "tok-1"
    assert ctx.get("order_id") == 100
    # with 注入只作用于被调 skill；未声明的 item_id 不应泄漏到流程作用域。
    assert not ctx.has("item_id")


def test_scope_isolation_blocks_internal_vars_from_leaking():
    """无 assign 时，被调 skill 内部变量绝不上提到流程作用域。"""
    client = _live_client(
        {"/login": httpx.Response(200, json={"token": "tok-1"})}
    )
    flow = [{"call": "login"}]  # 不写 assign
    ctx = Context()
    results = execute_flow(flow, _registry({"login": _login_skill()}), client, base_ctx=ctx)
    assert len(results) == 1 and results[0].succeeded
    # 内部产出 user_token 被回滚，流程作用域看不到。
    assert not ctx.has("user_token")


def test_nested_call_inside_skill():
    """skill 内部再 call 其他 skill：嵌套互调递归执行。"""
    ship = {
        "name": "ship",
        "inputs": ["order_id"],
        "outputs": ["tracking_no"],
        "steps": [
            {
                "name": "ship-it",
                "request": {"method": "POST", "path": "/ship/${order_id}"},
                "extract": {"tracking_no": {"path": "tracking", "extract_type": "jsonpath"}},
            }
        ],
    }
    outer = {
        "name": "full-order",
        "outputs": ["tracking_no"],
        "steps": [
            {
                "name": "login",
                "request": {"method": "POST", "path": "/login"},
                "extract": {"user_token": {"path": "token", "extract_type": "jsonpath"}},
            },
            {
                "call": "create-order",
                "with": {"item_id": 3},
                "assign": {"order_id": "${order_id}"},
            },
            {
                "call": "ship",
                "with": {"order_id": "${order_id}"},
                "assign": {"tracking_no": "${tracking_no}"},
            },
        ],
    }
    client = _live_client(
        {
            "/login": httpx.Response(200, json={"token": "tok-1"}),
            "/orders": httpx.Response(201, json={"order": {"id": 9}}),
            "/ship/9": httpx.Response(200, json={"tracking": "T-42"}),
        }
    )
    flow = [{"call": "full-order", "assign": {"tracking_no": "${tracking_no}"}}]
    ctx = Context()
    results = execute_flow(
        flow,
        _registry({"full-order": outer, "create-order": _create_order_skill(), "ship": ship}),
        client,
        base_ctx=ctx,
    )
    assert len(results) == 3  # login + create + ship-it
    assert all(r.succeeded for r in results)
    # 最外层通过 assign 拿到最深层的 tracking_no。
    assert ctx.get("tracking_no") == "T-42"
    # 中间变量 order_id / user_token 不泄漏到流程作用域。
    assert not ctx.has("order_id")
    assert not ctx.has("user_token")


def test_inline_step_in_flow():
    """flow 里允许内联步骤（name+request），不依赖任何 skill。"""
    client = _live_client({"/health": httpx.Response(200, json={"status": "ok"})})
    flow = [
        {"name": "health", "request": {"method": "GET", "path": "/health"}},
    ]
    ctx = Context()
    results = execute_flow(flow, _registry({}), client, base_ctx=ctx)
    assert len(results) == 1 and results[0].succeeded


def test_unknown_skill_becomes_failed_result_not_exception():
    """引用不存在的 skill：execute_flow 不抛穿，产出失败 StepResult。"""
    client = _live_client({})
    flow = [{"call": "no-such-skill"}]
    ctx = Context()
    results = execute_flow(flow, _registry({}), client, base_ctx=ctx)
    assert len(results) == 1
    assert not results[0].succeeded
    assert "no-such-skill" in results[0].reason


def test_depth_guard_guards_runaway_call_chain():
    """过深的嵌套互调触发深度守卫：execute_flow 不抛穿，产出失败 StepResult。"""
    # 自引用链（模拟环兜底）：a -> a 直到 MAX_CALL_DEPTH。
    chain: Dict[str, Any] = {
        "name": "recur",
        "steps": [{"call": "recur"}],
    }
    registry = _registry({"recur": chain})
    client = _live_client({})
    results = execute_flow([{"call": "recur"}], registry, client, base_ctx=Context())
    # 深度越界被降级为一条失败结果（含深度消息），而不是抛出异常中断整个流程。
    assert len(results) == 1
    assert not results[0].succeeded
    assert "深度" in results[0].reason

