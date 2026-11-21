"""多服务注册表——按领域选 :class:`HTTPClient` 并分派编排。

多服务项目横跨多个网关，每个域常量一个 ``HTTPClient``
（域 = 单一 ``base_url`` + 令牌名）。:class:`ServiceRegistry` 持有
``{host: HTTPClient}``，并按 :attr:`Case.host` 决定把单个用例/流程交给哪个
客户端执行。对 :mod:`ah.core.orchestrator` 保持零改动——编排层仍只认单个
client，跨域路由集中在这里。

用例可通过 ``request.host`` 选择域；缺省（None）回退到默认 ``base_url``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from ah.core.config import DomainSettings, settings
from ah.harness.http import HTTPClient
from ah.harness.loader import Case
from ah.utils.context import Context
from ah.utils.logging import get_logger

if TYPE_CHECKING:
    # 避免 ah.harness -> ah.core.orchestrator 的循环导入：编排层运行时惰性导入。
    from ah.core.orchestrator import LLMHooks, StepResult

log = get_logger("registry")

DEFAULT_HOST = "__default__"

#: 每个域一个 HTTPClient。client 字面（含注入的 httpx transport）交给调用方，
#: 以便测试用 MockTransport 注入；生产路径传 None，由工厂按 base_url 构造。
ClientFactory = Callable[[str, DomainSettings], HTTPClient]


def _default_factory(base_url: str, ds: DomainSettings) -> HTTPClient:
    """按域构造 HTTPClient；令牌经 TokenManager 提供方注入。"""
    return HTTPClient(
        base_url,
        get_token=_from_token_manager(ds.auth) if ds.auth else None,
    )


class ServiceRegistry:
    """按域持有并分派 HTTPClient。"""

    def __init__(
        self,
        domains: Optional[dict[str, DomainSettings]] = None,
        *,
        default_base_url: Optional[str] = None,
        client_factory: Optional[ClientFactory] = None,
        clients: Optional[dict[str, HTTPClient]] = None,
    ) -> None:
        self._domains: dict[str, DomainSettings] = dict(
            settings.domains if domains is None else domains
        )
        self._default_base_url = (
            settings.base_url if default_base_url is None else default_base_url
        )
        self._factory = client_factory or _default_factory
        # 显式传入的客户端优先，否则惰性按域构造。
        self._clients: dict[str, HTTPClient] = dict(clients or {})

    def host(self, case: Case) -> str:
        """用例目标域；缺省回退默认。"""
        return case.host or DEFAULT_HOST

    def client_for(self, key: str) -> HTTPClient:
        """取某域对应的 HTTPClient，惰性创建并缓存。"""
        key = key or DEFAULT_HOST
        if key in self._clients:
            return self._clients[key]
        if key == DEFAULT_HOST:
            if self._default_base_url:
                client = self._factory(self._default_base_url, DomainSettings())
                self._clients[key] = client
                return client
            raise KeyError(
                "no default client configured: case has no request.host and "
                "settings.base_url is empty"
            )
        ds = self._domains.get(key)
        if ds is None:
            raise KeyError(f"unknown service domain {key!r}; configured: {list(self._domains)}")
        client = self._factory(ds.base_url, ds)
        self._clients[key] = client
        return client

    def run_single(
        self,
        case: Case,
        ctx: Context,
        hooks: Optional[LLMHooks] = None,
        tracer: Any = None,
    ) -> StepResult:
        """单步入口也执行严格依赖校验，不允许绕过已声明的前置步骤。"""
        from ah.core.orchestrator import execute_flow

        return execute_flow(
            [case], ctx,
            lambda step, context: self._run_step(step, context, hooks, tracer),
        )[0]

    def _run_step(
        self,
        case: Case,
        ctx: Context,
        hooks: Optional[LLMHooks] = None,
        tracer: Any = None,
    ) -> StepResult:
        """按 case.host 取客户端后执行单个用例。"""
        from ah.core.orchestrator import StepResult, _run_step

        try:
            client = self.client_for(case.host or DEFAULT_HOST)
        except Exception as exc:
            return StepResult(
                name=case.name, succeeded=False, error=exc, attempts=0,
                status="blocked", verdict="unverified", reason=str(exc),
            )
        return _run_step(case, ctx, client, hooks=hooks, tracer=tracer)

    def run_flow(
        self,
        steps: list[Case],
        ctx: Context,
        hooks: Optional[LLMHooks] = None,
        tracer: Any = None,
    ) -> list[StepResult]:
        """按顺序运行，每步按其 host 路由到对应客户端。"""
        from ah.core.orchestrator import execute_flow

        return execute_flow(
            steps, ctx,
            lambda step, context: self._run_step(
                step, context, hooks=hooks, tracer=tracer
            ),
            hooks=hooks,
        )

    def close(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception:  # pragma: no cover - 关闭容错
                pass

    def __enter__(self) -> "ServiceRegistry":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _from_token_manager(auth: str) -> Callable[[Optional[str]], Optional[str]]:
    """从 TokenManager 取令牌的提供方（惰性导入避免循环依赖）。"""
    from ah.environment.token import TokenManager

    manager = TokenManager()

    def _get_token(_name: Optional[str]) -> Optional[str]:
        try:
            return manager.get_token(auth)
        except Exception as exc:  # pragma: no cover - 令牌缺失不阻塞请求
            log.warning("token获取失败 auth=%s: %s", auth, exc)
            return None

    return _get_token
