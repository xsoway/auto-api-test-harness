#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15
# @Filename : observe.py
# @Author   : Alan_Hsu
"""非侵入临时恢复（observe 动作的执行核心）。

对应《别让自愈把红灯洗绿》步骤2：observe 问题尝试做无侵入的临时恢复，
例如重登录、刷新、重试当前步骤。关键词是「无侵入」：

- 成功后**只重跑原步骤，不回写脚本**；
- 失败则进入隔离观察流程（交给簇群状态机累积证据），不改脚本。

与 AgentHeal（生成修复候选、验证后写回）的本质区别：observe 不产生任何
脚本变更，只是让"临时故障"在不过度修改用例的前提下自证恢复。

恢复步骤本身也是确定性探测器：保证在 LLMOFF / 无后端时纯本地、可回退、
绝不制造网络副作用。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from ah.harness.http import HTTPClient
from ah.utils.logging import get_logger

log = get_logger("observe")

#: 临时恢复动作集合（供报告展示用了哪种恢复）。
OBSERVE_RETRY = "retry-original"      # 原样重试当前步骤
OBSERVE_REAUTH = "re-auth"            # 重建令牌/会话后重试
OBSERVE_RECONNECT = "reconnect"       # 重连/换连接


class AgentObserve:
    """observe 动作的临时恢复器：按尽力而为顺序尝试，全部无侵入。

    构造参数：
    - ``client``：HTTPClient（用于重建令牌/重连）。
    - ``retry_once``：可覆盖的"原样重试一次"探测器；缺省用内置的
      ``run_single`` 原样重跑（见 :func:`observe_retry_original`）。
    - ``max_steps``：最多尝试多少种恢复动作。

    ``recover(case, resp, error, ctx, attempt)`` 逐动作尝试，任一成功即返回
    ``{"recovered": True, "mode": <动作>, ...}``；全部失败返回 ``{"recovered":
    False, "tried": [...]}``。绝不写回脚本、绝不抛异常。
    """

    def __init__(
        self,
        client: HTTPClient,
        *,
        retry_original: Optional[Callable[..., tuple[bool, Any]]] = None,
        max_steps: int = 3,
        token_manager: Any = None,
    ) -> None:
        self._client = client
        self._retry_original = retry_original
        self._max_steps = max(1, int(max_steps))
        self._token_manager = token_manager

    def recover(
        self,
        case: Any,
        resp: Any,
        error: BaseException,
        ctx: Any,
        attempt: int,
    ) -> dict[str, Any]:
        """按顺序尝试恢复动作；任一成功即返回恢复结果，不再尝试其余。"""
        tried: list[str] = []
        # 步骤 1：原样重试当前步骤（最无侵入）。
        if self._max_steps >= 1:
            tried.append(OBSERVE_RETRY)
            try:
                ok, _ = self._retry_original(case, ctx, self._client)
            except Exception as ro:  # noqa: BLE001 — 探测器失败不入恢复链
                log.debug("observe retry-original failed: %s", ro)
                ok = False
            if ok:
                return self._success(OBSERVE_RETRY, case, error, attempt)

        # 步骤 2：重建令牌/会话后重试（环境/鉴权问题常用）。
        if self._max_steps >= 2 and self._token_manager is not None:
            tried.append(OBSERVE_REAUTH)
            try:
                self._token_manager.refresh()
                ok, _ = self._retry_original(case, ctx, self._client)
            except Exception as ra:  # noqa: BLE001
                log.debug("observe re-auth failed: %s", ra)
                ok = False
            if ok:
                return self._success(OBSERVE_REAUTH, case, error, attempt)

        # 步骤 3：重连（换连接/重试）后重跑。
        if self._max_steps >= 3:
            tried.append(OBSERVE_RECONNECT)
            try:
                self._client.close()
                ok, _ = self._retry_original(case, ctx, self._client)
            except Exception as rc:  # noqa: BLE001
                log.debug("observe reconnect failed: %s", rc)
                ok = False
            if ok:
                return self._success(OBSERVE_RECONNECT, case, error, attempt)

        return {"recovered": False, "tried": tried, "error": str(error)}

    @staticmethod
    def _success(mode: str, case: Any, error: BaseException, attempt: int) -> dict[str, Any]:
        """构造恢复成功结果（携带无侵入语义标记）。"""
        return {
            "recovered": True,
            "mode": mode,
            "wrote_script": False,       # 铁律：恢复永不回写脚本
            "case": case.name if hasattr(case, "name") else str(case),
            "original_error": str(error),
            "attempt": attempt,
        }


def observe_retry_original(case: Any, ctx: Any, client: HTTPClient) -> tuple[bool, Any]:
    """内置"原样重试"探测器：复用 run_single 复用原用例重跑，无脚本变更。

    惰性导入 run_single 避免 observe 层与 orchestrator 硬耦合：
    任何实现（含测试注入的 fake）都可替换。
    """
    from ah.core.orchestrator import run_single

    result = run_single(case, ctx, client)
    return bool(result.succeeded), result
