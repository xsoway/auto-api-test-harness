"""HTTP 传输层——纯 HTTP，无 LLM 耦合。

``HTTPClient`` 包装 ``httpx`` 并返回统一的 :class:`APIResponse`。
它注入配置好的 base URL、全局头，以及（当提供令牌提供方时）每次调用的
全新 Authorization 头。每个请求都会以方法、路径、状态、耗时和截断的
正文记录日志。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

import httpx

from ah.utils.logging import get_logger

log = get_logger("client")

_LOG_BODY_LIMIT = 2048


class APIResponse:
    """统一的响应模型。``headers`` 支持大小写不敏感的访问。"""

    __slots__ = ("_resp", "validation", "latency_ms")

    def __init__(self, resp: httpx.Response) -> None:
        self._resp = resp
        # 预留给架构/断言验证结果。
        self.validation: Optional[dict[str, Any]] = None
        # 单次请求往返耗时（毫秒），由 HTTPClient 在返回前填充。
        self.latency_ms: Optional[float] = None

    @property
    def status_code(self) -> int:
        return self._resp.status_code

    @property
    def headers(self) -> httpx.Headers:
        return self._resp.headers

    @property
    def text(self) -> str:
        return self._resp.text

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._resp.cookies)

    @property
    def ok(self) -> bool:
        """HTTP 层成功（2xx 或配置的允许列表）。"""
        return 200 <= self._resp.status_code < 300

    def json(self, **kwargs: Any) -> Any:
        return self._resp.json(**kwargs)

    @property
    def underlying(self) -> httpx.Response:
        return self._resp

    def __repr__(self) -> str:
        return f"<APIResponse {self._resp.status_code} {len(self._resp.content)} bytes>"


class HTTPClient:
    """近似无状态客户端；每次运行的一个实例共享一个令牌提供方。"""

    def __init__(
        self,
        base_url: str,
        *,
        client: Optional[httpx.Client] = None,
        get_token: Optional[Callable[[Optional[str]], Optional[str]]] = None,
        default_headers: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
        trace: Any = None,
        domains: Optional[dict[str, str]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._get_token = get_token
        self._default_headers = dict(default_headers or {})
        self._timeout = timeout
        self._trace = trace
        # 多服务域路由：``domains`` 为 {域名: base_url}；request(host=...) 命中时
        # 用对应 base_url，否则回退 self.base_url。缺省 None → 原单域行为不变。
        self._domains = dict(domains or {})
        # 默认跟随 3xx 重定向：页面巡检（如 M 站缺 city/storeId 参数时服务端 307
        # 跳转）期望最终 200；测试用 MockTransport 注入自建 client，不受影响。
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Any = None,
        data: Any = None,
        headers: Optional[dict[str, str]] = None,
        auth: Optional[str] = None,
        timeout: Optional[float] = None,
        host: Optional[str] = None,
    ) -> APIResponse:

        """发送一个请求并返回统一的 :class:`APIResponse`。

        ``auth`` 是传给令牌提供方的令牌名；设置后，会自动把
        ``Authorization: Bearer <token>`` 头合并到请求上。
        """
        start = time.perf_counter()
        # 绝对 URL（页面/静态资源等）自带完整地址，直接使用；否则拼上 base_url。
        # host 命中 domains 映射时用该域 base_url（多服务路由），否则回退 self.base_url。
        if path.startswith(("http://", "https://")):
            url = path
        else:
            base = self._domains.get(host) if host else None
            url = f"{(base or self.base_url)}{path}"
        merged = dict(self._default_headers)
        if headers:
            merged.update(headers)
        if auth and self._get_token is not None:
            token = self._get_token(auth)
            if token:
                merged["Authorization"] = f"Bearer {token}"
        effective_timeout = timeout if timeout is not None else self._timeout

        span = None
        if self._trace is not None:
            span = self._trace.start_http(method, path)

        resp: Optional[httpx.Response] = None
        try:
            resp = self._client.request(
                method,
                url,
                params=params,
                json=json,
                data=data,
                headers=merged,
                timeout=effective_timeout,
            )
            api_resp = APIResponse(resp)
        except httpx.TimeoutException:
            raise
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            status = resp.status_code if resp is not None else None
            body = self._truncate(resp.text if resp is not None else "")
            log.info(
                "%s %s -> %s (%dms) body=%.500r",
                method.upper(),
                path,
                status,
                latency_ms,
                body,
            )
            if span is not None:
                span.finish(status=status, latency_ms=latency_ms)


        # 全程无异常时，把往返耗时挂到响应上（供报告/诊断使用）。
        api_resp.latency_ms = latency_ms
        return api_resp

    @staticmethod
    def _truncate(body: str) -> str:
        return body if len(body) <= _LOG_BODY_LIMIT else body[:_LOG_BODY_LIMIT] + "…"

    def close(self) -> None:
        self._client.close()
