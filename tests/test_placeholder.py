#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2026-10-09
# @Filename: test_placeholder.py
# @Author  : Alan_Hsu
"""⑦ 时间/随机占位符生成验证（${rand:...} / ${date:...}）。

生成类占位符每次产生新值（非确定），故只断言格式/范围/方向；并验证与既有
``${var}`` 插值共存、未知占位符保底原样。全部 hermetic。
"""

from __future__ import annotations

import datetime
import re
import uuid

import pytest

from ah.utils.context import Context, render, resolve_placeholders


@pytest.fixture(autouse=True)
def _llm_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制关闭 LLM：确保零网络依赖。"""
    monkeypatch.setenv("LLMOFF", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def test_date_now_is_iso_utc():
    """${date:now} 输出 ISO 8601 UTC 时间戳。"""
    out = render("${date:now}", Context())
    assert _ISO_RE.match(out)
    # 与真实 UTC 时间接近（< 60s 偏差）。
    parsed = datetime.datetime.strptime(out, "%Y-%m-%dT%H:%M:%SZ")
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    assert abs((now - parsed).total_seconds()) < 60


def test_date_plus_days_offset():
    """${date:+1d} 比 ${date:now} 晚约一天。"""
    base = render("${date:now}", Context())
    plus = render("${date:+1d}", Context())
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    base_dt = datetime.datetime.strptime(base, fmt)
    plus_dt = datetime.datetime.strptime(plus, fmt)
    assert 0.9 < (plus_dt - base_dt).days < 1.1


def test_date_minus_hours_offset():
    """${date:-2h} 比当前早约 2 小时。"""
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    out = datetime.datetime.strptime(render("${date:-2h}", Context()), "%Y-%m-%dT%H:%M:%SZ")
    assert 1.5 < ((now - out).total_seconds() / 3600) < 2.5


def test_date_now_custom_format():
    """${date:now:%Y%m%d} 按自定义 strftime 格式输出。"""
    assert re.match(r"^\d{8}$", render("${date:now:%Y%m%d}", Context()))


def test_rand_uuid_format():
    """${rand:uuid} 输出合法 UUID v4。"""
    out = render("${rand:uuid}", Context())
    assert _UUID_RE.match(out)
    assert uuid.UUID(out).version == 4


def test_rand_int_in_range():
    """${rand:int:1-10} 落在 [1,10]。"""
    for _ in range(50):
        out = render("${rand:int:1-10}", Context())
        assert 1 <= int(out) <= 10


def test_rand_int_reversed_range_normalized():
    """${rand:int:10-5} 归一化为 [5,10]。"""
    out = render("${rand:int:10-5}", Context())
    assert 5 <= int(out) <= 10


def test_rand_is_nondeterministic():
    """rand 每次产生新值（非确定性生成）。"""
    assert render("${rand:uuid}", Context()) != render("${rand:uuid}", Context())


def test_placeholder_mixes_with_var_interp():
    """占位符与 ${var} 插值共存。"""
    ctx = Context({"token": "abc"})
    out = render("${rand:uuid}|${token}", ctx)
    assert _UUID_RE.match(out.split("|")[0])
    assert out.split("|")[1] == "abc"


def test_unknown_placeholder_left_as_is():
    """不识别占位符（${foo:bar}）原样保留，不误报缺失变量。"""
    out = render("${foo:bar}", Context())
    assert out == "${foo:bar}"


def test_resolve_placeholders_noop_without_token():
    """无 ${} 内容直接原样返回（不触发生成）。"""
    assert resolve_placeholders("no token") == "no token"
