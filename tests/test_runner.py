"""Step 7 验证：run_single / run_flow 编排（隔离运行）。"""
import httpx

from ah.core.orchestrator import LLMHooks, run_flow, run_single
from ah.harness.http import HTTPClient
from ah.utils.context import Context


def _client(handler):
    transport = httpx.MockTransport(handler)
    raw = httpx.Client(transport=transport)
    return HTTPClient("http://api.test", client=raw), raw


def _single_case(path="/users/${user_id}", method="GET", assert_status=200, **kw):
    from ah.harness.loader import Case

    return Case(
        name="single",
        request={
            "method": method,
            "path": path,
            "json": kw.get("json"),
            "params": kw.get("params"),
            "headers": kw.get("headers"),
        },
        extract=kw.get("extract", {}),
        assert_={"status_code": assert_status, **kw.get("assert_", {})},
        auth=kw.get("auth"),
        llm_gen=kw.get("llm_gen", {}),
        on_failure=kw.get("on_failure", "stop"),
    )


def test_single_pass_interpolates_and_extracts():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["json"] = request.content
        return httpx.Response(200, json={"id": 7, "name": "amy"})

    client, raw = _client(handler)
    ctx = Context({"user_id": 5})
    case = _single_case(
        path="/users/${user_id}",
        json={"uid": "${user_id}"},
        extract={"uid": {"path": "id"}},
    )
    res = run_single(case, ctx, client)
    raw.close()

    assert res.succeeded
    assert seen["url"] == "http://api.test/users/5"
    assert b'"uid":"5"' in seen["json"].replace(b" ", b"")
    assert ctx.get("uid") == 7
    assert res.extracted == {"uid": 7}
    assert res.status_code == 200


def test_single_assert_failure_captures_error():
    def handler(request):
        return httpx.Response(500, json={"msg": "boom"})

    client, raw = _client(handler)
    ctx = Context()
    res = run_single(_single_case(path="/x"), ctx, client)
    raw.close()

    assert not res.succeeded
    assert res.error is not None
    assert "{status_code} expected 200, got 500" in str(res.error)


def test_flow_ordering_and_ctx_carryover():
    order = []

    def handler(request):
        order.append(str(request.url))
        if "users" in request.url.path:
            return httpx.Response(200, json={"token": "tok-abc"})
        # /orders 步骤从 ctx 读取 user_token
        assert b"tok-abc" in request.content
        return httpx.Response(201, json={"order_id": 99})

    client, raw = _client(handler)
    ctx = Context()
    step1 = _single_case(
        path="/users",
        method="POST",
        assert_status=200,
        extract={"user_token": {"path": "token"}},
    )
    step2 = _single_case(
        path="/orders",
        method="POST",
        json={"token": "${user_token}"},
        assert_status=201,
        extract={"oid": {"path": "order_id"}},
    )
    results = run_flow([step1, step2], ctx, client)
    raw.close()

    assert [r.succeeded for r in results] == [True, True]
    assert order == ["http://api.test/users", "http://api.test/orders"]
    assert ctx.get("user_token") == "tok-abc"
    assert ctx.get("oid") == 99


def test_on_failure_stop_halts():
    seen = []
    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(500, json={})

    client, raw = _client(handler)
    ctx = Context()
    s1 = _single_case(path="/a", assert_status=200)
    s2 = _single_case(path="/b", assert_status=200)
    results = run_flow([s1, s2], ctx, client)
    raw.close()

    assert [result.status for result in results] == ["executed", "skipped"]
    assert results[1].attempts == 0
    assert set(seen) == {"/a"}
    assert results[0].succeeded is False


def test_on_failure_continue_proceeds():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    client, raw = _client(handler)
    ctx = Context()
    s1 = _single_case(path="/a", assert_status=200)
    s2 = _single_case(path="/b", assert_status=200, on_failure="continue")
    results = run_flow([s1, s2], ctx, client)
    raw.close()

    assert len(results) == 2
    assert [r.succeeded for r in results] == [True, True]


def test_data_gen_hook_injects_params():
    def handler(request):
        return httpx.Response(200, json={})

    client, raw = _client(handler)
    ctx = Context()
    case = _single_case(path="/gen", json={}, llm_gen={"schema": "x"})
    called = {}

    def data_gen(c, ctx):
        called["case"] = c.name
        return {"ai_seed": 42}

    hooks = LLMHooks(data_gen=data_gen)
    res = run_single(case, ctx, client, hooks=hooks)
    raw.close()

    assert res.succeeded
    assert called["case"] == "single"
    assert res.llm_notes["data_gen"] == {"ai_seed": 42}
