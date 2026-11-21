"""用于顶层运行追踪跨度的可选 pytest 钩子。

钩子在会话开始时启动一个 Langfuse 运行跨度，并在会话结束时将其关闭。
它们由 :mod:`ah.plugin` 注册，仅在启用追踪时运行；否则 ``get_tracer()``
返回空跟踪器，这些钩子就退化为廉价的无操作。
"""

from __future__ import annotations

import time
from typing import Any

from ah.core.trace import get_tracer
from ah.utils.logging import get_logger

log = get_logger("trace")


def pytest_sessionstart(session: Any) -> None:
    """记录会话开始的墙钟时间，用于运行跨度。"""
    session._ah_run_start = time.time()  # type: ignore[attr-defined]


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """关闭运行跨度并冲洗缓冲（追踪被禁用时为无操作）。"""
    start = getattr(session, "_ah_run_start", None)
    if start is None:
        return
    tracer = get_tracer()
    if not getattr(tracer, "enabled", False):
        return
    try:
        if exitstatus != 0:
            tracer.observe_failure(
                "session-finish", RuntimeError(f"exitstatus={exitstatus}")
            )
        # v4 必须显式 flush 才会把缓冲的 spans 真正导出到 Langfuse。
        tracer.flush()
    except Exception as exc:  # 追踪绝不能中断运行
        log.warning("trace session-finish error: %s", exc)
