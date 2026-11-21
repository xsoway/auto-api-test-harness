#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/17 11:00
# @Filename : executor.py
# @Author   : Alan_Hsu
"""业务流程组装执行：把 skill 互调（call/with/assign）展开进 ``run_flow``。

用户用 ``flow:``（或 skill 的 ``steps:`` 里的 ``call:``）声明式组装业务流。本模块把
「call/with/assign」解释成对现有 :func:`ah.core.orchestrator.run_flow` 的调用：

- **作用域隔离**：每次 ``call`` 开一个子作用域（快照 ctx），``with`` 注入入参（先解析
  外层 ``${var}``），被调 skill 的步骤在该作用域执行并产出；结束后**回滚**除 ``assign``
  目标外的全部变更——skill 内部变量不泄漏到外层。
- **assign**：把被调 skill 产出的变量（``ctx`` 中的键）以调用方声明的变量名复制到外层
  作用域，供后续步骤消费。
- **复用现有执行内核**：内联步骤与 skill 步骤都最终交给 ``run_flow``（``stop/continue``、
  ``extract``、``assert``、hermetic 无不例外），不制造第二套执行器，``LLMOFF=1`` 语义不变。

本模块零 LLM、零网络；嵌套互调深度受 :data:`ah.skill.model.MAX_CALL_DEPTH` 限制防环
（环在加载期已由 :mod:`ah.skill.validate` 拦截，这里再兜一层）。
"""

from __future__ import annotations

from copy import deepcopy
from time import monotonic as _monotonic
from time import sleep as _sleep
from typing import Any, Dict, List, Optional

from ah.core.orchestrator import StepResult, run_flow
from ah.harness.loader import Case
from ah.skill.model import MAX_CALL_DEPTH, SkillRegistry, Step
from ah.skill.validate import WHEN_OPS
from ah.utils.context import Context, render_value
from ah.utils.logging import get_logger

log = get_logger("skill.executor")

#: 流程条目里互调的关键字（``call:``）。
_CALL_KEY = "call"
#: 流程条目里的条件分支关键字（``when:``，§9.2）。
_WHEN_KEY = "when"
#: 流程条目里的异步轮询关键字（``wait:``，§9.1）。
_WAIT_KEY = "wait"
#: wait 轮询允许的只读方法（拒绝写副作用目标的盲目重试，§9.1 警示）。
_WAIT_GET_METHODS: set[str] = {"GET", "HEAD"}

#: 流程条目里互调的关键字（``call:``）。
_CALL_KEY = "call"


class SkillExecutionError(Exception):
    """组装执行期错误（引用缺失 / 深度越界），由调用方捕获转结果。"""


def _entry_to_case(step: Step, *, skill: str) -> Optional[Case]:
    """把 skill 的内联步骤（非 call）转成 :class:`Case`。

    返回 None 表示该步骤是 ``call``（应由 executor 递归处理）。
    名字加 ``<skill>.<step>`` 前缀，避免多条 skill 展开后步骤名撞车
    （``run_flow`` 用名字做去重/依赖判定）。
    """
    if step.is_call or step.call is not None:
        return None
    return Case(
        name=f"{skill}.{step.name}",
        request=deepcopy(step.request or {}),
        extract=dict(step.extract or {}),
        assert_=dict(step.assert_ or {}),
        on_failure="stop",
        scenario=skill,
        source_path=skill,
    )


def _resolve_with(params: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    """解析 ``with`` 入参里的外层 ``${var}`` 引用，供注入子作用域。"""
    out: Dict[str, Any] = {}
    for key, value in params.items():
        out[key] = render_value(value, ctx)
    return out


def _assign_var_name(template: Any) -> str:
    """从 assign 目标形式（``${order_id}`` 或裸 ``order_id``）取出真·变量名。

    assign 的 value 语义是「被调 skill 产出变量的引用」：习惯写作 ``${name}``，
    也可能裸写 ``name``。两种都归一为键名再读 ctx，避免把 ``${name}`` 当字面键。
    """
    s = str(template).strip()
    if s.startswith("${") and s.endswith("}"):
        return s[2:-1]
    return s


def _execute_call(
    entry: Dict[str, Any],
    registry: SkillRegistry,
    client: Any,
    ctx: Context,
    *,
    tracer: Any = None,
    depth: int = 0,
) -> List[Any]:
    """执行一次 skill 互调：作用域隔离 + with 注入 + assign 上提。"""
    target = str(entry[_CALL_KEY])
    if depth >= MAX_CALL_DEPTH:
        raise SkillExecutionError(
            f"skill 互调深度超过 {MAX_CALL_DEPTH}（疑似循环或过深）: {target}"
        )
    callee = registry.get(target)
    if callee is None:
        raise SkillExecutionError(f"调用不存在的 skill {target!r}")
    with_params = dict(entry.get("with") or {})
    assign = dict(entry.get("assign") or {})

    # 1) 快照外层作用域，注入 with（值先在调用方作用域解析 ${var}）。
    snapshot = ctx.as_dict()
    for key, value in _resolve_with(with_params, ctx).items():
        ctx.set(key, value)

    # 2) 在被调 skill 的作用域内执行其 steps（内联 + 嵌套 call）。
    results: List[Any] = []
    try:
        for step in callee.steps:
            if step.is_call and step.call is not None:
                sub: Dict[str, Any] = {"call": step.call.target}
                if step.call.with_vars:
                    sub["with"] = step.call.with_vars
                if step.call.assign:
                    sub["assign"] = step.call.assign
                results.extend(
                    _execute_call(sub, registry, client, ctx, tracer=tracer, depth=depth + 1)
                )
            else:
                case = _entry_to_case(step, skill=callee.name)
                if case is not None:
                    results.extend(run_flow([case], ctx, client, tracer=tracer))
    finally:
        # 3) 回滚到快照；先在本作用域读 assign 目标原值，再把 assign 目标复制回外层。
        captured: Dict[str, Any] = {}
        for local, callee_out in assign.items():
            captured[local] = ctx.get(_assign_var_name(callee_out), None)
        # Context 无 delete/clear 公开接口，直接重建存储：移除 call 期间新增、还原被改动的键。
        changed = [k for k in ctx.as_dict() if k not in snapshot]
        for key in changed:
            ctx._data.pop(key, None)  # noqa: SLF001 — context.py 无公开清理接口
        ctx._data.update(snapshot)  # noqa: SLF001
        for local, value in captured.items():
            ctx.set(local, value)  # assign 目标上提到外层
    return results


def _execute_inline(
    entry: Dict[str, Any],
    client: Any,
    ctx: Context,
    *,
    tracer: Any = None,
    skill_name: str = "flow",
) -> List[Any]:
    """执行一个内联步骤（``name``+``request``+``extract``+``assert``）。"""
    name = str(entry.get("name") or f"{skill_name}.inline")
    case = Case(
        name=f"{skill_name}.{name}",
        request=deepcopy(entry.get("request") or {}),
        extract=dict(entry.get("extract") or {}),
        assert_=dict(entry.get("assert") or {}),
        on_failure="stop",
        scenario=skill_name,
        source_path=f"<inline:{skill_name}>",
    )
    # 内联步骤与 skill 步骤同样交给 run_flow（共享 ctx，产出回写上下文）。
    return list(run_flow([case], ctx, client, tracer=tracer))


def execute_entry(
    entry: Dict[str, Any],
    registry: SkillRegistry,
    client: Any,
    ctx: Context,
    *,
    tracer: Any = None,
    depth: int = 0,
) -> List[Any]:
    """执行一个流程条目（call 或内联步骤），返回 ``StepResult`` 列表。"""
    if _CALL_KEY in entry:
        return _execute_call(entry, registry, client, ctx, tracer=tracer, depth=depth)
    return _execute_inline(entry, client, ctx, tracer=tracer)

def _when_reason(when: Dict[str, Any]) -> str:
    """生成 when 条件的可读描述（供 skipped 原因展示，不执行任意表达式）。"""
    field = when.get("field", "?")
    op = next((k for k in WHEN_OPS if k in when), "?")
    return f"{field} {op} {when.get(op, '')}"


def _eval_when(when: Dict[str, Any], ctx: Context) -> bool:
    r"""评估一条 ``when`` 条件（§9.2）：有穷算子，不做任意表达式求值。

    ``field`` 引用 ctx 变量（可带 ``\${}``）；算子取值直接挂在算子键下：
    ``equals/not_equals/in/exists``。无算子视为放行（由 validate 拦截非法写法，
    这里兜底返回 True，不阻断合法 flow）。
    """
    field = _assign_var_name(when.get("field", "")).strip()
    if not field:
        return True
    for op in WHEN_OPS:
        if op not in when:
            continue
        if op == "exists":
            return ctx.has(field)
        actual = ctx.get(field)
        expected = when[op]
        if op == "equals":
            return actual == expected
        if op == "not_equals":
            return actual != expected
        if op == "in":
            return actual in expected if isinstance(expected, list) else False
    return True


def _execute_wait(
    wait: Dict[str, Any],
    client: Any,
    ctx: Context,
    *,
    tracer: Any = None,
) -> List[Any]:
    """执行一次异步轮询（§9.1 wait）：只读 GET 循环，直到字段等于期望值或超时。

    ``wait`` 映射含 ``request``（只允许 GET/HEAD）、``field``（引用 ctx 变量）、
    ``equals``（期望值）、可选 ``timeout_seconds``（默认 30）/``interval_seconds``
    （默认 1）。**拒绝写方法**（POST/PUT/DELETE…）：有副作用的创建/提交/扣费禁止
    盲目重试（§9.1 警示）。满足返回成功，超时返回 failed。
    """
    name = f"wait.{_assign_var_name(wait.get('field', '?'))}"
    req = wait.get("request") or {}
    method = str(req.get("method", "GET")).upper()
    if method not in _WAIT_GET_METHODS:
        return [
            StepResult(
                name=name,
                succeeded=False,
                status="blocked",
                verdict="unverified",
                reason=(
                    f"wait 只允许只读轮询（GET/HEAD），收到 {method}；"
                    f"有副作用的写目标禁止盲目重试（方案 §9.1）"
                ),
            )
        ]
    field = _assign_var_name(wait.get("field", "")).strip()
    equals = wait.get("equals")
    timeout = float(wait.get("timeout_seconds", 30) or 30)
    interval = float(wait.get("interval_seconds", 1) or 1)
    # 单次 GET 轮询 case：extract 把响应字段写回 ctx，供 field 判断。
    case = Case(
        name=name,
        request=deepcopy(req),
        extract=dict(wait.get("extract") or {}),
        assert_={},
        on_failure="stop",
        scenario="flow",
        source_path="<inline:flow.wait>",
    )
    deadline = _monotonic() + timeout
    attempts = 0
    while True:
        attempts += 1
        run_flow([case], ctx, client, tracer=tracer)
        actual = ctx.get(field)
        if actual == equals:
            return [
                StepResult(
                    name=name,
                    succeeded=True,
                    status="executed",
                    verdict="passed",
                    reason=f"等待 {field} == {equals!r}（第 {attempts} 次满足）",
                )
            ]
        if _monotonic() >= deadline:
            return [
                StepResult(
                    name=name,
                    succeeded=False,
                    status="failed",
                    verdict="unverified",
                    reason=(
                        f"轮询超时（{timeout:g}s，{attempts} 次）："
                        f"{field} 仍未等于 {equals!r}（当前 {actual!r}）"
                    ),
                )
            ]
        _sleep(max(interval, 0.0))


def _cleanup_resources(
    resources: List[Dict[str, Any]],
    registry: SkillRegistry,
    client: Any,
    ctx: Context,
    *,
    tracer: Any = None,
) -> List[Any]:
    r"""主流程后执行资源清理（§10.2），失败仅告警，不覆盖主流程结果。

    每条资源 ``{type, id, cleanup}``：``cleanup`` 是 skill 名（互调）或内联请求映射。
    ``id`` 支持 ``\${var}`` 引用当前 ctx。清理结果 append 到返回列表；任一条失败
    都记 warning 日志，但**不**改变主流程的成败判定（§10.2「清理失败不覆盖主流程
    原始错误」）。
    """
    out: List[Any] = []
    for idx, res in enumerate(resources):
        name = f"cleanup[{idx}]"
        # 渲染资源 id：missing 变量保留字面（可能由外部创建），不抛穿。
        try:
            ridd = render_value(str(res.get("id", "")), ctx)
        except KeyError:
            ridd = str(res.get("id", ""))
        try:
            cleanup = res.get("cleanup")
            if isinstance(cleanup, str):
                # 仅在资源 id 真正渲染为具体值时才注入 with.id；缺失变量保留字面
                # （如外部创建的资源），让被调 skill 用自己的 defaults/路径，避免
                # _resolve_with 对未解析 `${var}` 抛 KeyError 使清理误报失败。
                with_params = {} if "${" in ridd else {"id": ridd}
                out.extend(
                    execute_entry(
                        {"call": cleanup, **({"with": with_params} if with_params else {})},
                        registry,
                        client,
                        ctx,
                        tracer=tracer,
                    )
                )
            else:
                out.append(
                    StepResult(
                        name=name,
                        succeeded=False,
                        status="skipped",
                        verdict="unverified",
                        reason="cleanup 必须是 skill 名或内联请求映射",
                    )
                )
        except Exception as exc:  # noqa: BLE001 — 清理失败仅告警，不覆盖主流程
            log.warning("资源清理失败 %s: %s", name, exc, exc_info=True)
            out.append(
                StepResult(
                    name=name,
                    succeeded=False,
                    status="failed",
                    verdict="unverified",
                    reason=str(exc),
                )
            )
    return out


def execute_flow(
    flow: List[Dict[str, Any]],
    registry: SkillRegistry,
    client: Any,
    *,
    base_ctx: Optional[Context] = None,
    tracer: Any = None,
    dataset_row: Optional[Dict[str, Any]] = None,
    resources: Optional[List[Dict[str, Any]]] = None,
) -> List[Any]:
    r"""执行一条组装流程（``flow:`` 条目列表），返回全部 ``StepResult``。

    ``base_ctx`` 缺省用全新 :class:`Context`；传入时（如 ``ah run``）流程在其上执行
    并回写 assign/内联产出。可选编排扩展均**向后兼容**，缺省时行为与旧版完全一致：

    - ``dataset_row``（§7.3 数据驱动）：非空时把每字段注入 ``\${data.<k>}`` 引用，
      数据轮用独立数据快照，不回写共享 base_ctx，保证多轮互不污染。
    - 条目级 ``when``（§9.2 条件分支）：条件不满足的条目标记 skipped，不执行。
    - 条目级 ``wait``（§9.1 异步轮询）：只读 GET 循环轮询字段至等于期望值/超时。
    - ``resources``（顶层，§10.2 资源清理）：主流程后逐项清理，失败仅告警。
    """
    ctx = base_ctx if base_ctx is not None else Context()
    if dataset_row:
        # 数据驱动轮：独立数据空间，避免 data.* 残留到共享 base_ctx（多轮隔离）。
        ctx = ctx.copy() if base_ctx is not None else Context()
        for key, value in dataset_row.items():
            if key == "case":
                continue
            ctx.set(f"data.{key}", value)
    results: List[Any] = []
    for entry in flow:
        # 条件分支（when）：不满足则跳过该条目，不执行；skipped 不计失败。
        if isinstance(entry, dict) and _WHEN_KEY in entry:
            when = entry[_WHEN_KEY]
            if isinstance(when, dict) and not _eval_when(when, ctx):
                name = entry.get(_CALL_KEY) or entry.get("name", "?")
                results.append(
                    StepResult(
                        name=str(name),
                        succeeded=True,
                        status="skipped",
                        verdict="unverified",
                        reason=f"when 条件不满足（{_when_reason(when)}），跳过该条目",
                    )
                )
                continue
        try:
            # 异步轮询条目（wait）独立处理。
            if isinstance(entry, dict) and _WAIT_KEY in entry:
                results.extend(_execute_wait(entry[_WAIT_KEY], client, ctx, tracer=tracer))
                continue
            results.extend(execute_entry(entry, registry, client, ctx, tracer=tracer))
        except SkillExecutionError as exc:
            name = entry.get(_CALL_KEY) or entry.get("name", "?")
            # 组装期错误视为该步骤失败（不抛穿，保持 run 不中断）。
            results.append(
                StepResult(
                    name=str(name),
                    succeeded=False,
                    status="failed",
                    verdict="unverified",
                    reason=str(exc),
                )
            )
    # 资源清理（resources）：主流程执行后（无论成败）统一清理。
    if resources:
        results.extend(
            _cleanup_resources(resources, registry, client, ctx, tracer=tracer)
        )
    return results


