"""共享令牌维护。

``TokenManager`` 从 JSON 令牌存储（默认 ``config/tokens.json``）加载凭据，
其结构如下::

    {"<env>_token": {"token": "...", "refresh": "<endpoint>"}}

没有 ``refresh`` 端点的令牌被视为静态令牌，永不过期。当存在刷新端点时，
管理器会在令牌过期或接近过期时通过 ``POST <refresh>`` 重新签发
（发送当前令牌以及可选的 ``creds``）。更新的令牌会写回存储，并置于单个
全局锁之后，使多个环境可以安全地共享/复用令牌。

管理器只持有/使令牌状态失效；上下文插值由 :mod:`ah.context` 负责。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

import httpx

from ah.core.config import settings
from ah.utils.logging import get_logger

log = get_logger("token")

DEFAULT_TTL_SECONDS = 25 * 60  # 默认 25 分钟过期
REFRESH_LEAD_SECONDS = 60  # 处于过期前这个时间窗口内时执行刷新

_STORE_LOCK = threading.Lock()


class TokenInvalidError(RuntimeError):
    """当请求的名称没有可用令牌时抛出。"""


def _default_store_path() -> str:
    return settings.token_store_path


def reconcile_token_store(path: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """从磁盘加载令牌存储；缺失/不可读时返回 ``{}``。"""
    p = path or _default_store_path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


class TokenManager:
    """持有令牌状态、刷新逻辑，以及线程安全的存储持久化。"""

    def __init__(
        self,
        store_path: Optional[str] = None,
        *,
        client: Optional[httpx.Client] = None,
        creds: Optional[dict[str, Any]] = None,
    ) -> None:
        self._store_path = store_path or _default_store_path()
        self._client = client
        self._creds = dict(creds or {})
        self._tokens: dict[str, dict[str, Any]] = {}
        self._last_refresh: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        with _STORE_LOCK:
            self._tokens = reconcile_token_store(self._store_path)

    def _save(self) -> None:
        with _STORE_LOCK:
            try:
                with open(self._store_path, "w", encoding="utf-8") as fh:
                    json.dump(self._tokens, fh, indent=2, ensure_ascii=False)
            except OSError as exc:
                log.warning("cannot persist token store %s: %s", self._store_path, exc)

    def has(self, name: str) -> bool:
        return name in self._tokens

    def _entry(self, name: str) -> dict[str, Any]:
        return self._tokens.get(name, {})

    def _expired(self, name: str, entry: dict[str, Any]) -> bool:
        embedded = entry.get("expires_at")
        if isinstance(embedded, (int, float)) and embedded > 0:
            return embedded - time.time() <= REFRESH_LEAD_SECONDS
        last = self._last_refresh.get(name)
        if last is None:
            self._last_refresh[name] = time.time()
            last = self._last_refresh[name]
        return time.time() - last >= DEFAULT_TTL_SECONDS

    def _refresh(self, name: str, entry: dict[str, Any]) -> Optional[str]:
        endpoint = entry.get("refresh")
        if not endpoint:
            return None  # 静态令牌：永不过期
        # 将相对刷新端点解析到配置的 base URL 之下。
        if not endpoint.startswith(("http://", "https://")):
            prefix = settings.base_url.rstrip("/")
            endpoint = prefix + (endpoint if endpoint.startswith("/") else "/" + endpoint)
        payload: dict[str, Any] = {"token": entry.get("token", "")}
        payload.update(self._creds)
        try:
            if self._client is not None:
                resp = self._client.post(endpoint, json=payload, timeout=30.0)
            else:
                resp = httpx.post(endpoint, json=payload, timeout=30.0)
            if resp.status_code < 300:
                data = resp.json()
                new_token = data.get("token") or data.get("access_token")
                if new_token:
                    entry["token"] = new_token
                    if data.get("expires_at"):
                        entry["expires_at"] = data["expires_at"]
                    self._last_refresh[name] = time.time()
                    self._save()
                    return str(new_token)
            log.warning("token refresh failed for %s: %s", name, resp.status_code)
        except httpx.HTTPError as exc:
            log.warning("token refresh error for %s: %s", name, exc)
        return None

    def get_token(self, name: str) -> str:
        """返回 ``name`` 的当前令牌，必要时执行刷新。

        名称未知时抛出 :class:`TokenInvalidError`。
        """
        entry = self._entry(name)
        if not entry:
            raise TokenInvalidError(f"no token entry named {name!r}")
        token = entry.get("token")
        if not token:
            raise TokenInvalidError(f"token entry {name!r} has empty value")
        if entry.get("refresh") and self._expired(name, entry):
            refreshed = self._refresh(name, entry)
            if refreshed:
                return refreshed
            # 刷新不可用/失败：继续提供现有令牌（过期状态）。
            log.warning("serving stale token %r (refresh unavailable)", name)
        return str(token)

    def refresh_if_expired(self, name: str) -> Optional[str]:
        """强制检查过期并刷新；返回当前令牌或 None。"""
        entry = self._entry(name)
        if not entry or entry.get("refresh") is None:
            return entry.get("token")
        if self._expired(name, entry):
            refreshed = self._refresh(name, entry)
            if refreshed:
                return refreshed
        return entry.get("token")
