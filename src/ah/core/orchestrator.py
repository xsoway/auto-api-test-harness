"""用例编排：单接口与场景流。

:func:`run_single` 编排单个用例——从上下文渲染请求、调用框架 HTTP 客户端、
提取变量、断言响应——并返回一个 :class:`StepResult`。

:func:`run_flow` 针对共享的 :class:`Context` 按顺序运行一系列用例，使得每一步
都能消费上游输出；它遵循每个用例的 ``on_failure`` 键（``stop`` 为默认，
``continue`` 表示继续执行）。

LLM 功能通过 :class:`LLMHooks` 接入（全部可选；None = 隔离/关闭）：
``data_gen``（调用前）、``triage``（出错时）、``heal``（重试并验证）。

:class:`StepResult` 携带 ``to_dict()`` / ``from_dict()`` 重建逻辑
（镜像 Eval-Anything 的 ``Trajectory``），使运行结果可以被持久化、恢复，
或直接喂给报告层。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from itertools import groupby
from typing import Any, Callable, Optional

import httpx

from ah.core.variables import materialize_variables
from ah.evidence.laya_gate import (
    STAGE_RETRY,
    STAGE_TOOL,
    STAGE_TRIAGE,
    tool_enforce_enabled,
)
from ah.failure.triage import FAILURE_CATEGORIES
from ah.harness.http import APIResponse, HTTPClient
from ah.harness.loader import Case
from ah.metrics.assertions import assert_response
from ah.metrics.extract import extract_vars
from ah.utils.context import Context, render, render_value
from ah.utils.logging import get_logger

log = get_logger("runner")


class ToolRiskBlocked(Exception):
    """工具风险 ENFORCE 阻断：高风险工具调用被 Laya 高置信判为高危并拦截。

    ENFORCE（``AH_LAYA_ENFORCE_TOOL=1``）下，STAGE_TOOL 高置信 value=high
    时拒绝派发该用例（fail-safe）。崩溃在报告层可见为 blocked 原因，绝不静默。
    """

    def __init__(self, *, method: str, path: str, level: str, confidence: float) -> None:
        self.method = method
        self.path = path
        self.level = level
        self.confidence = confidence
        super().__init__(
            f"工具风险 ENFORCE 阻断：{method} {path} 被 Laya 判为高危"
            f"（confidence={confidence:.2f}），拒绝派发"
        )


@dataclass
class StepResult:
    """每个计划步骤的执行、验证与未执行原因。"""

    name: str
    succeeded: bool
    response: Optional[APIResponse] = None
    extracted: dict[str, Any] = field(default_factory=dict)
    error: Optional[BaseException] = None
    attempts: int = 1
    llm_notes: dict[str, Any] = field(default_factory=dict)
    request_kwargs: dict[str, Any] = field(default_factory=dict)
    # 本次执行逐条断言的评估结果（每条含 type/logic/expected/actual/passed），
    # 供报告「断言详情」渲染；无断言或未采集时为空列表。
# 本次执行逐条断言的评估结果（每条含 type/logic/expected/actual/passed），
    # 供报告「断言详情」渲染；无断言或未采集时为空列表。
    assertions: list[dict[str, Any]] = field(default_factory=list)
    status: str = "executed"  # executed | blocked | skipped
    verdict: str = ""  # passed | failed | unverified；空值兼容旧构造方
    reason: str = ""
    duration: float = 0.0
    #: 本步骤实际开始/结束的 ISO 时间戳（``%Y-%m-%dT%H:%M:%S``），
    #: 用于让 Agent 判断「这次验证覆盖的是哪一版代码」：仅当结束时间晚于它
    #: 最后一次提交，本次回归才算数（对齐《让 Agent 自己跑回归》字段一）。
    #: 旧记录/未捕获时为空串。
    started_at: str = ""
    ended_at: str = ""

    @property
    def request_detail(self) -> dict[str, Any]:
        """已构建请求的关键字段（用于报告展示）。"""
        req = self.request_kwargs or {}
        return {
            "method": req.get("method", ""),
            "path": req.get("path", ""),
            "params": req.get("params"),
            "json": req.get("json"),
            "data": req.get("data"),
        }

    @property
    def status_code(self) -> Optional[int]:
        return self.response.status_code if self.response else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StepResult":
        """根据序列化字典重建 StepResult（结果持久化）。"""
        return cls(
            name=str(data.get("name", "")),
            succeeded=bool(data.get("succeeded", False)),
            response=None,
            extracted=dict(data.get("extracted") or {}),
            attempts=int(data.get("attempts", 1)),
            llm_notes=dict(data.get("llm_notes") or {}),
            request_kwargs=dict(data.get("request_kwargs") or {}),
            assertions=[dict(a) for a in (data.get("assertions") or [])],
            status=str(data.get("status", "executed")),
            verdict=str(data.get("verdict", "")),
            reason=str(data.get("reason", "")),
duration=float(data.get("duration", 0.0)),
            started_at=str(data.get("started_at", "")),
            ended_at=str(data.get("ended_at", "")),
        )


@dataclass
class LLMHooks:
    """可选的 LLM 辅助钩子；全部默认为 None（隔离）。"""

    data_gen: Optional[Callable[["Case", Context], dict[str, Any]]] = None
    # 响应后处理：extract 之后、断言之前运行；返回的 dict 合并进 ctx 与
    # 结果的 extracted（数据依赖派生变量，如按业务键从列表匹配出 id）。
    on_response: Optional[Callable[["Case", APIResponse, Context], dict[str, Any]]] = None
    on_complete: Optional[Callable[[Context, list["StepResult"]], None]] = None
    triage: Optional[Callable[["Case", APIResponse, BaseException], dict[str, Any]]] = None
    heal: Optional[
        Callable[["Case", APIResponse, BaseException, int], tuple[bool, dict[str, Any]]]
    ] = None
    max_heal_attempts: int = 2
    # observe 临时恢复钩子：observe 动作时做无侵入恢复（重试/重连/重建令牌），
    # 成功只重跑原步骤、绝不回写脚本（对应《别让自愈把红灯洗绿》隔离观察）。
    observe: Optional[
        Callable[["Case", APIResponse, BaseException, Context, int], dict[str, Any]]
    ] = None


def _deep_render(node: Any, ctx: Context, *, typed: bool = False) -> Any:
    """递归渲染请求，默认保持旧字符串引用语义。"""
    return render_value(node, ctx, typed=typed)


def _build_request(
    case: Case,
    ctx: Context,
    extra_json: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if case.reference_mode not in {"legacy", "typed"}:
        raise ValueError(f"invalid reference_mode: {case.reference_mode!r}")
    req = case.request
    typed = case.reference_mode == "typed"
    params = _deep_render(req.get("params"), ctx, typed=typed)
    jsn = _deep_render(req.get("json"), ctx, typed=typed)
    if extra_json:
        base = dict(jsn) if isinstance(jsn, dict) else {}
        base.update(extra_json)
        jsn = base
    headers = _deep_render(req.get("headers"), ctx) or None
    return {
        "method": str(req.get("method", "GET")).upper(),
        "path": render(str(req.get("path", "")), ctx),
        "params": params,
        "json": jsn,
        "data": _deep_render(req.get("data"), ctx, typed=typed),
        "headers": headers,
        "auth": case.auth,
        "timeout": case.timeout,
    }


def _heal_fix_summary(meta: dict[str, Any]) -> str:
    """把 LLM 自愈 meta 提炼成人类可读的修复方案摘要（无修正信息返回空串）。"""
    parts: list[str] = []
    if isinstance(meta.get("assert"), (dict, list)):
        # LLM 修正后的断言（纠正了原断言范围/取值）。
        assert_txt = (
            str(meta["assert"])
            if isinstance(meta["assert"], str)
            else json.dumps(meta["assert"], ensure_ascii=False)
        )
        parts.append(f"修正断言 {assert_txt}")
    if meta.get("candidate"):
        # LLM 给出的候选请求（修正了参数/请求体）。
        candidate = str(meta["candidate"])
        parts.append(f"候选请求 {candidate}")
    if meta.get("validated_by"):
        parts.append(f"验证方式 {meta['validated_by']}")
    return "；".join(parts)


_RETRY_QUESTIONS: dict[str, Any] = {
    "retryable": {
        "type": "choice",
        "instructions": "这次接口失败是否应该重试？",
        "criteria": {
            "yes": "瞬时/偶发失败（超时、网络、5xx），重试可恢复",
            "no": "确定性失败（断言不符、逻辑/数据错误），重试无效",
            "other": "不确定",
        },
    }
}


_TOOL_QUESTIONS: dict[str, Any] = {
    "tool_risk": {
        "type": "choice",
        "instructions": "执行这个接口调用前，它的风险等级是？",
        "criteria": {
            "high": "破坏性/写操作（DELETE/POST 改数据）或关键路径，误执行代价高",
            "medium": "有副作用但可回滚/可观察",
            "low": "只读/幂等/沙箱内，风险可忽略",
            "other": "不确定",
        },
    }
}


def _tool_risk_state(method: str, path: str, params: Any, jsn: Any) -> str:
    """构造 Laya 可读的工具调用风险上下文（业务描述，不使用内部函数名）。"""
    parts = [f"HTTP 方法: {method}", f"路径: {path}"]
    if params:
        parts.append(f"查询参数: {str(params)[:120]}")
    if jsn:
        parts.append(f"请求体: {str(jsn)[:200]}")
    return "；".join(parts)


def _retry_state(exc: BaseException, response: Optional[APIResponse]) -> str:
    """构造 Laya 可读的重试决策上下文（业务描述，不使用内部函数名）。"""
    msg = (str(exc) or "").strip().replace("\n", " ")[:200]
    parts = [f"异常类型: {type(exc).__name__}"]
    if msg:
        parts.append(f"错误消息: {msg}")
    if response is not None:
        parts.append(f"响应状态码: {response.status_code}")
    return "；".join(parts)


def _is_retryable_error(
    exc: BaseException,
    response: Optional[APIResponse],
    *,
    gate: Any = None,
) -> bool:
    """判断失败是否属于"瞬时错误"（可配置重试的目标）。

    仅对三类瞬时失败重试：传输层异常（超时/连接/网络）、或拿到了 5xx 响应。
    断言不匹配、提取失败等确定性客户端错误不属于瞬时，不重试。

    前置决策分流：当 ``gate`` 非 None 且该环节开闸时，先咨询 LayaGate。
    高置信裁决（action==auto 且 value∈{yes,no}）直接采用；否则（未开闸/
    低置信/异常）回退下方确定性判定（字节等价）。gate=None（默认/off）→
    完全跳过，零网络。
    """
    if gate is not None:
        try:
            dec = gate.best(STAGE_RETRY, _retry_state(exc, response), _RETRY_QUESTIONS)
            if dec.get("action") == "auto" and dec.get("value") in ("yes", "no"):
                return dec["value"] == "yes"
        except Exception as gate_exc:  # Laya 故障绝不改变原流程
            log.warning("laya retry 分流失败，回退确定性判定: %s", gate_exc)
    if isinstance(exc, (httpx.TransportError, OSError)):
        return True
    if response is not None:
        try:
            status = int(response.status_code)
        except (TypeError, ValueError):
            return False
        if status >= 500:
            return True
    return False


def _run_single(
    case: Case,
    ctx: Context,
    client: HTTPClient,
    hooks: Optional[LLMHooks] = None,
    tracer: Any = None,
    gate: Any = None,
    governor: Any = None,
) -> StepResult:
    """执行单个用例；返回 :class:`StepResult`（永不抛出异常）。

    ``gate`` 缺省（None）时惰性取进程级 LayaGate（AH_LAYA 开启才非 None）；
    未设/off → `get_gate()` 返回 None → triage/retry 埋点完全跳过，字节等价。

    ``governor``（可选 FailureGovernor）开启失败治理：依据 triage 分类动作
    （heal/observe/block）分流——block 不尝试自愈、observe 走无侵入临时恢复、
    heal 走既有 AgentHeal；并把结果记入失败簇群与知识库。缺省 None → 原流程
    字节等价。
    """
    hooks = hooks or LLMHooks()
    result = StepResult(name=case.name, succeeded=False, attempts=0)
    extra_json: dict[str, Any] = {}

    if hooks.data_gen and case.llm_gen:
        try:
            extra_json = hooks.data_gen(case, ctx) or {}
            if isinstance(extra_json, dict):
                result.llm_notes["data_gen"] = extra_json
        except Exception as exc:
            log.warning("data_gen hook failed for %s: %s", case.name, exc)
            result.llm_notes["data_gen_error"] = str(exc)

    try:
        request_kwargs = _build_request(case, ctx, extra_json)
    except BaseException as build_exc:  # 模板渲染/配置错误（如缺失 ${var}）
        # 确定性错误：重跑也无法修复，直接记为失败返回，保证 run_single 不抛出。
        # 并行模式下，单链的模板错误不应通过异常传播影响其他链。
        result.error = build_exc
        result.reason = str(build_exc)
        return result
    result.request_kwargs = request_kwargs
    # 工具调用风险路由：默认 SHADOW（advisory-only，仅注记 llm_notes 供报告参考）；
    # 当 AH_LAYA_ENFORCE_TOOL=1 时进入 ENFORCE——高置信高风险即拒绝派发该用例
    # （fail-safe：拿不准就不放行，见《别让自愈把红灯洗绿》隔离/兜底语义）。
    _tool_enforce = tool_enforce_enabled()
    if gate is not None:
        try:
            _tdec = gate.best(
                STAGE_TOOL,
                _tool_risk_state(
                    request_kwargs["method"],
                    request_kwargs["path"],
                    request_kwargs.get("params"),
                    request_kwargs.get("json"),
                ),
                _TOOL_QUESTIONS,
            )
            _level = _tdec.get("value")
            if _tdec.get("action") == "auto" and _level in {"high", "medium"}:
                result.llm_notes["laya_tool_risk"] = {
                    "level": _level,
                    "confidence": _tdec.get("confidence", 0.0),
                    "signal": dict(_tdec.get("signal") or {}),
                }
                # ENFORCE：仅"高"风险且判定状态为高置信（confident）才阻断。
                if (
                    _tool_enforce
                    and _level == "high"
                    and _tdec.get("status") == "confident"
                ):
                    result.error = ToolRiskBlocked(
                        method=request_kwargs["method"],
                        path=request_kwargs["path"],
                        level=_level,
                        confidence=_tdec.get("confidence", 0.0),
                    )
                    result.llm_notes["laya_tool_blocked"] = {
                        "level": _level,
                        "confidence": _tdec.get("confidence", 0.0),
                        "signal": dict(_tdec.get("signal") or {}),
                    }
                    result.status = "blocked"
                    result.reason = str(result.error)
                    return result
        except Exception as _texc:  # Laya 故障绝不改变原流程
            log.warning("tool-risk Laya 分流失败，忽略: %s", _texc)

    def _execute() -> APIResponse:
        """执行完整单次运行：HTTP 请求 + 提取 + on_response 派生 + 断言。

        L1 重试需要重跑整条管道（而非仅 HTTP），否则会跳过 on_response/
        断言语义，把数据不匹配误判为自愈成功。

        每次执行包围一个 step 跨度（start_step/…/finish），使本地链路呈现
        run→case→step→HTTP/LLM 层级；finish 放在 finally 保证异常也弹栈，
        绝不泄漏 parent 上下文。
        """
        _step_start = time.perf_counter()
        # 防御式：tracer 可能是测试注入的鸭子类型（仅 record_heal），
        # 缺失 start_step 时跳过 step 跨度，不改变原流程。
        step_span = (
            tracer.start_step("attempt")
            if tracer is not None and hasattr(tracer, "start_step")
            else None
        )
        try:
            result.attempts += 1
            result.extracted = {}
            result.assertions = []
            # 严格步骤的每次尝试使用私有上下文，失败提取/断言不污染下游。
            attempt_ctx = Context(deepcopy(ctx.as_dict())) if case.strict else ctx
            resp = client.request(
                request_kwargs["method"],
                request_kwargs["path"],
                params=request_kwargs["params"],
                json=request_kwargs["json"],
                data=request_kwargs["data"],
                headers=request_kwargs["headers"],
                auth=request_kwargs["auth"],
                timeout=request_kwargs.get("timeout"),
                host=case.host,
            )
            result.response = resp
            if case.extract:
                extracted = extract_vars(
                    resp.text, case.extract, attempt_ctx,
                    allow_null=case.reference_mode == "typed",
                )
                result.extracted = extracted
            if hooks.on_response:
                derived = hooks.on_response(case, resp, attempt_ctx) or {}
                if isinstance(derived, dict):
                    for key, value in derived.items():
                        attempt_ctx.set(key, value)
                    result.extracted = {**dict(result.extracted or {}), **derived}
            # 采集本条断言结果（type/logic/expected/actual/passed），供报告「断言详情」。
            # 放 _execute 末尾：重跑时覆盖为当次尝试的真实断言集（终态响应对齐）。
            assertions_collected: list[dict[str, Any]] = []
            result.assertions = assertions_collected
            assert_response(
                case.assert_, resp, attempt_ctx, collected=assertions_collected,
                soft=case.soft_assert, reference_mode=case.reference_mode,
            )
            if case.strict and any(not a.get("passed") for a in assertions_collected):
                raise AssertionError("严格步骤包含失败的软断言")
            result.error = None
            if case.strict and assertions_collected:
                for key, value in result.extracted.items():
                    ctx.set(key, value)
            if case.soft_assert:
                # 软断言：失败不判用例失败，但记录软失败条数供报告/排障。
                soft_fails = sum(1 for a in assertions_collected if not a.get("passed"))
                if soft_fails:
                    result.llm_notes["soft_failures"] = soft_fails
            return resp
        finally:
            if step_span is not None:
                step_span.finish(
                    latency_ms=(time.perf_counter() - _step_start) * 1000.0
                )

    # ── Phase A：可配置运行重试（仅瞬时错误；区别于 L1 自愈）────────
    # 首个请求失败且命中"瞬时错误"（5xx/超时/连接）时，按 case.retries
    # 退避重试；每次重试覆盖 response/assertions 为当次真实状态。
    exc: Optional[BaseException] = None
    retry_count = 0
    while True:
        try:
            _execute()
            result.succeeded = True
            if retry_count:
                # 配置重试成功：记录重试证据（键 retried，区别于 L1 自愈 healed）。
                result.llm_notes["retried"] = {
                    "attempts": retry_count,
                    "configured": case.retries,
                    "backoff": case.retry_backoff,
                    "trigger": str(exc),
                }
            return result
        except BaseException as attempt_exc:  # 涵盖 AssertionError、ExtractionError、HTTP
            result.error = attempt_exc
            exc = attempt_exc
            # 仅瞬时错误且未用完配置次数 → 退避后重试；否则逃逸到 L1/L2。
            if (retry_count < case.retries
                    and _is_retryable_error(attempt_exc, result.response, gate=gate)):
                retry_count += 1
                if case.retry_backoff > 0:
                    time.sleep(case.retry_backoff)
                continue
            break
    # ↓ exc 已指向逃逸那次失败；下方走既有 L1 确定性自愈 + L2 LLM 路径。
    # L1 确定性自愈：先原样重跑一次吸收瞬时/偶发失败；成功即自愈，
    # 失败再进 LLM 反思（L2，若配置了 hooks）。
    try:
        # _execute() 已包含完整单次管道（HTTP+提取+on_response+断言）；
        # 重跑一次等于原样重跑整个步骤，只有真瞬时失败才会在此自愈。
        retry_resp = _execute()
        result.response = retry_resp
        result.succeeded = True
        result.error = None  # L1 重试成功 = 原用例瞬时失败，非真实缺陷
        result.llm_notes["healed"] = {
            "level": "L1",
            "mode": "retry",
            # 自愈触发条件：首跑失败的具体错误（瞬时/偶发，非真实缺陷）。
            "trigger": str(exc),
            # 自愈原因：原样重跑即恢复 ⇒ 判定为瞬时失败。
            "reason": "瞬时/偶发失败：原样重跑一次即恢复",
            # 修复方案：L1 确定性重试（原样重跑完整单次管道）。
            "fix": "L1 确定性重试（原样重跑一次完整单次管道）",
        }
        if tracer is not None:
            tracer.record_heal(case.name, level="1", mode="retry", succeeded=True)
        return result
    except BaseException as retry_exc:
        # 重试仍失败：保留原始错误（更贴近首因），叠加 LLM 根因。
        result.error = retry_exc
        result.llm_notes["L1_retry"] = {"succeeded": False, "error": str(retry_exc)}
        if tracer is not None:
            tracer.record_heal(case.name, level="1", mode="retry", succeeded=False)
    # Laya 失败分类前置注记（默认 AH_LAYA=off → gate=None → 完全跳过）。
    # 仅在拿到响应时构造 state（连 HTTP 都失败时无响应可分类）。
    # 拆题（文章2"让 Agent 学会判断"）：单选分类交给 Laya（便宜、高频），
    # 根因生成仍保留给 hooks.triage（AgentTriage 的 LLM 反思）。Laya 高置信
    # 分类会被 governor 用作 explicit_category（见下方 governor 分支）。
    laya_category: Optional[str] = None
    if gate is not None and result.response is not None:
        try:
            req = case.request or {}
            status = getattr(result.response, "status_code", None)
            state = (
                f"用例: {case.name} | 接口: {req.get('method', '')} {req.get('path', '')} "
                f"| HTTP {status} | 异常: {type(exc).__name__}: {exc}"
            )
            # 单选分类与 AgentTriage 共享同一 6 分类方案（见 ah.failure.triage）。
            questions = {
                "failure_class": {
                    "type": "choice",
                    "instructions": "这个接口失败属于哪一类？单选。",
                    "criteria": {
                        "script_bug": "脚本本身的定位器/操作/断言/逻辑编写错误",
                        "product_defect": "被测系统的缺陷（断言不符、逻辑错误），重试无效",
                        "environment": "网络/5xx/超时/崩溃等环境问题",
                        "flaky": "偶发抖动，暂未定位稳定根因",
                        "known_issue": "经确认过的已知问题或已知限制",
                        "unknown": "暂时无法判断的问题",
                    },
                }
            }
            dec = gate.best(STAGE_TRIAGE, state, questions)
            val = dec.get("value") or {}
            lvalue = val.get("failure_class")
            if dec.get("action") == "auto" and dec.get("status") == "confident" \
                    and lvalue in FAILURE_CATEGORIES:
                laya_category = lvalue
                result.llm_notes["laya_triage"] = {
                    "category": lvalue,
                    "action": "auto",
                    "confidence": dec.get("confidence", 0.0),
                    "status": dec.get("status"),
                    "signal": dec.get("signal"),
                }
            else:
                # 低置信/未开闸/故障：不采信，留给 LLM 根因 + 确定性规则。
                result.llm_notes["laya_triage"] = {
                    "category": None,
                    "action": dec.get("action"),
                    "status": dec.get("status", "disabled"),
                    "confidence": dec.get("confidence", 0.0),
                    "signal": dec.get("signal"),
                }
        except Exception as ley:
            log.warning("laya triage gate failed for %s: %s", case.name, ley)
    # 仅当拿到响应时才做 LLM 根因分析（连 HTTP 都失败时无响应可分析）；
    # 根因生成保留（拆题：分类已由 Laya 单选，根因仍是 LLM 职责）。
    if hooks.triage and result.response is not None:
        try:
            result.llm_notes["triage"] = hooks.triage(case, result.response, exc)
        except Exception as ter:
            log.warning("triage hook failed for %s: %s", case.name, ter)

    # ── 失败治理：分类→动作分流（governor 开启时才生效；None → 原流程）──
    # 依据 triage 分类动作（heal/observe/block）决定后续：
    #   block   -> 不尝试自愈（产品缺陷，终止等人工）
    #   observe -> 无侵入临时恢复，成功只重跑原步骤不回写脚本
    #   heal    -> 走既有 AgentHeal（生成候选、验证后写回）
    method = str((case.request or {}).get("method", ""))
    path = str((case.request or {}).get("path", ""))
    error_text = str(exc)
    status_code = result.status_code
    if governor is not None:
        triage = result.llm_notes.get("triage") or {}
        # 拆题：Laya 高置信单选分类优先（便宜、高频）；否则回退 LLM AgentTriage
        # 分类；两者皆无 → governor 走确定性规则（classify）。
        explicit_category = laya_category or triage.get("category") or None
        gd = governor.decide(
            method=method,
            path=path,
            case_name=case.name,
            error=exc,
            status_code=status_code,
            explicit_category=explicit_category,
            error_text=error_text,
        )
        result.llm_notes["failure_governance"] = gd
        action = gd["action"]
        # block → 不治，直接失败返回（保留原错误 + 分类动作）。
        if action == "block":
            return result
        # observe → 无侵入临时恢复；成功则标记 recovered（不回写脚本）。
        if action == "observe":
            # observe 的恢复回调不等于本步骤发起的新 HTTP 尝试。
            rec = governor.observe_recover(
                case=case, resp=result.response, error=exc, ctx=ctx, attempt=1
            )
            if rec.get("recovered"):
                result.succeeded = True
                result.error = None
                result.llm_notes["recovered"] = {
                    "level": "observe",
                    "mode": rec.get("mode", "retry-original"),
                    # 铁律：observe 恢复不写回脚本。
                    "wrote_script": False,
                    "trigger": error_text,
                    "reason": "隔离观察：无侵入临时恢复成功，未修改脚本",
                    "fix": f"observe 临时恢复（{rec.get('mode')}）",
                    "recovered": True,
                }
                governor.record_pass(
                    method=method, path=path, case_name=case.name,
                    failure_class=gd["category"],
                )
                return result
            # 恢复失败 → 继续隔离观察（记录已在上方），不改脚本。
            result.llm_notes.setdefault("observe_tried", []).append(rec)
            return result
        # heal → 落到下方既有 AgentHeal 闭环。

    # 自愈：反射并重新验证，最多尝试 max_heal_attempts 次。
    if hooks.heal and result.response is not None:
        for attempt in range(1, hooks.max_heal_attempts + 1):
            try:
                ok, meta = hooks.heal(case, result.response, exc, attempt)
                if ok:
                    # 通过候选修复重跑后自愈成功。
                    result.succeeded = True
                    # 自愈触发条件 = 原始失败 + L1 重试仍失败。
                    trigger = str(exc)
                    # 自愈原因：优先取 LLM 根因分析（AgentTriage），否则给通用说明。
                    triage = result.llm_notes.get("triage") or {}
                    reason = (
                        str(triage.get("root_cause"))
                        or str(triage.get("llm_off_reason"))
                        or "LLM 反思判定可通过修正用例修复"
                    )
                    # 修复方案：LLM 给出的修正（断言/候选请求）摘要，无则用通用描述。
                    heal_fix = "L2 LLM 反思给出候选修复并重跑验证通过"
                    meta_fix = _heal_fix_summary(meta)
                    if meta_fix:
                        heal_fix += f"：{meta_fix}"
                    result.llm_notes["healed"] = {
                        "level": "L2",
                        "mode": "llm",
                        "trigger": trigger,
                        "reason": reason,
                        "fix": heal_fix,
                        "meta": meta,
                    }
                    if governor is not None:
                        gov_cat = (result.llm_notes.get("failure_governance") or {}) \
                            .get("category", "")
                        governor.record_pass(
                            method=method, path=path, case_name=case.name,
                            failure_class=gov_cat,
                        )
                    if tracer is not None:
                        tracer.record_heal(
                            case.name, level="2", mode="llm", succeeded=True, meta=meta
                        )
                    return result
                result.llm_notes.setdefault("heal", []).append(meta)
                if tracer is not None:
                    tracer.record_heal(
                        case.name, level="2", mode="llm", succeeded=False, meta=meta
                    )
            except Exception as her:
                log.warning("heal hook failed for %s: %s", case.name, her)
    return result


def run_flow(
    steps: list[Case],
    ctx: Context,
    client: HTTPClient,
    hooks: Optional[LLMHooks] = None,
    tracer: Any = None,
    gate: Any = None,
) -> list[StepResult]:
    """顺序执行，同一链内遵循 stop/continue，并为每个计划步骤保留记录。"""
    return execute_flow(
        steps, ctx,
        lambda step, context: _run_step(
            step, context, client, hooks=hooks, tracer=tracer, gate=gate
        ),
        hooks=hooks,
    )


def run_single(
    case: Case,
    ctx: Context,
    client: HTTPClient,
    hooks: Optional[LLMHooks] = None,
    tracer: Any = None,
    gate: Any = None,
    governor: Any = None,
) -> StepResult:
    """独立执行也先检查严格依赖，禁止绕过流程策略发送请求。"""
    return _execute_chain(
        [case], ctx,
        lambda step, context: _run_step(
            step, context, client, hooks, tracer, gate, governor
        ),
    )[0]


def _run_step(
    case: Case,
    ctx: Context,
    client: HTTPClient,
    hooks: Optional[LLMHooks] = None,
    tracer: Any = None,
    gate: Any = None,
    governor: Any = None,
) -> StepResult:
    """执行一步并统一验证结论；严格模式仅发布本步骤验证成功的产出。"""
    start = time.perf_counter()
    step_ctx = Context(deepcopy(ctx.as_dict())) if case.strict else ctx
    result = _run_single(case, step_ctx, client, hooks, tracer, gate, governor)
    result.duration = time.perf_counter() - start
    # 记录开始/结束时间戳：Agent 用结束时间与它的最后一次提交比对，
    # 判断本次回归是否覆盖了当前版本（未捕获时保持空串）。
    result.started_at = _now_iso()
    result.ended_at = _now_iso()
    if result.status != "executed":
        result.verdict = "unverified"
    elif not result.succeeded:
        result.verdict = "failed"
    elif not result.assertions:
        result.verdict = "unverified"
        result.reason = "步骤没有已评估的断言"
        if case.strict:
            result.succeeded = False
    elif any(not a.get("passed") for a in result.assertions) or (
        case.strict and result.error is not None
    ):
        result.verdict = "failed"
        if case.strict:
            result.succeeded = False
    else:
        result.verdict = "passed"
        if case.strict:
            for key, value in result.extracted.items():
                ctx.set(key, deepcopy(value))
    if case.strict and result.verdict != "passed":
        result.extracted = {}
    if not result.reason and result.error:
        result.reason = str(result.error)
    return result


def execute_flow(
    steps: list[Case],
    ctx: Context,
    execute: Callable[[Case, Context], StepResult],
    *,
    hooks: Optional[LLMHooks] = None,
) -> list[StepResult]:
    """所有入口共用的顺序策略；execute 负责客户端路由及单步跨度。"""
    def scope(step: Case) -> tuple[str, Optional[str], bool]:
        return (step.source_path, step.scenario, step.strict)

    scopes = {scope(step) for step in steps}
    if len(scopes) > 1 and any(step.strict for step in steps):
        # 多严格场景可接收相同显式种子，但不得看到彼此的运行产出。
        seed = deepcopy(ctx.as_dict())
        results: list[StepResult] = []
        for _, grouped in groupby(steps, key=scope):
            chain = list(grouped)
            context = Context(deepcopy(seed)) if chain[0].strict else ctx
            results.extend(_execute_chain(chain, context, execute))
    else:
        results = _execute_chain(steps, ctx, execute)
    if hooks and hooks.on_complete:
        try:
            hooks.on_complete(ctx, results)
        except Exception as exc:
            log.warning("on_complete hook failed for scenario: %s", exc)
    return results


def _execute_chain(
    steps: list[Case], ctx: Context, execute: Callable[[Case, Context], StepResult],
) -> list[StepResult]:
    """单作用域内执行；无效严格计划在任何请求前整体阻断。"""
    # 声明式夹具：把作用域开头步骤携带的 variables 一次性 materialize 进 ctx，
    # 使整个链共享同一批生成值（username/phone 等在不同步骤间保持一致）。
    if steps and steps[0].variables:
        materialize_variables(steps[0].variables, ctx)
    results: list[StepResult] = []
    by_name: dict[str, StepResult] = {}
    halted: Optional[str] = None
    duplicates = {name for name, count in Counter(step.name for step in steps).items() if count > 1}
    seen: set[str] = set()
    invalid: dict[int, str] = {}
    for index, step in enumerate(steps):
        if step.strict:
            if step.name in duplicates:
                invalid[index] = f"严格步骤名重复: {step.name}"
            elif any(name not in seen for name in step.depends_on):
                invalid[index] = "依赖步骤不存在或为前向引用"
        seen.add(step.name)
    if invalid:
        reason = "严格流程计划无效: " + "; ".join(invalid.values())
        invalid = {index: reason for index, step in enumerate(steps) if step.strict}
    for index, step in enumerate(steps):
        unavailable = [
            name for name in step.depends_on
            if name not in by_name or by_name[name].verdict != "passed"
        ] if step.strict else []
        if index in invalid or unavailable or halted:
            status = "blocked" if index in invalid or unavailable else "skipped"
            reason = (
                invalid[index] if index in invalid else
                f"依赖未验证成功: {', '.join(unavailable)}"
                if unavailable else f"stop 策略在步骤 {halted!r} 后停止"
            )
            result = StepResult(
                name=step.name, succeeded=False, attempts=0,
                status=status, verdict="unverified", reason=reason,
            )
        else:
            result = execute(step, ctx)
            if not result.succeeded and step.on_failure != "continue":
                halted = step.name
        results.append(result)
        by_name[step.name] = result
    return results


_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

def _now_iso() -> str:
    """当前本地时间 ISO 字符串（``%Y-%m-%dT%H:%M:%S``）。

    用于给步骤/运行打上开始、结束时间戳，Agent 据此判断「这次验证覆盖的是哪一版代码」。
    """
    return datetime.now().strftime(_TS_FORMAT)

def summarize_results(results: list[Any]) -> dict[str, int]:
    """按计划/执行/验证计数；assertions 仅统计终态实际评估数，非计划断言数。"""
    counts = dict.fromkeys(
        ("planned", "executed", "passed", "failed", "blocked", "skipped",
         "unverified", "assertions", "assertions_passed", "assertions_failed"), 0
    )
    counts["planned"] = len(results)
    for result in results:
        status = getattr(result, "status", "executed")
        label = getattr(result, "result", "")
        if status == "executed" and label in {"BLOCKED", "SKIP"}:
            status = "blocked" if label == "BLOCKED" else "skipped"
        if status in {"executed", "blocked", "skipped"}:
            counts[status] += 1
        verdict = getattr(result, "verdict", "")
        if not verdict:
            # 历史 PASS/执行成功不补造验证结论；明确失败仍保留失败事实。
            verdict = (
                "failed" if label in {"FAIL", "ERROR"} or (
                    not label and not getattr(result, "succeeded", False)
                ) else "unverified"
            )
        if verdict in {"passed", "failed", "unverified"}:
            counts[verdict] += 1
        for assertion in getattr(result, "assertions", []):
            counts["assertions"] += 1
            counts["assertions_passed" if assertion.get("passed") else "assertions_failed"] += 1
    return counts

