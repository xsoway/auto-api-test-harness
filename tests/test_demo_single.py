"""Step 10 演示：通过真实 harness 路径端到端运行单接口用例。

加载 ``projects/demo/data/users_single.yaml``，并通过
:func:`ah.core.orchestrator.run_single` 结合桩传输进行驱动，验证完整的
接线：``${user_id}`` 被插值进请求 URL，``fetched_name`` 从响应中提取
到共享上下文里。
"""

import httpx

from ah.core.orchestrator import run_single
from ah.harness.http import HTTPClient
from ah.harness.loader import load_cases
from ah.utils.context import Context


def test_demo_single_case_interpolates_and_extracts():
    cases = load_cases("projects/demo/data/users_single.yaml")
    assert len(cases) == 1
    case = cases[0]
    assert case.name == "get-user-by-id"

    captured: dict[str, str] = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"user": {"id": 42, "name": "Ada Wang"}},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as raw:
        client = HTTPClient("http://api.test", client=raw)
        ctx = Context()
        ctx.set("user_id", 42)
        result = run_single(case, ctx, client)

    assert result.succeeded, result.error
    assert captured["url"] == "http://api.test/users/42"
    # 从响应中提取的结果已写入共享上下文。
    assert ctx.get("fetched_name") == "Ada Wang"
    assert result.extracted == {"fetched_name": "Ada Wang"}
