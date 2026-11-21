"""Step 8 验证：LLM 辅助与代理的隔离行为（LLMOFF）。"""
import httpx
import pytest

from ah.harness.http import HTTPClient
from ah.harness.loader import Case
from ah.llm.agents import AgentDataGen, AgentHeal, AgentTriage
from ah.llm.openai import LLMClient, LLMDisabledError, llm_enabled
from ah.utils.context import Context


@pytest.fixture(autouse=True)
def force_llm_off(monkeypatch):
    monkeypatch.setenv("LLMOFF", "1")


def test_llm_client_raises_when_disabled(monkeypatch):
    monkeypatch.setenv("LLMOFF", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(LLMDisabledError):
        LLMClient()


def test_llm_enabled_flag(monkeypatch):
    monkeypatch.setenv("LLMOFF", "1")
    assert llm_enabled() is False


def _case():
    return Case(
        name="heal-case",
        request={"method": "GET", "path": "/users/1"},
        assert_={"status_code": 200},
    )


def _client():
    def handler(request):
        return httpx.Response(500, json={"err": "boom"})

    transport = httpx.MockTransport(handler)
    return HTTPClient("http://api.test", client=httpx.Client(transport=transport))


def test_heal_hermetic_returns_llm_off_no_network():
    ctx = Context()
    client = _client()
    resp = client.request("GET", "/users/1")  # 返回 500
    heal = AgentHeal(client, ctx)  # 无 LLM -> 走隔离路径
    ok, meta = heal.heal(_case(), resp, AssertionError("x"), 1)
    assert ok is False
    assert meta.get("llm_off") is True
    assert "LLM_OFF" in meta
    client.close()


def test_data_gen_hermetic_returns_empty():
    assert AgentDataGen().generate_request_body({"type": "object"}) == {}


def test_triage_hermetic_canned():
    out = AgentTriage().explain_failure(_case(), None, ValueError("nope"))
    assert out.get("llm_off") is True
    assert "hard failure" in out["root_cause"]




def test_heal_runs_live_when_llm_provided_and_fails_without_openai(monkeypatch):
    # 即使提供假 llm 对象，若构造路径需要 openai，也必须保证 LLMOFF 关闭时安全。
    monkeypatch.setenv("LLMOFF", "1")
    ctx = Context()
    client = _client()
    resp = client.request("GET", "/users/1")

    class FakeLLM:
        def judge(self, task, prompt, model="strong"):
            return {"give_up": True}

    heal = AgentHeal(client, ctx, llm=FakeLLM())  # 提供了 llm 但接近隔离
    ok, meta = heal.heal(_case(), resp, AssertionError("x"), 1)
    assert ok is False
    assert meta.get("action") == "proposal-failed"  # give_up -> proposal-failed
    client.close()


def _history(case_name: str, outcomes: list[str]) -> list[dict]:
    """构造逐运行历史：每 run 一行 ``{cases: {name: result}}``。"""
    return [
        {"run_time": f"t{i}", "cases": {case_name: outcome}}
        for i, outcome in enumerate(outcomes)
    ]


def test_heal_guard_skips_persistent_failure():
    ctx = Context()
    client = _client()
    resp = client.request("GET", "/users/1")  # 500
    # 接口历史 ≥3 次全 FAIL、从无 PASS → 持久性缺陷，跳过 L2 自愈。
    history = _history("heal-case", ["FAIL", "FAIL", "FAIL", "FAIL"])
    heal = AgentHeal(client, ctx, history_rows=history, min_samples=3)
    ok, meta = heal.heal(_case(), resp, AssertionError("x"), 1)
    assert ok is False
    assert meta.get("skipped") == "persistent-failure"
    assert meta.get("action") == "no-heal"
    client.close()


def test_heal_guard_allows_when_history_has_pass():
    ctx = Context()
    client = _client()
    resp = client.request("GET", "/users/1")  # 500
    # 历史中有 PASS → 不判持久性失败，走 llm_off 隔离路径。
    history = _history("heal-case", ["PASS", "FAIL", "FAIL", "FAIL"])
    heal = AgentHeal(client, ctx, history_rows=history, min_samples=3)
    ok, meta = heal.heal(_case(), resp, AssertionError("x"), 1)
    assert ok is False
    assert meta.get("llm_off") is True
    client.close()


def test_heal_guard_requires_min_samples():
    ctx = Context()
    client = _client()
    resp = client.request("GET", "/users/1")  # 500
    # 样本不足（< min_samples）→ 不做激进闸断，走隔离路径。
    history = _history("heal-case", ["FAIL", "FAIL"])
    heal = AgentHeal(client, ctx, history_rows=history, min_samples=3)
    ok, meta = heal.heal(_case(), resp, AssertionError("x"), 1)
    assert ok is False
    assert meta.get("llm_off") is True
    client.close()
