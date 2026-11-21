#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/30 09:30
# @Filename : test_variables.py
# @Author   : Alan_Hsu

"""声明式 variables 数据策略的单测与链路端到端验证。

覆盖：materialize_variables 各策略生成；未知策略快速失败；loader 顶层声明
挂到各 step；execute_flow 在链执行前把生成值注入 Context 供 ``${var}`` 消费，
且整链共享同一批生成值。
"""

import httpx
import pytest

from ah.core.orchestrator import run_flow
from ah.core.variables import materialize_variables
from ah.harness.http import HTTPClient
from ah.harness.loader import Case, load_cases
from ah.utils.context import Context


def _client(handler):
    return HTTPClient(
        "http://api.test", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def _case(name, *, variables=None, request=None, **kwargs):
    return Case(
        name=name,
        request=request or {"method": "GET", "path": f"/{name}"},
        scenario=kwargs.pop("scenario", "chain"),
        strict=kwargs.pop("strict", True),
        reference_mode=kwargs.pop("reference_mode", "typed"),
        assert_=kwargs.pop("assert_", {"status_code": 200}),
        variables=variables or [],
        **kwargs,
    )


# ---------- materialize_variables：策略生成 ----------

def test_timestamp_prefix_and_uniqueness():
    ctx = Context()
    materialize_variables([{"name": "u", "strategy": "timestamp", "prefix": "shop_"}], ctx)
    v1 = ctx.get("u")
    # 秒级毫秒时间戳，带前缀
    assert v1.startswith("shop_") and v1[len("shop_"):].isdigit()
    # 同一次运行内多次调用唯一
    materialize_variables([{"name": "u", "strategy": "timestamp", "prefix": "shop_"}], ctx)
    assert ctx.get("u") != v1


def test_random_prefix_and_length_with_seed_reproducible():
    env = [{"name": "phone", "strategy": "random", "prefix": "138", "length": 8, "seed": 7}]
    a = Context()
    b = Context()
    materialize_variables(env, a)
    materialize_variables(env, b)
    assert a.get("phone") == b.get("phone")  # seed 可复现
    assert a.get("phone").startswith("138")
    assert len(a.get("phone")) == 3 + 8


def test_fixed_value_preserves_type():
    ctx = Context()
    materialize_variables(
        [{"name": "pwd", "value": "Test@123456"}, {"name": "count", "value": 5}], ctx
    )
    assert ctx.get("pwd") == "Test@123456"
    assert ctx.get("count") == 5 and isinstance(ctx.get("count"), int)


def test_uuid_strategy():
    ctx = Context()
    materialize_variables([{"name": "uid", "strategy": "uuid"}], ctx)
    v = ctx.get("uid")
    assert len(v) == 36 and v.count("-") == 4


def test_empty_and_none_are_noop():
    ctx = Context()
    assert materialize_variables(None, ctx) is ctx
    assert materialize_variables([], ctx) is ctx
    assert ctx.as_dict() == {}


def test_missing_name_raises():
    with pytest.raises(ValueError):
        materialize_variables([{"strategy": "uuid"}], Context())


def test_strategy_and_value_exclusive_raises():
    with pytest.raises(ValueError):
        materialize_variables(
            [{"name": "x", "strategy": "uuid", "value": "abc"}], Context()
        )


def test_unknown_strategy_raises():
    with pytest.raises(ValueError):
        materialize_variables([{"name": "x", "strategy": "magic"}], Context())


def test_fixed_requires_value_raises():
    with pytest.raises(ValueError):
        materialize_variables([{"name": "x", "strategy": "fixed"}], Context())


# ---------- loader：顶层 variables 挂到各 step ----------

def test_loader_attaches_top_level_variables(tmp_path):
    f = tmp_path / "scen.yaml"
    f.write_text(
        "schema_version: 2\n"
        "name: order-flow\n"
        "variables:\n"
        "  - { name: username, strategy: timestamp, prefix: shop_ }\n"
        "scenario:\n"
        "  - name: login\n"
        "    request: { method: POST, path: /login, json: { u: '${username}' } }\n"
        "  - name: create\n"
        "    request: { method: POST, path: /order, json: { phone: '${username}' } }\n",
        encoding="utf-8",
    )
    cases = load_cases(f)
    assert len(cases) == 2
    for c in cases:
        assert c.variables == [{"name": "username", "strategy": "timestamp", "prefix": "shop_"}]
        assert c.scenario == "order-flow"


def test_loader_step_level_variables_override_top(tmp_path):
    f = tmp_path / "scen.yaml"
    f.write_text(
        "schema_version: 2\n"
        "variables:\n"
        "  - { name: username, strategy: timestamp, prefix: shop_ }\n"
        "scenario:\n"
        "  - name: login\n"
        "    variables: [ { name: username, value: alan } ]\n"
        "    request: { method: POST, path: /login, json: { u: '${username}' } }\n",
        encoding="utf-8",
    )
    cases = load_cases(f)
    assert cases[0].variables == [{"name": "username", "value": "alan"}]


def test_loader_no_variables_keeps_empty(tmp_path):
    f = tmp_path / "single.yaml"
    f.write_text(
        "schema_version: 2\n"
        "name: ping\n"
        "request: { method: GET, path: /ping }\n",
        encoding="utf-8",
    )
    assert load_cases(f)[0].variables == []


# ---------- execute_flow 端到端：生成值注入并跨步骤共享 ----------

@pytest.mark.parametrize("strict", [True, False])
def test_chain_shared_generated_variables(strict):
    captured = {}

    def handler(request):
        import json as _json
        captured[request.url.path] = _json.loads(request.content or b"{}")
        return httpx.Response(200, json={"ok": True})

    cases = [
        _case(
            "producer",
            variables=[{"name": "ck", "strategy": "timestamp", "prefix": "chk_"}],
            request={
                "method": "POST", "path": "/seed",
                "json": {"token": "${ck}"},
            },
            strict=strict,
        ),
        _case(
            "consumer",
            depends_on=["producer"],
            request={
                "method": "POST", "path": "/consume",
                "json": {"token": "${ck}"},
            },
            strict=strict,
        ),
    ]
    client = _client(handler)
    results = run_flow(cases, Context(), client)
    assert all(r.verdict == "passed" for r in results)
    # 两个步骤注入的同一生成值（timestamp 只生成一次）
    assert captured["/seed"]["token"] == captured["/consume"]["token"]
    assert captured["/seed"]["token"].startswith("chk_")
