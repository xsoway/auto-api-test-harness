"""Step 4 验证：TokenManager 静态/刷新/线程安全行为。"""
import json

import httpx
import pytest

from ah.environment.token import TokenInvalidError, TokenManager, reconcile_token_store


def _write_store(tmp_path, data):
    p = tmp_path / "tokens.json"
    p.write_text(json.dumps(data))
    return str(p)


def test_static_token_never_refreshed(tmp_path):
    path = _write_store(tmp_path, {"dev_token": {"token": "static-1"}})
    mgr = TokenManager(path)
    assert mgr.get_token("dev_token") == "static-1"


def test_missing_token_raises(tmp_path):
    path = _write_store(tmp_path, {})
    mgr = TokenManager(path)
    with pytest.raises(TokenInvalidError):
        mgr.get_token("nope")


def test_refresh_when_expired(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"token": "fresh-2", "expires_at": 9999999999})

    p = _write_store(tmp_path, {"dev_token": {"token": "old", "refresh": "/renew"}})
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as raw:
        mgr = TokenManager(p, client=raw)
        # 将上次刷新时间设为许久之前以强制过期。
        mgr._last_refresh["dev_token"] = 0
        assert mgr.get_token("dev_token") == "fresh-2"
        assert mgr._tokens["dev_token"]["token"] == "fresh-2"

    # 更新后的令牌已回写存储。
    persisted = json.loads(open(p).read())
    assert persisted["dev_token"]["token"] == "fresh-2"


def test_refresh_absent_endpoint_static(tmp_path):
    p = _write_store(tmp_path, {"dev_token": {"token": "static"}})
    mgr = TokenManager(p)
    mgr._last_refresh["dev_token"] = 0  # 若可刷新则已过期
    assert mgr.get_token("dev_token") == "static"  # 无刷新：保持静态


def test_reconcile_missing_store_returns_empty(tmp_path):
    assert reconcile_token_store(str(tmp_path / "missing.json")) == {}
