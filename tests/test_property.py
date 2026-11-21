#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2026-10-08 21:20
# @Filename: test_property.py
# @Author  : Alan_Hsu
"""hypothesis 未装→清晰报错；装了→对样本用例做参数空间探索。

全程零网络：HTTP 走 ``httpx.MockTransport``。
"""
from __future__ import annotations

import argparse

import httpx

from ah.harness.http import HTTPClient
from ah.harness.loader import Case
from ah.property import (
    HypothesisUnavailable,
    build_payload_strategies,
    explore_properties,
    require_hypothesis,
    run,
)


def _client(handler) -> HTTPClient:
    raw = httpx.Client(transport=httpx.MockTransport(handler))
    return HTTPClient("http://api.test", client=raw)


def _case(*, json_body=None, assert_body=None, params=None) -> Case:
    request: dict = {"method": "POST", "path": "/echo"}
    if json_body is not None:
        request["json"] = json_body
    if params is not None:
        request["params"] = params
    return Case(name="c1", request=request, assert_=assert_body or {})


def test_hypothesis_installed_in_this_env():
    """guarantee: 本测试环境需装了 hypothesis（否则下方探索断言无意义）。"""
    ns = require_hypothesis()
    assert ns.hypothesis is not None


def test_build_payload_strategies_no_payload():
    """无 json/params → 两个 just(None)，表示无参数空间可探索。"""
    ns = require_hypothesis()
    jsn, params = build_payload_strategies(_case().request)
    assert ns.hypothesis.find(jsn, lambda x: x is None) is None
    assert ns.hypothesis.find(params, lambda x: x is None) is None


def test_explore_all_pass_when_assert_holds():
    """断言恒真（status 200, mock 恒 200）→ 全部样本通过，run==examples。"""
    case = _case(json_body={"age": 42}, assert_body={"status_code": 200})
    client = _client(lambda r: httpx.Response(200, text="ok"))
    rep = explore_properties(case, examples=25, client=client, seed=7)
    assert rep.examples_run == 25
    assert rep.failed == 0
    assert rep.passed == 25
    assert rep.all_passed is True
    assert rep.success_rate == 1.0


def test_explore_failure_counted_does_not_abort():
    """断言恒假（n 是整数却断言为 string）→ 失败计数==examples，探索不中断。"""
    case = _case(
        json_body={"n": 1},
        assert_body={"jsonpath": {"n": {"type": "string"}}},
    )
    client = _client(lambda r: httpx.Response(200, json={"n": 1}))
    rep = explore_properties(case, examples=15, client=client, seed=3)
    assert rep.examples_run == 15
    assert rep.failed == 15
    assert rep.passed == 0
    assert rep.all_passed is False
    assert rep.success_rate == 0.0


def test_explore_varies_params_not_just_single_value():
    """参数空间被真正探索：不同样本收到不同请求体（Mock 记录所有 json）。"""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.content.decode("utf-8", "replace"))
        return httpx.Response(200, text="ok")

    case = _case(json_body={"n": 1}, assert_body={"status_code": 200})
    client = _client(handler)
    explore_properties(case, examples=20, client=client, seed=11)
    # json 是 {"n": <int>}；探索应产生超过 1 种 n 值。
    assert len({s for s in seen}) > 1


def test_seed_deterministic():
    """同一种子复现同样样本序列。"""
    case = _case(
        json_body={"v": 1},
        assert_body={"jsonpath": {"v": {"type": "integer"}}},
    )
    r1 = explore_properties(
        case, examples=8, client=_client(lambda r: httpx.Response(200)), seed=99
    )
    r2 = explore_properties(
        case, examples=8, client=_client(lambda r: httpx.Response(200)), seed=99
    )
    assert [(s.passed, s.error, s.json) for s in r1.samples] == [
        (s.passed, s.error, s.json) for s in r2.samples
    ]
    assert r1.failed == r2.failed
    assert r1.passed == r2.passed


def test_run_reports_clear_error_when_hypothesis_missing(monkeypatch):
    """hypothesis 未装（模拟 HypothesisUnavailable）→ run 返回 2。"""

    def _boom():
        raise HypothesisUnavailable("未安装 hypothesis，请先 `pip install hypothesis`")

    monkeypatch.setattr("ah.property.require_hypothesis", _boom)
    args = argparse.Namespace(
        project="x", case_name="y", examples=5, seed=None, write_json=False
    )
    assert run(args) == 2
