"""② 用例级 timeout 配置验证：case.timeout 透传给 HTTPClient.request。

覆盖 HTTPClient 默认 timeout（缺省 None 用客户端默认 30.0）。全部 hermetic。
"""
import httpx

from ah.core.orchestrator import run_single
from ah.harness.http import HTTPClient
from ah.harness.loader import Case
from ah.utils.context import Context


class _RecClient(httpx.Client):
    """记录每次请求收到的 timeout，便于断言透传。"""

    def __init__(self, transport=None):
        super().__init__(transport=transport)
        self.last_timeout = None

    def request(self, method, url, **kwargs):
        self.last_timeout = kwargs.get("timeout")
        return super().request(method, url, **kwargs)


def _single(timeout):
    return Case(
        name="t",
        request={"method": "GET", "path": "/x"},
        assert_={"status_code": 200},
        timeout=timeout,
    )


def _run(case):
    raw = _RecClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
    )
    client = HTTPClient("http://api.test", client=raw, timeout=30.0)
    res = run_single(case, Context(), client)
    client.close()
    return res, raw.last_timeout


def test_case_timeout_flows_to_client():
    """case.timeout=5.0 → 底层 client 收到 timeout 5.0。"""
    _, got = _run(_single(5.0))
    assert got == 5.0


def test_no_case_timeout_uses_client_default():
    """未配置 case timeout → 底层 client 用 HTTPClient 默认 30.0。"""
    _, got = _run(_single(None))
    assert got == 30.0


def test_timeout_still_passes_assertions():
    """配置 timeout 不影响正常执行与断言结果。"""
    res, _ = _run(_single(2.0))
    assert res.succeeded
    assert res.status_code == 200


def test_loader_parses_top_level_timeout():
    """YAML 顶层 timeout 字段被解析到 Case.timeout。"""
    import tempfile
    from pathlib import Path

    from ah.harness.loader import load_cases

    with tempfile.TemporaryDirectory() as tmp:
        yaml_path = Path(tmp) / "c.yaml"
        yaml_path.write_text(
            "name: t\n"
            "request:\n  method: GET\n  path: /x\n"
            "assert:\n  status_code: 200\n"
            "timeout: 7.5\n",
            encoding="utf-8",
        )
        (cases,) = load_cases(str(yaml_path))
        assert cases.timeout == 7.5


def test_loader_parses_request_timeout_fallback():
    """timeout 也可写在 request 段内（request.timeout 兜底）。"""
    import tempfile
    from pathlib import Path

    from ah.harness.loader import load_cases

    with tempfile.TemporaryDirectory() as tmp:
        yaml_path = Path(tmp) / "c.yaml"
        yaml_path.write_text(
            "name: t\n"
            "request:\n  method: GET\n  path: /x\n  timeout: 3.0\n"
            "assert:\n  status_code: 200\n",
            encoding="utf-8",
        )
        (cases,) = load_cases(str(yaml_path))
        assert cases.timeout == 3.0


def test_loader_defaults_timeout_none():
    """未配置 timeout → Case.timeout 为 None（用客户端默认）。"""
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
        assert cases.timeout is None
