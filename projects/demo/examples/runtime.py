"""Run the order and execution-policy examples with local HTTP fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
import yaml

from ah.core.orchestrator import LLMHooks, run_flow, run_single
from ah.failure.governor import FailureGovernor
from ah.failure.observe import AgentObserve, observe_retry_original
from ah.harness.http import HTTPClient
from ah.harness.loader import Case, load_cases
from ah.llm import MockLLM
from ah.llm.agents import AgentHeal, AgentTriage
from ah.property import explore_properties
from ah.skill.executor import execute_flow
from ah.skill.model import build_registry
from ah.utils.context import Context

DEMO = Path(__file__).resolve().parents[1]


def order_flow() -> None:
    calls = []
    login_ok = True

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if path == "/login":
            if not login_ok:
                return httpx.Response(401, json={"error": "login_failed"})
            return httpx.Response(200, json={"token": "demo-token"})
        assert request.headers["Authorization"] == "Bearer demo-token"
        if path == "/orders":
            assert json.loads(request.content)["item_id"] == "5"
            return httpx.Response(201, json={"order": {"id": 100}})
        assert path == "/orders/100"
        return httpx.Response(200, json={"order": {"id": 100}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as raw:
        client = HTTPClient("https://orders.demo.test", client=raw)
        spec = yaml.safe_load((DEMO / "flows" / "order-create.yaml").read_text(encoding="utf-8"))
        ctx = Context()
        results = execute_flow(spec["flow"], build_registry(DEMO), client, base_ctx=ctx)
        assert [result.verdict for result in results] == ["passed"] * 3, [
            (result.name, result.error) for result in results
        ]
        assert ctx.get("user_token") == "demo-token" and ctx.get("order_id") == 100
        assert calls == [("POST", "/login"), ("POST", "/orders"), ("GET", "/orders/100")]
        print("Skill 组合：登录 → 下单（Bearer demo-token）→ 查询订单 100；3 步通过")

        calls.clear()
        login_ok = False
        results = run_flow(load_cases(DEMO / "data" / "order_flow.yaml"), Context(), client)
        assert not results[0].succeeded and results[0].status_code == 401
        assert results[1].status == "skipped" and results[1].attempts == 0
        assert calls and all(path == "/login" for _, path in calls)
        print("on_failure=stop：登录 HTTP 401；下单 skipped，实际下单请求 0 次")


def policies() -> None:
    observed_limits = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/slow":
            assert request.extensions["timeout"]["read"] == 0.05
            raise httpx.ReadTimeout("simulated read timeout", request=request)
        if path == "/unstable":
            observed_limits.append(request.url.params["limit"])
            if len(observed_limits) <= 2:
                return httpx.Response(503)
        limit = int(request.url.params.get("limit", 10))
        if limit < 1:
            return httpx.Response(400)
        return httpx.Response(200, json={"returned": 9 if path == "/defect" else limit})

    with httpx.Client(transport=httpx.MockTransport(handle)) as raw:
        client = HTTPClient("https://catalog.demo.test", client=raw)
        expect = {"status_code": 200, "jsonpath": {"returned": {"eq": 10}}}

        slow = Case("slow", {"method": "GET", "path": "/slow"}, assert_=expect, timeout=0.05)
        timed = run_single(slow, Context(), client)
        assert isinstance(timed.error, httpx.ReadTimeout)
        print("超时：read=0.05 秒已传给 HTTPX；模拟读超时被记录")

        defect = Case("defect", {"method": "GET", "path": "/defect"}, assert_=expect)
        soft_case = Case("soft", defect.request, assert_=expect, soft_assert=True)
        soft = run_single(soft_case, Context(), client)
        assert soft.succeeded and soft.llm_notes["soft_failures"] == 1
        assert soft.verdict == "failed"
        print(f"软断言：流程继续，记录 1 条断言失败；验真={soft.verdict}")

        prop = Case(
            "explore-limit",
            {"method": "GET", "path": "/products", "params": {"limit": 10}},
            assert_={"status_code": 200},
        )
        first = explore_properties(prop, examples=8, seed=42, client=client)
        second = explore_properties(prop, examples=8, seed=42, client=client)
        samples = [(sample.params, sample.passed) for sample in first.samples]
        assert samples == [(sample.params, sample.passed) for sample in second.samples]
        assert first.examples_run == 8 and 0 < first.failed < first.examples_run
        print(f"属性探索：seed=42；8 个样本及判定可复现，失败 {first.failed} 个")
        print(f"  失败样本：{next(params for params, passed in samples if not passed)}")

        llm = MockLLM()
        llm.set_json_response({"category": "product_defect"})
        blocked = run_single(
            defect, Context(), client,
            hooks=LLMHooks(triage=AgentTriage(llm).explain_failure),
            governor=FailureGovernor(),
        )
        assert blocked.llm_notes["failure_governance"]["action"] == "block"
        assert not blocked.succeeded and "healed" not in blocked.llm_notes
        print("阻断：HTTP 200 但返回 9，业务期望 10；原始失败保留")

        unstable = Case(
            "unstable",
            {"method": "GET", "path": "/unstable", "params": {"limit": 10}},
            assert_=expect,
        )
        observer = AgentObserve(client, retry_original=observe_retry_original, max_steps=1)
        recovered = run_single(
            unstable, Context(), client, governor=FailureGovernor(observe=observer),
        )
        note = recovered.llm_notes["recovered"]
        assert recovered.llm_notes["failure_governance"]["action"] == "observe"
        assert note["mode"] == "retry-original" and not note["wrote_script"]
        assert recovered.succeeded and recovered.verdict == "failed"
        assert observed_limits == ["10", "10", "10"]
        print(f"观察恢复：503 → 503 → 200；原样请求，未修改脚本；验真={recovered.verdict}")

        stale = Case(
            "stale-input",
            {"method": "GET", "path": "/products", "params": {"limit": 0}},
            assert_=expect,
        )
        llm.set_json_response({"category": "script_bug"})
        llm.set_json_response({"request": {"params": {"limit": 10}}})
        ctx = Context()
        healed = run_single(
            stale, ctx, client, governor=FailureGovernor(),
            hooks=LLMHooks(
                triage=AgentTriage(llm).explain_failure,
                heal=AgentHeal(client, ctx, llm).heal,
            ),
        )
        assert healed.llm_notes["failure_governance"]["action"] == "heal"
        assert healed.llm_notes["healed"]["meta"]["validated_by"] == "re-run"
        assert healed.succeeded and healed.verdict == "failed"
        assert stale.request["params"] == {"limit": 0} and stale.assert_ == expect
        print(f"脚本自愈：回放候选 limit=0 → 10，候选重跑通过；原用例验真={healed.verdict}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("example", choices=("flows", "policies"))
    (order_flow if parser.parse_args().example == "flows" else policies)()
