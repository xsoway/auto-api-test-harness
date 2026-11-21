"""Step 11 演示：场景流程传递上游变量并在首个失败处停止。

加载 ``projects/demo/data/order_flow.yaml``，并通过
:func:`ah.core.orchestrator.run_flow` 结合桩传输进行驱动。验证订单步骤
收到登录步骤提取的 ``user_token``（通过 ``${user_token}`` 插值写入
Authorization 请求头），并且失败的步骤会中止整个流程（``on_failure=stop``）。
"""

import httpx

from ah.core.orchestrator import run_flow
from ah.harness.http import HTTPClient
from ah.harness.loader import Case, load_cases
from ah.utils.context import Context


def _flow_client(captured: list[dict]) -> HTTPClient:
    def handler(request):
        captured.append({"path": str(request.url), "auth": request.headers.get("authorization")})
        if request.url.path == "/login":
            return httpx.Response(200, json={"token": "user-tok-1"})
        return httpx.Response(201, json={"order": {"id": 100}})

    transport = httpx.MockTransport(handler)
    raw = httpx.Client(transport=transport)
    return HTTPClient("http://api.test", client=raw), raw


def test_demo_flow_passes_user_token_downstream():
    cases = load_cases("projects/demo/data/order_flow.yaml")
    assert len(cases) == 2
    assert cases[0].scenario == "order-flow"
    assert cases[1].on_failure == "stop"

    captured: list[dict] = []
    client, raw = _flow_client(captured)
    with raw:
        results = run_flow(cases, Context(), client)

    assert len(results) == 2
    assert all(r.succeeded for r in results)
    assert captured[1]["path"] == "http://api.test/orders"
    # 订单步骤收到了登录步骤提取的令牌。
    assert captured[1]["auth"] == "Bearer user-tok-1"


def test_demo_flow_stops_on_first_failure():
    seen = []
    def handler(request):
        seen.append(request.url.path)
        # 登录失败 -> 流程必须在订单步骤之前中止。
        return httpx.Response(401, json={"error": "unauthorized"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as raw:
        client = HTTPClient("http://api.test", client=raw)
        cases = load_cases("projects/demo/data/order_flow.yaml")
        results = run_flow(cases, Context(), client)

    assert len(results) == len(cases)
    assert results[1].status == "skipped"
    assert results[1].attempts == 0
    assert set(seen) == {"/login"}
    assert not results[0].succeeded


def test_flow_passes_case_host_to_client_request():
    """case.host 经 _run_step 透传到 client.request(host=...)（域路由键）。"""
    captured: list[dict] = []

    def handler(request):
        captured.append({"path": str(request.url)}, )
        return httpx.Response(200, json={"token": "t-1"})

    transport = httpx.MockTransport(handler)
    raw = httpx.Client(transport=transport)
    client = HTTPClient(
        "http://default.test",
        client=raw,
        domains={"orders": "https://api.test"},
    )
    cases = [
        Case(
            name="login",
            scenario="host-flow",
            request={"method": "POST", "path": "/bg/login", "host": "orders"},
            assert_={"status_code": 200},
        )
    ]
    with raw:
        results = run_flow(cases, Context(), client)

    assert results[0].succeeded
    assert captured == [{"path": "https://api.test/bg/login"}]
    client.close()
