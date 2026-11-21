"""① 可配置运行重试验证：run_single 对瞬时错误按 retries/retry_backoff 重试。

区别于 L1 自愈——仅对瞬时错误（5xx/超时/连接）重试，断言不匹配等
确定性客户端错误不重试。全部 hermetic（MockTransport、零网络）。
"""
import httpx

from ah.core.orchestrator import run_single
from ah.harness.http import HTTPClient
from ah.harness.loader import Case
from ah.utils.context import Context


def _case(**kw) -> Case:
    return Case(
        name="retry-case",
        request={"method": "GET", "path": "/x"},
        assert_={"status_code": 200},
        retries=kw.get("retries", 0),
        retry_backoff=kw.get("retry_backoff", 0.0),
    )


def _client(handler) -> HTTPClient:
    raw = httpx.Client(transport=httpx.MockTransport(handler))
    return HTTPClient("http://api.test", client=raw)


def _counts(handler, case=None):
    counts = {"n": 0}

    def wrapped(request):
        counts["n"] += 1
        return handler(request, counts["n"])

    client = _client(wrapped)
    res = run_single(case or _case(), Context(), client)
    client.close()
    return counts["n"], res


def test_retry_succeeds_after_transient_500():
    """500→200 且配置 retries=1：首跑 + 1 次可配置重试即成功，记录 retried。"""
    def handler(request, n):
        return httpx.Response(500 if n == 1 else 200, json={"ok": True})

    n, res = _counts(handler, _case(retries=1))
    assert n == 2
    assert res.succeeded
    assert res.llm_notes["retried"]["attempts"] == 1
    assert res.llm_notes["retried"]["configured"] == 1


def test_retry_with_configured_retries_succeeds():
    """配置 retries=2：首次 500 → 重试两次 → 第三次才 200。"""
    def handler(request, n):
        return httpx.Response(200 if n >= 3 else 500, json={"ok": True})

    n, res = _counts(handler, _case(retries=2))
    assert n == 3  # Phase A 首跑 + 2 次重试 = 3 次请求，第三次成功。
    assert res.succeeded
    assert res.llm_notes["retried"]["attempts"] == 2
    assert res.llm_notes["retried"]["configured"] == 2


def test_non_transient_assert_failure_does_not_retry():
    """400（非瞬时）即使配置 retries 也不重试：仅首跑 + L1 一次原样重跑。"""
    def handler(request, n):
        return httpx.Response(400, text="bad request")

    n, res = _counts(handler, _case(retries=5))
    # retries=5 但 4xx 非瞬时 → Phase A 不重试；L1 固定原样重跑一次 → 共 2 次。
    assert n == 2
    assert res.succeeded is False
    assert "retried" not in res.llm_notes


def test_retry_count_exhausted_still_fails():
    """持续 500 且 retries=2：Phase A 尝试 3 次 + L1 1 次 = 4 次请求后失败。"""
    def handler(request, n):
        return httpx.Response(500, text="boom")

    n, res = _counts(handler, _case(retries=2))
    assert n == 4  # Phase A 首跑 + 2 次可配置重试 + L1 1 次原样重跑。
    assert res.succeeded is False
    assert "retried" not in res.llm_notes  # 重试未成功，不记 retried 证据


def test_timeout_is_retryable():
    """超时（TransportError 子类）属于瞬时错误 → 配置 retries 会重试。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("connect timeout")
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    res = run_single(_case(retries=2), Context(), client)
    client.close()
    assert res.succeeded
    assert res.llm_notes["retried"]["attempts"] == 1


def test_loader_parses_retries_and_backoff():
    """用例 YAML 的 retries/retry_backoff 字段被正确解析到 Case。"""
    import tempfile
    from pathlib import Path

    from ah.harness.loader import load_cases

    with tempfile.TemporaryDirectory() as tmp:
        yaml_path = Path(tmp) / "c.yaml"
        yaml_path.write_text(
            "name: t\n"
            "request:\n  method: GET\n  path: /x\n"
            "assert:\n  status_code: 200\n"
            "retries: 3\n"
            "retry_backoff: 0.5\n",
            encoding="utf-8",
        )
        (cases,) = load_cases(str(yaml_path))
        assert cases.retries == 3
        assert cases.retry_backoff == 0.5


def test_loader_defaults_retries_zero():
    """未配置 retries 时默认 0（关闭可配置重试）。"""
    import tempfile
    from pathlib import Path

    from ah.harness.loader import load_cases

    with tempfile.TemporaryDirectory() as tmp:
        yaml_path = Path(tmp) / "c.yaml"
        yaml_path.write_text(
            "name: t\n"
            "request:\n  method: GET\n  path: /x\n"
            "assert:\n  status_code: 200\n",
            encoding="utf-8",
        )
        (cases,) = load_cases(str(yaml_path))
        assert cases.retries == 0
        assert cases.retry_backoff == 0.0
