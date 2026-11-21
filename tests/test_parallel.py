#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2026-10-09
# @Filename: test_parallel.py
# @Author  : Alan_Hsu
"""③ 并行执行 parallel 验证：run_cases(parallel>1) 只并行相互独立的用例链。

覆盖：parallel=1 保持既有顺序/共享 Context 语义；parallel>1 真实并发、同场景链
内顺序且共享 Context、输出顺序稳定、跨链 Context 隔离；空切片。全部 hermetic
（stub 传输 + 线程安全并发峰值计数，无时序竞态）。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

import httpx
import pytest

from ah.harness.http import HTTPClient
from ah.harness.loader import Case
from ah.scheduler import run_cases


@pytest.fixture(autouse=True)
def _llm_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制关闭 LLM：确保所有用例 URL 匹配走确定性回退、零网络依赖（hermetic）。"""
    monkeypatch.setenv("LLMOFF", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def _case(name: str, path: str, scenario: Optional[str] = None,
          order: int = 0, extract: Optional[dict[str, Any]] = None,
          vars_req: Optional[list[str]] = None, status: int = 200) -> Case:
    """构造一个 hermetic 用例：默认 GET /path 断言 status。"""
    return Case(
        name=name,
        request={"method": "GET", "path": path},
        extract=extract or {},
        assert_={"status_code": status},
        vars_req=vars_req or [],
        scenario=scenario,
        order=order,
    )


class _ClockClient(httpx.Client):
    """统计并发峰值的桩客户端：请求时占用一个 slot 并记录活跃峰值。

    所有线程共用一个锁 + 计数器；每个请求 sleep 固定时长再释放 slot，峰值越高
    代表真实并发更强。响应按路径区分：含未渲染 ``${token}`` 字面量则返回 400，
    其余返回 200 + token 字段（供提取）。
    """

    def __init__(self, delay: float = 0.05, handler: Optional[Callable] = None):
        super().__init__(transport=httpx.MockTransport(handler or self._default_handler))
        self.delay = delay
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.thread_ids: list[int] = []

    def _default_handler(self, request: httpx.Request) -> httpx.Response:
        if "${" in str(request.url.path):
            return httpx.Response(400, json={"error": "unrendered template"})
        return httpx.Response(200, json={"token": "abc123", "ok": True})

    def request(self, method, url, **kwargs):
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.thread_ids.append(threading.get_ident())
        try:
            time.sleep(self.delay)
            return super().request(method, url, **kwargs)
        finally:
            with self._lock:
                self._active -= 1


def _wrap(client: _ClockClient) -> HTTPClient:
    return HTTPClient("http://api.test", client=client, timeout=30.0)


# ───────────────────────── parallel=1：既有语义 ─────────────────────────


def test_parallel_one_keeps_shared_context_sequential():
    """parallel=1：顺序执行且共享单一 Context（提取变量跨用例传递）。"""
    cases = [
        _case("a", "/login", extract={"token": {"path": "token"}}),
        _case("b", "/orders/${token}", vars_req=["token"]),
    ]
    raw = _ClockClient(delay=0.01)
    wrapper = _wrap(raw)
    try:
        records = run_cases(cases, client=wrapper, parallel=1)
    finally:
        wrapper.close()
    assert [r.name for r in records] == ["a", "b"]
    # b 的 ${token} 从共享 Context 取到 a 提取的值 → 路径命中 200 分支 → PASS。
    assert records[1].result == "PASS"


def test_parallel_one_max_active_is_one():
    """parallel=1 不并发：任何时刻最多 1 个在途请求。"""
    cases = [_case(f"c{i}", f"/p{i}") for i in range(4)]
    raw = _ClockClient(delay=0.03)
    wrapper = _wrap(raw)
    try:
        run_cases(cases, client=wrapper, parallel=1)
    finally:
        wrapper.close()
    assert raw.max_active == 1


# ───────────────────────── parallel>1：真实并发 ─────────────────────────


def test_parallel_gt1_runs_independent_chains_concurrently():
    """parallel>1：多个独立单条用例真实并发（峰值 ≥ 2）。"""
    cases = [_case(f"c{i}", f"/p{i}") for i in range(4)]
    raw = _ClockClient(delay=0.05)
    wrapper = _wrap(raw)
    try:
        run_cases(cases, client=wrapper, parallel=4)
    finally:
        wrapper.close()
    assert raw.max_active >= 2


def test_parallel_preserves_output_order():
    """parallel>1：输出记录顺序与输入一致（顺序稳定）。"""
    cases = [_case(f"c{i}", f"/p{i}") for i in range(6)]
    raw = _ClockClient(delay=0.02)
    wrapper = _wrap(raw)
    try:
        records = run_cases(cases, client=wrapper, parallel=3)
    finally:
        wrapper.close()
    assert [r.name for r in records] == ["c0", "c1", "c2", "c3", "c4", "c5"]


def test_parallel_same_scenario_chain_sequential_and_shared_context():
    """parallel>1：同场景相邻用例组成单链、链内顺序且共享 Context。"""
    cases = [
        _case("a", "/login", scenario="flow", order=1,
              extract={"token": {"path": "token"}}),
        _case("b", "/orders/${token}", scenario="flow", order=2,
              vars_req=["token"]),
        _case("x", "/other", scenario=None),
    ]
    raw = _ClockClient(delay=0.02)
    wrapper = _wrap(raw)
    try:
        records = run_cases(cases, client=wrapper, parallel=2)
    finally:
        wrapper.close()
    # 同场景链内 a→b 顺序执行且共享 Context：b 的 ${token} 被渲染 → PASS。
    assert [r.name for r in records] == ["a", "b", "x"]
    assert records[1].result == "PASS"


def test_parallel_cross_chain_context_isolated():
    """parallel>1：不同链之间 Context 隔离（跨链提取变量不共享，边界文档化）。

    b（独立不同场景）引用 a 提取的 token；并行模式下链各自独立 Context，token
    不共享 → b 的 ${token} 未渲染 → 命中 400 分支 → FAIL。
    """
    cases = [
        _case("a", "/login", scenario="s1", extract={"token": {"path": "token"}}),
        _case("b", "/orders/${token}", scenario="s2", vars_req=["token"]),
    ]
    raw = _ClockClient(delay=0.01)
    wrapper = _wrap(raw)
    try:
        records = run_cases(cases, client=wrapper, parallel=2)
    finally:
        wrapper.close()
    names = {r.name: r for r in records}
    assert names["a"].result == "PASS"
    # 隔离是预期边界：b 因跨链取不到 token 而失败。
    assert names["b"].result == "FAIL"


# ───────────────────────── 空切片 ─────────────────────────


def test_run_cases_empty_returns_empty():
    """空用例切片：无论并行与否都返回空列表。"""
    raw = _ClockClient(delay=0.01)
    wrapper = _wrap(raw)
    try:
        assert run_cases([], client=wrapper, parallel=4) == []
    finally:
        wrapper.close()
