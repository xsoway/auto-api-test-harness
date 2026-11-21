"""Step 3 验证：HTTPClient + APIResponse 统一响应模型。"""
import httpx

from ah.harness.http import APIResponse, HTTPClient


def test_response_model_case_insensitive_headers_and_json():
    resp = httpx.Response(200, headers={"X-Test": "yes"}, text='{"a": 1}')
    api = APIResponse(resp)
    assert api.status_code == 200
    assert api.headers["X-Test"] == "yes"
    assert api.headers["x-test"] == "yes"
    assert api.ok
    assert api.json() == {"a": 1}
    assert api.text == '{"a": 1}'


def test_response_ok_false_on_error():
    assert not APIResponse(httpx.Response(404)).ok


def test_client_injects_base_url_and_auth(read_only_http=None):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"user": "u1"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as raw:
        client = HTTPClient(
            "http://api.test",
            client=raw,
            get_token=lambda name: "tok-123",
            default_headers={"X-Env": "dev"},
        )
        api = client.request("GET", "/users/1", auth="default_token")

    assert captured["url"] == "http://api.test/users/1"
    assert captured["auth"] == "Bearer tok-123"
    assert api.status_code == 200
    assert api.json() == {"user": "u1"}
    client.close()


def test_client_omits_auth_without_provider():
    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as raw:
        client = HTTPClient("http://api.test", client=raw)
        api = client.request("GET", "/health")
    assert api.status_code == 204
    client.close()


def test_client_absolute_path_ignores_base_url_and_keeps_params():
    """path 为绝对 URL 时直接使用，不拼 base_url，且保留 query 参数。"""
    captured: dict[str, str] = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, text="<html>ok</html>")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as raw:
        client = HTTPClient("https://ignored.example.com", client=raw)
        api = client.request("GET", "https://m.example.test/home", params={"a": "1"})

    assert api.status_code == 200
    assert captured["url"] == "https://m.example.test/home?a=1"
    client.close()


def test_client_domain_routing_uses_matching_domain_base_url():
    """host 命中 domains 映射时，用对应域 base_url 拼 URL。"""
    captured: dict[str, str] = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as raw:
        client = HTTPClient(
            "http://default.example.com",
            client=raw,
            domains={"orders": "https://api.example.cn", "inventory": "http://inventory.example.test"},
        )
        api = client.request("POST", "/bg/login", host="orders")

    assert api.status_code == 200
    assert captured["url"] == "https://api.example.cn/bg/login"
    client.close()


def test_client_domain_routing_falls_back_when_host_unknown():
    """host 未命中 domains 映射时，回退 self.base_url（原行为不变）。"""
    captured: dict[str, str] = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as raw:
        client = HTTPClient(
            "http://default.example.com",
            client=raw,
            domains={"orders": "https://api.example.cn"},
        )
        api = client.request("GET", "/health", host="unknown")

    assert api.status_code == 200
    assert captured["url"] == "http://default.example.com/health"
    client.close()


def test_client_domain_routing_omitted_host_keeps_default():
    """不传 host（旧调用方）时字节等价于未配置 domains。"""
    captured: dict[str, str] = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as raw:
        client = HTTPClient(
            "http://default.example.com",
            client=raw,
            domains={"orders": "https://api.example.cn"},
        )
        api = client.request("GET", "/health")

    assert api.status_code == 200
    assert captured["url"] == "http://default.example.com/health"
    client.close()
