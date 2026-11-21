"""demo 业务项目执行入口（框架演示 / 新增业务参考模板）。

演示如何在业务项目内自包含地驱动三种用例形态，全部用桩传输隔离运行（不连真实
服务）：
- **单接口**（``users_single.yaml``）：``run_single`` + 桩响应，验证
  ``${user_id}`` 插值进 URL、``fetched_name`` 提取到共享上下文。
- **场景流程**（``order_flow.yaml``）：``run_flow``，验证登录提取的
  ``user_token`` 向下游传递，且 ``on_failure=stop`` 在首个失败处中止。
- **分类元数据**（``categorized.yaml``）：``select_cases`` 按 env/suite/
  priority/tags 过滤，验证冒烟（test 环境）子集。

一个业务项目一个这样的 ``test_<业务>.py``：加载自己的 ``data/``、定义自己的
桩/实连传输、断言业务预期。真实巡检项目在 conftest 里注册 ``--run-live``
并将对应测试指向专用测试环境。
"""

from __future__ import annotations

from pathlib import Path

import httpx

from ah.core.orchestrator import run_flow, run_single
from ah.harness.http import HTTPClient
from ah.harness.loader import load_cases, select_cases
from ah.utils.context import Context

_DIR = Path(__file__).resolve().parent
DATA = str(_DIR / "data")


def test_single_interpolates_and_extracts():
    """单接口：``${user_id}`` 插值进请求 URL，响应字段提取到共享上下文。"""
    case = load_cases(f"{DATA}/users_single.yaml")[0]
    assert case.name == "get-user-by-id"
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"user": {"id": 42, "name": "Ada Wang"}})

    raw = httpx.Client(transport=httpx.MockTransport(handler))
    client = HTTPClient("http://api.test", client=raw)
    ctx = Context()
    ctx.set("user_id", 42)
    with raw:
        result = run_single(case, ctx, client)
    assert result.succeeded, result.error
    assert captured["url"] == "http://api.test/users/42"
    assert result.extracted == {"fetched_name": "Ada Wang"}


def test_flow_passes_user_token_downstream():
    """场景流程：登录提取的 user_token 传递给下游订单步骤。"""
    cases = load_cases(f"{DATA}/order_flow.yaml")
    assert len(cases) == 2
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {"path": str(request.url), "auth": request.headers.get("authorization")}
        )
        if request.url.path == "/login":
            return httpx.Response(200, json={"token": "user-tok-1"})
        return httpx.Response(201, json={"order": {"id": 100}})

    raw = httpx.Client(transport=httpx.MockTransport(handler))
    client = HTTPClient("http://api.test", client=raw)
    with raw:
        results = run_flow(cases, Context(), client)
    assert len(results) == 2
    assert all(r.succeeded for r in results)
    assert captured[1]["path"] == "http://api.test/orders"
    assert captured[1]["auth"] == "Bearer user-tok-1"


def test_flow_stops_on_first_failure():
    """on_failure=stop：登录失败即中止，不执行订单步骤。"""
    seen = []
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(401, json={"error": "unauthorized"})

    raw = httpx.Client(transport=httpx.MockTransport(handler))
    client = HTTPClient("http://api.test", client=raw)
    cases = load_cases(f"{DATA}/order_flow.yaml")
    with raw:
        results = run_flow(cases, Context(), client)
    assert len(results) == len(cases)
    assert results[1].status == "skipped"
    assert results[1].attempts == 0
    assert set(seen) == {"/login"}
    assert not results[0].succeeded


def test_categorized_smoke_filter():
    """分类元数据：select_cases 按 env/suite 过滤出冒烟子集。"""
    cases = load_cases(f"{DATA}/categorized.yaml")
    assert len(cases) == 1
    smoke = select_cases(cases, suite="smoke", env="test")
    assert [c.name for c in smoke] == ["health-check"]
    # 其它过滤维度未命中子集为空。
    assert select_cases(cases, suite="regression") == []
    assert select_cases(cases, priority="P1") == []