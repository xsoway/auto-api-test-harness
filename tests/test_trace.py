"""Step 6 验证：NullTracer 隔离路径 + HTTP 跨度集成 + Langfuse v4 真实路径。"""
import sys
import types

import httpx

from ah.core import trace as trace_mod
from ah.core.trace import NullTracer, Tracer, get_tracer
from ah.harness.http import HTTPClient


def test_null_tracer_noop_and_disabled():
    t = NullTracer()
    assert not t.enabled
    span = t.start_http("GET", "/x")
    span.finish(status=200, latency_ms=1.0)  # 不得抛异常
    t.observe_failure("step", ValueError("boom"))  # 无操作


def test_get_tracer_returns_null_when_no_credentials(monkeypatch):
    monkeypatch.setenv("AH_TRACE_ENABLED", "1")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    # local_obs 默认开启；此用例验证「全部关闭→null」的降级，故显式关本地链路
    # （settings 是进程级单例，直接改属性；改 env 只对下次 load_settings 生效）。
    monkeypatch.setattr(trace_mod.settings, "local_obs_enabled", False)
    trace_mod._tracer = None
    tracer = get_tracer()
    # 缺少凭据且本地链路关闭时，应优雅降级为 null（绝不导入 langfuse）。
    assert not getattr(tracer, "enabled", False)


def test_http_client_uses_tracer_spans():
    calls = []

    class FakeTracer:
        def start_http(self, method, path):
            calls.append(("http", method, path))
            span = {"done": []}

            def finish(**kw):
                span["done"].append(kw)

            from types import SimpleNamespace
            return SimpleNamespace(finish=finish)

        def observe_failure(self, step_name, exc):
            calls.append(("fail", step_name, str(exc)))

    def handler(request):
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as raw:
        client = HTTPClient("http://x", client=raw, trace=FakeTracer())
        api = client.request("GET", "/p")
    assert api.status_code == 200
    assert calls[0] == ("http", "GET", "/p")


def test_tracer_v4_root_and_span_creation_with_fake_client():
    """Tracer 用 v4 API：root span + 子 observation + finish 字段映射（隔离，不触网）。"""
    created = []

    class FakeSpan:
        """模拟 langfuse v4 的 LangfuseSpan（update/end/start_observation）。"""

        def __init__(self, name):
            self.name = name
            self.updated = {}
            self.ended = False

        def update(self, **kwargs):
            self.updated.update(kwargs)
            return self

        def end(self, **kwargs):
            self.ended = True
            return self

        def start_observation(self, **kwargs):
            span = FakeSpan(kwargs.get("name", ""))
            created.append((self.name, kwargs))
            return span

    class FakeLangfuse:
        def start_observation(self, **kwargs):
            span = FakeSpan(kwargs["name"])
            created.append(("ROOT", kwargs))
            return span

        def flush(self):
            pass

    trace_id = "ab" * 16  # 32 位 hex
    tracer = Tracer(FakeLangfuse(), trace_id, trace_name="my-run")
    assert tracer.enabled
    assert tracer.run_trace_id == trace_id
    # root span 已建（name=可读 trace_name，带 trace_context）
    root_calls = [c for c in created if c[0] == "ROOT"]
    assert root_calls
    assert root_calls[0][1]["name"] == "my-run"
    assert "trace_context" in root_calls[0][1]

    # HTTP span 用 as_type="span"
    hs = tracer.start_http("GET", "/users/1")
    hs.finish(status=200, latency_ms=5)
    http_call = [c for c in created if c[1]["name"] == "HTTP GET /users/1"]
    assert http_call and http_call[0][1]["as_type"] == "span"

    # LLM span 用 as_type="generation" 且带 model
    ls = tracer.start_llm("triage", "gpt-4o")
    ls.finish(prompt="p", response="r", usage={"input": 10, "output": 5}, latency_ms=8)
    llm_call = [c for c in created if c[1]["name"] == "LLM triage"]
    assert llm_call and llm_call[0][1]["as_type"] == "generation"
    assert llm_call[0][1]["model"] == "gpt-4o"


def test_span_finish_maps_fields_to_v4_usage_details():
    """_Span.finish 把 prompt/response/usage 映射到 v4 字段（usage→usage_details）。"""

    class FakeInner:
        def __init__(self):
            self.updated = None
            self.ended = False

        def update(self, **kwargs):
            self.updated = kwargs
            return self

        def end(self, **kwargs):
            self.ended = True
            return self

    from ah.core.trace import _Span

    inner = FakeInner()
    span = _Span(inner)
    span.finish(
        status=200, latency_ms=12.345, prompt="q", response="a",
        usage={"input": 5, "output": 3},
    )
    assert inner.ended
    assert inner.updated is not None
    assert inner.updated["usage_details"] == {"input": 5, "output": 3}
    assert inner.updated["input"] == {"prompt": "q"}
    assert inner.updated["output"] == {"status_code": 200, "response": "a"}
    assert inner.updated["metadata"] == {"latency_ms": 12.35}


def test_get_tracer_use_real_returns_tracer_v4(monkeypatch):
    """三个条件满足时走真实 v4 分支：非降级，返回 Tracer，trace_id 为 32 hex。"""
    # 用假 langfuse 模块替换 sys.modules，确保零网络、零真实 SDK。
    fake = types.ModuleType("langfuse")
    fake_tc = types.ModuleType("langfuse.types")

    class FakeTraceContext:
        def __init__(self, trace_id=None, trace_name=None):
            self.trace_id = trace_id
            self.trace_name = trace_name

    fake_tc.TraceContext = FakeTraceContext

    class FakeLangfuse:
        def __init__(self, public_key="", secret_key="", host=""):
            self.public_key = public_key

        def create_trace_id(self):
            return "ab" * 16  # 合法 32 hex

        def start_observation(self, **kwargs):
            return FakeTraceContext(kwargs.get("trace_context"))

    fake.Langfuse = FakeLangfuse
    fake.types = fake_tc
    sys.modules["langfuse"] = fake
    sys.modules["langfuse.types"] = fake_tc

    # 三项条件满足
    monkeypatch.setattr(trace_mod.settings, "trace_enabled", True)
    monkeypatch.setattr(trace_mod.settings, "trace_public_key", "pk")
    monkeypatch.setattr(trace_mod.settings, "trace_secret_key", "sk")
    monkeypatch.setattr(trace_mod.settings, "trace_host", "http://127.0.0.1:1")
    monkeypatch.setattr(trace_mod.settings, "trace_name", "my-run")
    trace_mod._tracer = None

    tracer = get_tracer()
    assert isinstance(tracer, Tracer)
    assert tracer.enabled
    assert len(tracer.run_trace_id) == 32
    assert all(c in "0123456789abcdef" for c in tracer.run_trace_id)



def test_tracer_trace_url_delegates_to_sdk():
    """Tracer.trace_url 委托 v4 SDK 的 get_trace_url，返回官方格式链接。"""
    class FakeSpan:
        def __init__(self, name):
            self.name = name

        def start_observation(self, **kwargs):
            return FakeSpan(kwargs.get("name", ""))

        def end(self, **kwargs):
            return self

    class FakeLangfuse:
        def __init__(self):
            self.urls = {}
            self.flushed = 0

        def start_observation(self, **kwargs):
            return FakeSpan(kwargs.get("name", ""))

        def get_trace_url(self, trace_id=None):
            return (
                "https://cloud.langfuse.com/project/pj-123/traces/"
                f"{trace_id}"
            )

        def flush(self):
            self.flushed += 1

    lf = FakeLangfuse()
    tracer = Tracer(lf, "ab" * 16, trace_name="my-run")
    url = tracer.trace_url("ab" * 16)
    # 官方格式：/project/{projectId}/traces/{traceId}
    assert url == "https://cloud.langfuse.com/project/pj-123/traces/" + "ab" * 16


def test_null_tracer_trace_url_is_none():
    """追踪禁用时无法生成深度链接，恒返回 None。"""
    assert NullTracer().trace_url("ab" * 16) is None
