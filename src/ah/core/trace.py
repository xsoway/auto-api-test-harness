#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/09 16:20
# @Filename : trace.py
# @Author   : Alan_Hsu
"""运行过程追踪：Langfuse 导出 + 本地 SQLite 双写。

:class:`Tracer` 把 HTTP/LLM/失败/自愈/调度/用例 等活动包装成一条运行 trace 下的
层级跨度（run → case → step → HTTP/LLM）。它支持两个并行的 sink：

- **Langfuse**（官方）：``settings.trace_enabled`` 为真且凭据存在时，经
  langfuse-python v4 SDK 导出（observations-first 模型）。隔离路径绝不触碰网络。
- **本地 SQLite**（local-obs）：``settings.local_obs_enabled`` 为真时，把每个
  span 实时 ``upsert_observation`` 写入 ``settings.local_obs_db``，形成
  run→case→step→HTTP/LLM 父子树，供本地平台展示运行过程链路。

两个 sink 各自独立开关、互不影响；写本地库失败仅告警，绝不中断运行
（与 Langfuse exports 同哲学）。分层通过线程局部上下文 ``_Ctx`` 传递当前
run/parent：``start_http``/``start_llm`` 等叶子自动挂到最近一次
``push_context`` 规定的父节点下，调用方无需逐层改签名。

跨度 API（由 ``HTTPClient``/LLM 调用/编排器共用）：
- ``start_http(method, path)`` -> 跨度，带 ``finish(status=..., latency_ms=...)``
- ``start_llm(task, model)`` -> 跨度，带 ``finish(..., prompt=, response=, usage=)``
- ``start_case(name)`` / ``start_step(name)`` -> 层级容器跨度
- ``start_dispatch(text, label)`` -> 意图调度跨度
- ``observe_failure(step_name, exc)`` / ``record_heal(...)`` -> 失败治理节点
- ``push_context(run_id, parent_id)`` / ``pop_context()`` -> 分层 parent 传递
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from ah.core.config import settings
from ah.observe.store import ObserveStore, open_store
from ah.utils.logging import get_logger

log = get_logger("trace")


def _snowflake_id() -> str:
    # 类 Snowflake 风格：<timestamp_ms>.<seq> —— 足够作为独立的 trace 锚点。
    now = time.time()
    return f"{int(now * 1000)}.{int((now * 1e6) % 1e6)}"


class _Ctx(threading.local):
    """线程局部：当前 run_id 与 parent observation id 栈。

    ``start_case``/``start_step`` 压栈，``_LocalSpan.finish`` 出栈；叶子跨度
    (HTTP/LLM) 读取栈顶作为 parent，从而自然形成 run→case→step 层级。
    """

    def __init__(self) -> None:
        self.run_id: Optional[str] = None
        self.parent_id: Optional[str] = None
        # 保存 parent_id 的栈，供 finish 时恢复（case/step 结束回退到更外层）。
        self._parents: list[Optional[str]] = []


_CTX = _Ctx()


class _TraceSpan:
    """由 NullTracer/HTTP 返回的最小跨度；总是成功。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.start = time.time()

    def finish(self, **fields: Any) -> None:
        pass


class NullTracer:
    """当追踪完全被禁用时使用的空操作跟踪器（无双写、无本地库）。"""

    @property
    def enabled(self) -> bool:
        return False

    def start_http(self, method: str, path: str) -> _TraceSpan:
        return _TraceSpan(f"HTTP {method} {path}")

    def start_llm(self, task: str, model: str) -> _TraceSpan:
        return _TraceSpan(f"LLM {task}")

    def start_case(self, name: str) -> _TraceSpan:
        return _TraceSpan(f"case:{name}")

    def start_step(self, name: str) -> _TraceSpan:
        return _TraceSpan(f"step:{name}")

    def start_dispatch(self, text: str, label: str = "") -> _TraceSpan:
        return _TraceSpan(f"dispatch:{label}")

    def trace_url(self, trace_id: str) -> Optional[str]:
        """追踪被禁用时无链接可生成，恒返回 None。"""
        return None

    def observe_failure(self, step_name: str, exc: BaseException) -> None:
        pass

    def record_heal(
        self,
        step_name: str,
        *,
        level: str,
        mode: str,
        succeeded: bool,
        meta: Optional[dict[str, Any]] = None,
    ) -> None:
        pass

    def push_context(self, run_id: Optional[str] = None, parent_id: Optional[str] = None) -> None:
        pass

    def pop_context(self) -> None:
        pass


class Tracer:
    """基于 Langfuse + 本地 SQLite 的跟踪器。惰性构造；静态/类风格工厂。

    ``langfuse`` 可为 None（仅本地双写）；``store`` 可为 None（仅 Langfuse 或纯
    Langfuse）。任一 sink 存在即视为 enabled。两个 sink 写失败都仅告警。
    """

    def __init__(
        self,
        langfuse: Any,
        trace_id: str,
        trace_name: Optional[str] = None,
        store: Optional[ObserveStore] = None,
        store_db: Optional[str] = None,
    ) -> None:
        self._langfuse = langfuse
        self._store = store
        self._store_db = store_db
        self.run_trace_id = trace_id
        self._trace_name = trace_name or trace_id
        self._local_run_id: Optional[str] = None
        self._local_start_ms = int(time.time() * 1000)
        if langfuse is not None:
            # v4 模型：用一个 root span 作为本次运行的 trace 锚点，通过 TraceContext
            # 钉住 trace_id（保持“整个 pytest 运行同一条 trace”的设计）。
            from langfuse.types import TraceContext

            # v4 要求 trace_id 是合法 32 位小写十六进制；trace_name 作可读标签。
            self._ctx = TraceContext(trace_id=trace_id, trace_name=trace_name or trace_id)
            self._root = langfuse.start_observation(
                name=trace_name or trace_id, as_type="span", trace_context=self._ctx
            )
        else:
            self._ctx = None
            self._root = None

    @property
    def enabled(self) -> bool:
        return True

    def flush(self) -> None:
        """把缓冲的 spans 显式导出到 Langfuse（v4 需要调用才会真正上报）。"""
        if self._langfuse is None:
            return
        try:
            self._langfuse.flush()
        except Exception as exc:  # 追踪绝不能中断运行
            log.warning("trace flush error: %s", exc)

    def trace_url(self, trace_id: str) -> Optional[str]:
        """返回官方 trace 深度链接（``/project/{projectId}/traces/{id}``）。"""
        if self._langfuse is None:
            return None
        try:
            fn = getattr(self._langfuse, "get_trace_url", None)
            if fn is None:
                return None
            return fn(trace_id=trace_id)
        except Exception as exc:  # 生成链接失败不影响运行
            log.warning("trace_url failed: %s", exc)
            return None

    # ---- 本地 run 生命周期 ----
    def _ensure_store(self) -> Optional[ObserveStore]:
        """惰性打开本地 store（若配置开启）。失败返回 None，绝不抛出。"""
        if self._store is None and self._store_db:
            try:
                self._store = open_store(self._store_db)
            except Exception as exc:  # 本地上报失败不影响运行
                log.warning("open local-obs store failed: %s", exc)
                self._store = None
        return self._store

    def _ensure_local_run(self) -> None:
        """首次写入时惰性建 run 行（id = live:<snowflake>，source_type=live）。"""
        store = self._ensure_store()
        if store is None or self._local_run_id is not None:
            return
        self._local_run_id = f"live:{_snowflake_id()}"
        try:
            from datetime import datetime

            store.upsert_run({
                "id": self._local_run_id,
                "name": self._trace_name,
                "start_time": datetime.now().isoformat(timespec="seconds"),
                "source_type": "live",
            })
        except Exception as exc:  # 建 run 失败仅告警，链路仍尽量落。
            log.warning("upsert live run failed: %s", exc)

    def _local_parent(self) -> tuple[Optional[str], Optional[str]]:
        """返回当前线程局部的 (run_id, parent_id)。"""
        store = self._ensure_store()
        if store is None:
            return None, None
        self._ensure_local_run()
        return _CTX.run_id or self._local_run_id, _CTX.parent_id

    def _write_local_obs(
        self,
        name: str,
        *,
        otype: str,
        start_ms: float,
        latency_ms: Optional[float] = None,
        level: str = "INFO",
        status: str = "ok",
        model: str = "",
        task: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: float = 0.0,
        input: Any = None,
        output: Any = None,
        metadata: Optional[dict[str, Any]] = None,
        source: str = "live",
    ) -> Optional[str]:
        """写一条本地 observation；返回其 id（写失败返回 None，绝不抛出）。"""
        store = self._ensure_store()
        if store is None:
            return None
        self._ensure_local_run()
        run_id = _CTX.run_id or self._local_run_id
        parent_id = _CTX.parent_id
        oid = f"{run_id}:span:{_snowflake_id()}"
        try:
            store.upsert_observation({
                "id": oid,
                "run_id": run_id,
                "parent_id": parent_id,
                "name": name,
                "type": otype,
                "start_ms": round(start_ms, 3),
                "latency_ms": round(latency_ms or 0.0, 3),
                "level": level,
                "status": status,
                "model": model,
                "task": task,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost": cost,
                "input": input,
                "output": output,
                "metadata": metadata or {},
                "source": source,
            })
        except Exception as exc:  # 本地上报失败绝不中断运行
            log.warning("write local obs failed (%s): %s", name, exc)
            return None
        return oid

    # ---- 层级上下文 ----
    def push_context(self, run_id: Optional[str] = None, parent_id: Optional[str] = None) -> None:
        """进入一个层级：设置 run_id（可选）并把父节点压栈。"""
        _CTX._parents.append(_CTX.parent_id)
        if run_id is not None:
            _CTX.run_id = run_id
        _CTX.parent_id = parent_id

    def pop_context(self) -> None:
        """退出层级：恢复上一层的 parent（出栈）。"""
        if _CTX._parents:
            _CTX.parent_id = _CTX._parents.pop()

    # ---- 容器跨度：case / step / dispatch ----
    def start_case(self, name: str) -> "_LocalSpan":
        """开启一个用例跨度；返回的 finish() 会回退 parent 层级。"""
        return self._start_container("case:" + (name or "case"), _CTX.parent_id)

    def start_step(self, name: str) -> "_LocalSpan":
        """开启一个 step 跨度；返回的 finish() 会回退 parent 层级。"""
        return self._start_container("step:" + (name or "step"), _CTX.parent_id)

    def _start_container(self, name: str, parent: Optional[str]) -> "_LocalSpan":
        start_ms = time.time()
        oid = self._write_local_obs(name, otype="SPAN", start_ms=start_ms)
        inner = None
        if self._root is not None:
            try:
                inner = self._root.start_observation(name=name, as_type="span")
            except Exception as exc:  # Langfuse 失败绝不影响本地链路
                log.warning("slf start obs failed: %s", exc)
        # 压栈：让后续叶子跨度挂到本容器下；finish 时出栈恢复。
        _CTX._parents.append(parent)
        _CTX.parent_id = oid or parent
        return _LocalSpan(inner=inner, local_id=oid, start_ms=start_ms,
                         is_container=True, store=self._ensure_store())

    def start_dispatch(self, text: str, label: str = "") -> "_LocalSpan":
        """开启一个意图调度跨度。"""
        start_ms = time.time()
        name = f"dispatch:{label or '?'}"
        oid = self._write_local_obs(
            name, otype="SPAN", start_ms=start_ms, input={"text": (text or "")[:500]},
            metadata={"label": label}, source="dispatch",
        )
        inner = None
        if self._root is not None:
            try:
                inner = self._root.start_observation(name=name, as_type="span")
            except Exception as exc:
                log.warning("dispatch obs failed: %s", exc)
        # dispatch 是叶子操作，不入栈（不充当后续叶子的父）。
        return _LocalSpan(inner=inner, local_id=oid, start_ms=start_ms,
                         is_container=False, store=self._ensure_store())

    # ---- 叶子跨度：HTTP / LLM ----
    def start_http(self, method: str, path: str) -> "_LocalSpan":
        start_ms = time.time()
        oid = self._write_local_obs(
            f"HTTP {method} {path}", otype="SPAN", start_ms=start_ms,
            input={"method": method, "path": (path or "")[:500]}, source="http",
        )
        inner = None
        if self._root is not None:
            try:
                inner = self._root.start_observation(
                    name=f"HTTP {method} {path}", as_type="span"
                )
            except Exception as exc:
                log.warning("http obs failed: %s", exc)
        return _LocalSpan(inner=inner, local_id=oid, start_ms=start_ms,
                         store=self._ensure_store())

    def start_llm(self, task: str, model: str) -> "_LocalSpan":
        start_ms = time.time()
        oid = self._write_local_obs(
            f"LLM {task}", otype="GENERATION", start_ms=start_ms,
            model=model, task=task, source="llm",
        )
        inner = None
        if self._root is not None:
            try:
                inner = self._root.start_observation(
                    name=f"LLM {task}", as_type="generation", model=model
                )
            except Exception as exc:
                log.warning("llm obs failed: %s", exc)
        return _LocalSpan(inner=inner, local_id=oid, start_ms=start_ms,
                         store=self._ensure_store())

    # ---- 失败治理节点 ----
    def observe_failure(self, step_name: str, exc: BaseException) -> None:
        self._write_local_obs(
            f"failure:{step_name}", otype="SPAN",
            start_ms=time.time(), level="ERROR", status="error",
            metadata={"error_type": type(exc).__name__, "message": str(exc)[:500]},
            source="failure",
        )
        if self._root is not None:
            try:
                self._root.start_observation(
                    name=f"failure:{step_name}",
                    as_type="span",
                    level="ERROR",
                    status_message=str(exc),
                    metadata={"error_type": type(exc).__name__},
                )
            except Exception as lan:
                log.warning("failure obs failed: %s", lan)

    def record_heal(
        self,
        step_name: str,
        *,
        level: str,
        mode: str,
        succeeded: bool,
        meta: Optional[dict[str, Any]] = None,
    ) -> None:
        """发出一个自愈节点（L1 重试 / L2 LLM 反思）。"""
        self._write_local_obs(
            f"heal:{step_name}", otype="SPAN",
            start_ms=time.time(),
            status="ok" if succeeded else "error",
            metadata={
                "level": level, "mode": mode, "succeeded": succeeded,
                "meta": meta or {},
            },
            source="heal",
        )
        if self._root is not None:
            try:
                self._root.start_observation(
                    name=f"heal:{step_name}",
                    as_type="span",
                    level="INFO",
                    status_message=f"L{level} heal {'ok' if succeeded else 'failed'} ({mode})",
                    metadata={"level": level, "mode": mode, "succeeded": succeeded, "meta": meta},
                )
            except Exception as lan:
                log.warning("heal obs failed: %s", lan)


class _LocalSpan:
    """本地 + Langfuse 合并跨度；统一 ``finish(...)`` 签名，双写收尾。"""

    def __init__(
        self,
        inner: Any,
        local_id: Optional[str],
        start_ms: float,
        is_container: bool = False,
        store: Optional[ObserveStore] = None,
    ) -> None:
        self._inner = inner
        self._local_id = local_id
        self._start_ms = start_ms
        self._is_container = is_container
        # 本地 store 引用：finish 时按 id 部分更新 latency/status/tokens/output。
        self._store = store

    def finish(
        self,
        *,
        status: Optional[int] = None,
        latency_ms: Optional[float] = None,
        prompt: Optional[str] = None,
        response: Optional[str] = None,
        usage: Optional[dict[str, Any]] = None,
    ) -> None:
        # 容器跨度（case/step）：finish 时出栈恢复外层 parent。
        if self._is_container and _CTX._parents:
            _CTX.parent_id = _CTX._parents.pop()
        # Langfuse 收尾（可选）。
        if self._inner is not None:
            fields: dict[str, Any] = {}
            if latency_ms is not None:
                fields["metadata"] = {"latency_ms": round(latency_ms, 2)}
            if status is not None:
                fields["output"] = {"status_code": status}
            if prompt is not None:
                fields["input"] = {"prompt": prompt}
            if response is not None:
                fields["output"] = {**(fields.get("output") or {}), "response": response}
            if usage:
                fields["usage_details"] = usage
            try:
                if fields and hasattr(self._inner, "update"):
                    self._inner.update(**fields)
                if hasattr(self._inner, "end"):
                    self._inner.end()
            except Exception as exc:  # 追踪收尾失败绝不影响运行
                log.warning("langfuse span finish failed: %s", exc)
        # 本地库收尾：补 latency / status / output（失败仅告警）。
        if self._local_id is not None and self._store is not None:
            try:
                fields_upd: dict[str, Any] = {}
                if latency_ms is not None:
                    fields_upd["latency_ms"] = round(latency_ms, 3)
                if status is not None:
                    fields_upd["status"] = "ok" if status < 500 else "error"
                    fields_upd["output"] = {"status_code": status}
                if response is not None:
                    fields_upd["output"] = {
                        **(fields_upd.get("output") or {}),
                        "response": (response or "")[:2000],
                    }
                if prompt is not None:
                    fields_upd["input"] = {"prompt": (prompt or "")[:2000]}
                if usage:
                    fields_upd["prompt_tokens"] = usage.get("prompt_tokens", 0)
                    fields_upd["completion_tokens"] = usage.get("completion_tokens", 0)
                self._store.update_observation(self._local_id, fields_upd)
            except Exception as exc:
                log.warning("local span finish failed: %s", exc)


class _Span:
    """Langfuse 跨度适配器，带统一的 finish(...) 签名。

    仅供需要单独构造 v4 Langfuse 跨度的地方单独使用；:class:`_LocalSpan`
    是它以本地双写为副作用的合并实现。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def finish(
        self,
        *,
        status: Optional[int] = None,
        latency_ms: Optional[float] = None,
        prompt: Optional[str] = None,
        response: Optional[str] = None,
        usage: Optional[dict[str, Any]] = None,
    ) -> None:
        fields: dict[str, Any] = {}
        if latency_ms is not None:
            fields["metadata"] = {"latency_ms": round(latency_ms, 2)}
        if status is not None:
            fields["output"] = {"status_code": status}
        if prompt is not None:
            fields["input"] = {"prompt": prompt}
        if response is not None:
            fields["output"] = {**(fields.get("output") or {}), "response": response}
        # v4 用 usage_details 表达 token 用量（原来的 usage 不生效）。
        if usage:
            fields["usage_details"] = usage
        if fields:
            self._inner.update(**fields)
        self._inner.end()


_tracer: Any = None


def get_tracer() -> Any:
    """返回共享跟踪器（Langfuse + 本地双写或空）。

    任一开关开启（``local_obs_enabled`` 或 ``trace_enabled``+凭据）都构造真实
    Tracer。两套开关各自独立，隔离测试（LLMOFF=1）下若设 ``AH_LOCAL_OBS=1``
    仍能得到本地过程链路（无网络）。写失败/依赖缺失时降级空跟踪器，绝不抛出。
    """
    global _tracer
    if _tracer is not None:
        return _tracer

    local_enabled = bool(settings.local_obs_enabled)
    use_langfuse = bool(
        settings.trace_enabled
        and settings.trace_public_key
        and settings.trace_secret_key
    )
    if not local_enabled and not use_langfuse:
        # 完全禁用：空操作跟踪器。
        _tracer = NullTracer()  # type: ignore[assignment]
        return _tracer

    trace_name = settings.trace_name or _snowflake_id()
    store: Optional[ObserveStore] = None
    lf: Any = None
    trace_id: str = ""
    try:
        if local_enabled:
            store = open_store(settings.local_obs_db)
    except Exception as exc:  # 本地上报失败仅告警，Langfuse 仍可用
        log.warning("local-obs open failed: %s", exc)
        store = None
    if use_langfuse:
        try:
            from langfuse import Langfuse  # type: ignore

            lf = Langfuse(
                public_key=settings.trace_public_key,
                secret_key=settings.trace_secret_key,
                host=settings.trace_host,
            )
            # v4 要求 trace_id 是合法 32 位小写十六进制，用 SDK 生成。
            trace_id = lf.create_trace_id()
        except Exception as exc:  # langfuse 缺失或配置错误：降级为仅本地
            log.warning("langfuse disabled: %s", exc)
            lf = None
    if lf is None and store is None:
        _tracer = NullTracer()  # type: ignore[assignment]
        return _tracer
    _tracer = Tracer(lf, trace_id or _snowflake_id(), trace_name, store=store,
                     store_db=str(settings.local_obs_db))
    return _tracer
