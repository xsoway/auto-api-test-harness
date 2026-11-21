"""报告生成。

``build_report(results, trace_urls)`` 写入 ``artifacts/reports/report.md``
（运行时间戳、环境、各用例摘要表）以及机器可读的
``report.json``。输入记录是运行期间收集的 :class:`ResultRecord` 实例；
:class:`ResultRecord` 也可以通过 :meth:`ResultRecord.from_step`
从 StepResult 构造。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ah.core.config import settings
from ah.core.trace import get_tracer
from ah.evidence.laya_gate import STAGE_REPORT
from ah.utils.logging import get_logger

log = get_logger("report")

# 报告详情里值得展示的响应头（其余略，避免噪音/敏感泄露）。
_HEADER_KEEP = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "server",
        "x-request-id",
        "x-trace-id",
        "x-correlation-id",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "set-cookie",
        "location",
        "date",
    }
)


@dataclass
class ResultRecord:
    name: str
    result: str  # PASS | FAIL | SKIP | ERROR | BLOCKED | UNVERIFIED
    duration: float = 0.0
    #: 本用例实际开始/结束的 ISO 时间戳（``%Y-%m-%dT%H:%M:%S``）。
    #: 与 :attr:`duration` 一起构成「跑没跑完 + 覆盖哪一版」的时间证据；
    #: 旧记录/未捕获时为空串（对齐《让 Agent 自己跑回归》字段一）。
    started_at: str = ""
    ended_at: str = ""
    failure_msg: Optional[str] = None
    llm_notes: dict[str, Any] = field(default_factory=dict)
    request: Optional[dict[str, Any]] = None
    response: Optional[dict[str, Any]] = None
    # 通过但内部触发过自愈（L1/L2）→ 标记为 flaky，体现"非首过即绿"。
    flaky: bool = False
    heal_level: Optional[str] = None
    assertions: list[dict[str, Any]] = field(default_factory=list)
    #: 本用例配置的请求超时（秒，仅当显式配置；未配置为 None）。
    timeout: Optional[float] = None
    #: 软断言失败条数（soft_assert 用例捕获的非阻断失败数；硬断言恒 0）。
    soft_failures: int = 0
    status: str = "executed"
    verdict: str = ""
    reason: str = ""
    attempts: int = 1
    _RESP_SNIPPET = 500

    @classmethod
    def from_step(cls, step: Any, *, duration: float = 0.0) -> "ResultRecord":
        succeeded = bool(step.succeeded)
        status = getattr(step, "status", "executed")
        verdict = getattr(step, "verdict", "") or ("unverified" if succeeded else "failed")
        reason = getattr(step, "reason", "")
        if verdict == "unverified" and not reason:
            reason = "缺少已验证断言证据"
        label = (
            "BLOCKED" if status == "blocked" else "SKIP" if status == "skipped"
            else "UNVERIFIED" if verdict == "unverified"
            else "PASS" if verdict == "passed" else "FAIL"
        )
        notes = dict(step.llm_notes or {})
        healed = notes.get("healed") if isinstance(notes.get("healed"), dict) else None
        # flaky = 终态通过但内部触发过自愈（L1 重试 / L2 LLM 修复）。
        flaky = label == "PASS" and healed is not None
        heal_level = str(healed.get("level", "")) if healed else None
        resp_meta: dict[str, Any] = {}
        if step.response is not None:
            resp_meta = {
                "status_code": step.status_code,
                "text": (
                    step.response.text[: cls._RESP_SNIPPET]
                    if step.response.text
                    else None
                ),
            }
            if step.response.text and len(step.response.text) > cls._RESP_SNIPPET:
                resp_meta["truncated"] = True
                resp_meta["full_len"] = len(step.response.text)
            head = getattr(step.response, "headers", None)
            if head is not None:
                sel = {k: v for k, v in head.items() if k.lower() in _HEADER_KEEP}
                if sel:
                    resp_meta["headers"] = sel
            ck = getattr(step.response, "cookies", None) or {}
            if ck:
                resp_meta["cookies"] = dict(ck)
            lat = getattr(step.response, "latency_ms", None)
            if lat is not None:
                resp_meta["latency_ms"] = round(lat, 1)
        return cls(
            name=step.name,
            result=label,
            duration=duration,
            failure_msg=str(step.error) if step.error else reason or None,
            llm_notes=notes,
            request=(
                dict(step.request_detail)
                if getattr(step, "request_detail", None)
                else None
            ),
            response=resp_meta or None,
            flaky=flaky,
            heal_level=heal_level,
            started_at=getattr(step, "started_at", "") or "",
            ended_at=getattr(step, "ended_at", "") or "",
            assertions=[dict(a) for a in (getattr(step, "assertions", None) or [])],
            timeout=(
                float(step.request_kwargs["timeout"])
                if getattr(step, "request_kwargs", None)
                and step.request_kwargs.get("timeout") is not None
                else None
            ),
            soft_failures=int(notes.get("soft_failures", 0) or 0),
            status=status,
            verdict=verdict,
            reason=reason,
            attempts=int(getattr(step, "attempts", 1)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResultRecord":
        """从序列化 dict 重建 ResultRecord（结果持久化）。

        旧格式记录缺 ``flaky`` 字段时，按与 :meth:`from_step` 一致的口径
        回退推导：终态 PASS 且 ``llm_notes.healed`` 存在 → 非首过（flaky）。
        """
        notes = dict(data.get("llm_notes") or {})
        result = str(data.get("result", "FAIL"))
        status = str(data.get("status", "executed"))
        verdict = str(data.get("verdict", ""))
        if status == "blocked":
            result = "BLOCKED"
        elif status == "skipped":
            result = "SKIP"
        elif verdict == "unverified":
            result = "UNVERIFIED"
        elif verdict == "failed":
            result = "FAIL"
        healed = notes.get("healed") if isinstance(notes.get("healed"), dict) else None
        flaky = bool(
            data.get("flaky", False)
            or (result == "PASS" and healed is not None)
        )
        return cls(
            name=str(data.get("name", "")),
            result=result,
            duration=float(data.get("duration", 0.0)),
            failure_msg=data.get("failure_msg"),
            llm_notes=notes,
            request=dict(data["request"]) if data.get("request") else None,
            response=dict(data["response"]) if data.get("response") else None,
            flaky=flaky,
            heal_level=data.get("heal_level") or (
                str(healed.get("level", "")) if healed else None
            ),
            assertions=[dict(a) for a in (data.get("assertions") or [])],
            timeout=(
                float(data["timeout"]) if data.get("timeout") is not None else None
            ),
            soft_failures=int(
                data.get("soft_failures")
                or (data.get("llm_notes") or {}).get("soft_failures")
                or 0
            ),
            status=status,
            verdict=verdict,
            reason=str(data.get("reason", "")),
            attempts=int(data.get("attempts", 1)),
            started_at=str(data.get("started_at", "")),
            ended_at=str(data.get("ended_at", "")),
        )



def _heal_summary(r: ResultRecord) -> str:
    """将自愈 + 根因分析结果渲染成一行摘要（空字符串表示未触发）。"""
    notes = r.llm_notes or {}
    parts: list[str] = []
    healed = notes.get("healed")
    if isinstance(healed, dict):
        level = healed.get("level")
        mode = healed.get("mode", "")
        if level == "L1":
            parts.append("L1 重试自愈")
        elif level == "L2":
            parts.append(f"L2 LLM 自愈 ({mode})")
        # 自愈原因（enriched：触发条件/原因/修复方案里优先给原因）。
        reason = healed.get("reason")
        if reason:
            parts.append(f"原因: {str(reason)}")
    if notes.get("L1_retry"):
        parts.append("L1 重试失败")
    triage = notes.get("triage")
    if isinstance(triage, dict):
        cause = triage.get("root_cause") or triage.get("llm_off_reason") or triage.get("analysis")
        if cause:
            parts.append(f"根因: {str(cause)}")
    return "；".join(parts)
def _heal_fields(r: ResultRecord) -> list[dict[str, str]]:
    """抽出自愈的「触发条件 / 原因 / 修复方案」，供 HTML 结构化展示。

    仅当该记录确实触发过自愈（``llm_notes.healed`` 存在）才返回非空列表，
    依次为 trigger/reason/fix 三行；缺哪个字段就只列已存在的（保持向后兼容
    旧报告里不含这些字段的 healed 记录）。
    """
    healed = (r.llm_notes or {}).get("healed")
    if not isinstance(healed, dict):
        return []
    rows: list[dict[str, str]] = []
    if healed.get("trigger"):
        rows.append({"k": "触发条件", "v": str(healed["trigger"])})
    if healed.get("reason"):
        rows.append({"k": "原因", "v": str(healed["reason"])})
    if healed.get("fix"):
        rows.append({"k": "修复方案", "v": str(healed["fix"])})
    return rows
def _prediction_section(
    plan: Any,
    key_interfaces: Optional[list[dict[str, Any]]] = None,
    *,
    llm_advice: str = "",
) -> list[str]:
    """把预测分析渲染成 md 行（分类建议 + 重点接口 + 高风险榜 + LLM 预测建议）。

    ``key_interfaces`` 为经验回退的"历史累积失败"重点关注接口；为空时仅渲染
    风险榜。``llm_advice`` 为 LLM 生成的预测分析建议（可为空）。两者皆空
    （无历史信号）返回空列表。
    """
    focused = [p for p in (plan.focused if plan is not None else []) if p.risk > 0]
    if not focused and not key_interfaces and not llm_advice:
        return []
    lines = ["## 预测分析 · 重点关注", ""]
    lines.append(
        "基于历史运行累积的风险概率（风险模型）与逐运行失败次数（经验回退）"
        "推演。回归覆盖建议：`smoke`（冒烟）→ `regression`（回归）→ `prod`"
        "（生产回归），可用 `--ah-suite / --ah-env / --ah-priority` 筛选抽取。"
    )
    lines.append("")
    if key_interfaces:
        lines.append("### 重点关注接口（历史累积失败最多）")
        lines.append("| 接口 | 历史失败次数 | 本次 |")
        lines.append("| --- | --- | --- |")
        for k in key_interfaces:
            latest = "未通过" if k["latest"] else "通过"
            lines.append(f"| {k['interface']} | {k['failures']} | {latest} |")
        lines.append("")
    if focused:
        lines.append("### 风险预测（回归必测，风险 > 0）")
        lines.append("| Interface | Risk | Confidence | Failures/Total | 主要失败类别 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for p in focused:
            cls = ", ".join(p.top_failure_classes[:3]) or "—"
            lines.append(
                f"| {p.interface} | {p.risk:.2f} | {p.confidence:.2f} "
                f"| {p.failures}/{p.total} | {cls} |"
            )
        lines.append("")
    if llm_advice:
        lines.append("### 预测分析 · LLM 建议")
        lines.append("")
        lines.append(llm_advice)
    return lines

def _case_slug(name: str, index: int) -> str:
    """给用例做稳定锚点 id（失败分析可链接回用例详情）。"""
    base = re.sub(r"[^0-9a-zA-Z]+", "-", name).strip("-").lower() or "case"
    return f"d{index}-{base[:40]}"


def _failure_category(r: "ResultRecord") -> str:
    """给失败用例归类（用于聚合条形图与失败明细表）。"""
    msg = r.failure_msg or ""
    if msg.startswith("{status_code}"):
        return "status_code"
    if msg.startswith("{"):
        key = msg.split("}", 1)[0][1:]
        return key if key else "assert"
    if "HTTP" in msg.upper() or r.response is None:
        return "http/网络"
    return "其他断言"


_SEVERITY_QUESTIONS: dict[str, Any] = {
    "severity": {
        "type": "choice",
        "instructions": "这次接口失败/告警的严重度属于哪一类？",
        "criteria": {
            "high": "关键接口失败或数据破坏，需立即处理",
            "medium": "功能受影响但可降级",
            "low": "边缘情况/噪音，可忽略",
            "other": "无法归类",
        },
    }
}


def _severity_state(failure_msg: Optional[str], method: str = "", path: str = "") -> str:
    """构造 Laya 可读的严重度决策上下文（业务描述，不使用内部函数名）。"""
    parts = [f"接口: {method or '未知方法'} {path or '未知路径'}"]
    msg = (failure_msg or "").strip().replace("\n", " ")[:200]
    if msg:
        parts.append(f"失败消息: {msg}")
    return "；".join(parts)


def _rule_severity(failure_msg: Optional[str], method: str = "", path: str = "") -> str:
    """确定性严重度判定（Laya 未开闸/回退时的原规则）。

    与 :func:`_failure_category` 同源归类语义，映射到 高/中/低 三级：
    破坏性/写方法（POST/PUT/DELETE/PATCH）或 HTTP/状态码失败 → 高；普通
    断言不符/网络异常 → 中；无消息/边缘情况 → 低。作为后端裁决的稳定
    回退基线（字节等价，不依赖 Laya）。
    """
    msg = (failure_msg or "").upper()
    if method.upper() in ("POST", "PUT", "DELETE", "PATCH"):
        return "high"
    if msg.startswith("{STATUS_CODE") or " HTTP " in f" {msg} ":
        return "high"
    if not failure_msg:
        return "low"
    return "medium"


def severity_classify(
    failure_msg: Optional[str],
    method: str = "",
    path: str = "",
    *,
    gate: Any = None,
) -> tuple[str, bool]:
    """报告失败严重度分级前置分流（LayaGate STAGE_REPORT）。

    gate 非 None 且该环节开闸时，先咨询 LayaGate：高置信 AUTO 且
    value∈{high,medium,low} 直接采用裁决（signal=True）；否则（未开闸/
    低置信/异常）回退下方确定性规则 :func:`_rule_severity`（字节等价）。
    gate=None（默认/off）→ 完全跳过，零网络。返回 (severity, signal)，
    signal 表示该次判定是否采纳了 Laya 裁决。
    """
    rule = _rule_severity(failure_msg, method, path)
    if gate is not None:
        try:
            dec = gate.best(
                STAGE_REPORT,
                _severity_state(failure_msg, method, path),
                _SEVERITY_QUESTIONS,
            )
            if dec.get("action") == "auto" and dec.get("value") in ("high", "medium", "low"):
                return dec["value"], True
        except Exception as gate_exc:  # Laya 故障绝不改变原流程
            log.warning("laya report 严重度分流失败，回退确定性判定: %s", gate_exc)
    return rule, False


def _parse_diff(msg: str) -> Optional[dict[str, str]]:
    """从断言消息解析 expected/actual 用于并排对比（B2）。

    支持的格式 ``{key} expected X, got Y``。无法解析返回 None（回退纯文本）。
    """
    m = re.search(r"\bexpected (.+?),\s+got (.+)$", msg)
    if not m:
        return None
    expected = m.group(1).strip()
    actual = m.group(2).strip()
    if not expected or not actual:
        return None
    return {"expected": expected[:200], "actual": actual[:400]}


def _trace_id_from(resp: Optional[dict[str, Any]]) -> Optional[str]:
    """从响应 headers 提取后端追踪 ID（大小写不敏感：x-trace-id / x-request-id）。"""
    if not resp:
        return None
    head = resp.get("headers") or {}
    for k in ("x-trace-id", "x-request-id", "x-correlation-id"):
        if head.get(k):
            return str(head[k])
        low = next((v for kk, v in head.items() if kk.lower() == k and v), None)
        if low:
            return str(low)
    return None


def _trace_link(trace_id: str) -> Optional[str]:
    """Langfuse 官方 trace 深度链接（``/project/{projectId}/traces/{id}``）。

    由 v4 SDK 的 ``get_trace_url`` 生成（官方正确格式，含 projectId）；
    SDK 不可用或追踪禁用（`NullTracer`）时返回 None。
    """
    try:
        tracer = get_tracer()
        fn = getattr(tracer, "trace_url", None)
        if fn is None:
            return None
        return fn(trace_id=trace_id)
    except Exception:  # 追踪/链接失败绝不中断报告
        return None




def _run_trace_link() -> Optional[str]:
    """整次运行的 Langfuse 深度链接（用 tracer.run_trace_id）。

    真实 tracer 启用且持有 run_trace_id 时返回官方链接；追踪禁用/无 id/无
    trace_url 方法时返回 None（不中断报告）。
    """
    try:
        tracer = get_tracer()
        if not getattr(tracer, "enabled", False):
            return None
        rid = getattr(tracer, "run_trace_id", None)
        if not rid:
            return None
        fn = getattr(tracer, "trace_url", None)
        if fn is None:
            return None
        return fn(trace_id=rid)
    except Exception:  # 生成失败绝不中断报告
        return None

def _p95(vals: list[float]) -> Optional[float]:
    """最近秩 P95（升序）。空列表返回 None。"""
    if not vals:
        return None
    s = sorted(vals)
    return s[int(round(0.95 * (len(s) - 1)))]


def _percentile(vals: list[float], q: float) -> Optional[float]:
    """线性插值分位数（升序），用于耗时分布 avg/p95/p99。空列表返回 None。"""
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    frac = pos - lo
    return s[lo] + frac * (s[lo + 1] - s[lo])


def _dur_ms(sec: float) -> float:
    """耗时秒 → 毫秒（保留 1 位）。"""
    return round(sec * 1000, 1) if sec else 0.0


def _env_fingerprint() -> dict[str, str]:
    """当前环境指纹（用于跨环境回归守卫）。"""
    return {"base_url": settings.base_url or ""}

def _actual_targets(results: list[Any]) -> str:
    """聚合本次运行实际命中的请求目标（host），供报告「目标 base URL」显示。

    每个结果的 ``request.path`` 若是绝对 URL 则取其 host；相对路径回退到
    ``settings.base_url`` 的 host（缺省目标的兜底）。去重（保持出现顺序）并
    去端口号，带计数。唯一目标返回单个 host；多个返回「host1 / host2 …」
    （至多 3 个，超出加「等 N 个」）。空则返回空串。
    """
    hosts: list[str] = []
    for r in results:
        req = getattr(r, "request", None) or {}
        path = (req or {}).get("path") if isinstance(req, dict) else getattr(req, "path", None)
        if not isinstance(path, str) or not path:
            continue
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme and parsed.netloc:
            host = parsed.hostname or ""
        else:
            host = urllib.parse.urlsplit(settings.base_url).hostname or settings.base_url
        if host and host not in hosts:
            hosts.append(host)
    if not hosts:
        return ""
    if len(hosts) == 1:
        return hosts[0]
    shown, extra = hosts[:3], len(hosts) - 3
    tail = f" 等 {extra} 个" if extra > 0 else ""
    return " / ".join(shown) + tail

def _md_escape(text: str) -> str:
    """HTML 转义 + 行内 markdown（**bold** / `code`）转 HTML 标签。

    先转义再套标签：``<``/``>``/``&`` 先被转义，``**`` 与反引号不受影响，
    因此可安全地在转义结果上做行内替换，避免用户/LLM 内容里注入 HTML。
    """
    esc = (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc, flags=re.S)
    esc = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc)
    return esc


def _md_to_html(md: str) -> str:
    """轻量 markdown → 结构化 HTML（单文件自包含，不引入外部渲染库）。

    支持 ``##``/``###`` 标题、连续 ``- `` 无序列表（含 2/4 空格缩进嵌套）、
    ``**bold**`` 与 `` `code` `` 行内样式、空行分段。用于把「运行分析 /
    LLM 建议」从裸 md 渲染成友好 HTML。
    """
    if not md or not str(md).strip():
        return ""
    lines = [ln.rstrip() for ln in str(md).splitlines()]
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        # 标题
        m = re.match(r"^(#{2,4})\s+(.+)$", ln)
        if m:
            lvl = {2: "h3", 3: "h4", 4: "h5"}[min(len(m.group(1)), 4)]
            out.append(f"<{lvl}>{_md_escape(m.group(2))}</{lvl}>")
            i += 1
            continue
        # 无序列表（含缩进嵌套）
        if re.match(r"^([ \t]*)-[ \t]+", ln):
            items: list[tuple[int, str]] = []
            j = i
            while j < n:
                lm = re.match(r"^([ \t]*)-[ \t]+(.+)$", lines[j])
                if not lm:
                    break
                items.append((len(lm.group(1)), lm.group(2)))
                j += 1
            i = j
            out.append(_render_md_list(items))
            continue
        # 普通段落：合并连续非空、非标题、非列表行
        para: list[str] = [ln]
        i += 1
        while i < n and lines[i].strip():
            pm = re.match(r"^(#{2,4})\s+", lines[i])
            plm = re.match(r"^([ \t]*)-[ \t]+", lines[i])
            if pm or plm:
                break
            para.append(lines[i])
            i += 1
        out.append(f"<p>{_md_escape(' '.join(para))}</p>")
    return "\n".join(out)


def _render_md_list(items: list[tuple[int, str]]) -> str:
    """把 (缩进深度, 行文本) 列表渲染成嵌套 <ul><li>。"""
    parts: list[str] = ["<ul>"]
    prev_indent = items[0][0]
    for indent, text in items:
        if indent > prev_indent:
            parts.append("<ul>")
        elif indent < prev_indent:
            parts.append("</ul>")
        parts.append(f"<li>{_md_escape(text)}</li>")
        prev_indent = indent
    parts.append("</ul>")
    return "".join(parts)
def _trend_svg(trend: list[dict[str, Any]]) -> str:
    """通过率趋势 → 内联 SVG 线性图（无 JS）。返回空串当不足 2 个点。"""
    pts = [(t.get("run_time", "") or "", t.get("pass_rate")) for t in trend]
    rows = [(t, p) for t, p in pts if isinstance(p, (int, float))]
    if len(rows) < 2:
        return ""
    w, h = 620, 220
    pad_l, pad_r, pad_t, pad_b = 34, 14, 18, 30
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    n = len(rows)

    def y(rate: float) -> float:
        return pad_t + plot_h * (1 - max(0.0, min(100.0, rate)) / 100.0)

    xs = [pad_l + plot_w * i / (n - 1) for i in range(n)]
    ys = [y(p) for _, p in rows]
    poly = " ".join(f"{x:.1f},{y_:.1f}" for x, y_ in zip(xs, ys))
    # x 轴时间标注：全部在同一天 → HH:MM；跨天 → MM-DD（run_time 为 YYYY-MM-DD HH:MM:SS）。
    dates = {t[:10] for t, _ in rows}
    fmt = "%H:%M" if len(dates) <= 1 else "%m-%d"
    axis_labels = [
        (datetime.strptime(t, "%Y-%m-%d %H:%M:%S").strftime(fmt) if len(t) >= 19 else t)
        for t, _ in rows
    ]
    dots = "".join(
        f'<g class="trend-dot" data-tip="{t} · {p:.0f}% 通过率">'
        f'<title>{t} · {p:.0f}% 通过率</title>'
        f'<circle cx="{x:.1f}" cy="{y_:.1f}" r="3.6" '
        f'fill="{("#c05b5b" if p < 90 else "#18a167")}"/></g>'
        for x, y_, p, t in zip(xs, ys, [p for _, p in rows], [t for t, _ in rows])
    )
    labels = "".join(
        f'<text x="{x:.1f}" y="{y_ - 8:.1f}" text-anchor="middle" '
        f'class="tv">{p:.0f}%</text>'
        for x, y_, p in zip(xs, ys, [p for _, p in rows])
    )
    tlabels = "".join(
        f'<text x="{x:.1f}" y="{h - 8}" text-anchor="middle" class="tt">'
        f"{lab}</text>"
        for x, lab in zip(xs, axis_labels)
    )
    grid = ""
    for gv in (0, 50, 100):
        gy = y(gv)
        grid += (
            f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{w - pad_r}" '
            f'y2="{gy:.1f}" class="tg"/>'
            f'<text x="{pad_l - 6}" y="{gy + 3:.1f}" text-anchor="end" '
            f'class="tv">{gv}</text>'
        )
    return (
        f'<svg class="trend-svg" viewBox="0 0 {w} {h}" '
        f'xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="通过率趋势线性图">'
        f"<defs><linearGradient id=\"tlw\" x1=\"0\" y1=\"0\" "
        f"x2=\"0\" y2=\"1\">"
        f"<stop offset=\"0\" stop-color=\"#18a167\" stop-opacity=\".18\"/>"
        f"<stop offset=\"1\" stop-color=\"#18a167\" stop-opacity=\"0\"/>"
        f"</linearGradient></defs>"
        f"{grid}"
        f'<polygon points="{poly} {xs[-1]:.1f},{pad_t + plot_h} '
        f'{xs[0]:.1f},{pad_t + plot_h}" fill="url(#tlw)"/>'
        f'<polyline points="{poly}" fill="none" class="tp"/>'
        f"{dots}{labels}{tlabels}</svg>"
    )
def _status_delta(cur: dict[Any, int], prev: dict[Any, int]) -> str:
    """状态码分布变化摘要：新增 / 消失的码各带计数。无变化返回空串。"""
    cs = {str(k): int(v) for k, v in (cur or {}).items()}
    ps = {str(k): int(v) for k, v in (prev or {}).items()}
    added = sorted(set(cs) - set(ps))
    gone = sorted(set(ps) - set(cs))
    parts: list[str] = []
    if added:
        parts.append("新增 " + ", ".join(f"{k}:{cs[k]}" for k in added))
    if gone:
        parts.append("消失 " + ", ".join(f"{k}:{ps[k]}" for k in gone))
    return "；".join(parts)
def _key_interfaces(
    runs: list[dict[str, Any]], cur_cases: dict[str, str], top: int = 8
) -> list[dict[str, Any]]:
    """从逐运行历史累积"重点关注接口"：先按历史失败次数，再按当前未通过。

    ``runs`` 为 ``report_history.jsonl`` 读回的逐运行聚合行（含 ``cases``
    的 名称→结果 映射）。累积每个接口的历史失败数，失败并列时当前 run
    仍失败的排前。返回至多 ``top`` 条：``{interface, failures, latest}``。
    全为稳定通过（无历史失败且当前全 PASS）时返回空列表。
    """
    failures: dict[str, int] = {}
    for run in runs:
        for name, res in (run.get("cases") or {}).items():
            if res != "PASS":
                failures[name] = failures.get(name, 0) + 1
    if not failures:
        return []
    latest_fail = {n for n, res in cur_cases.items() if res != "PASS"}
    ranked = sorted(
        failures.items(),
        key=lambda kv: (-kv[1], kv[0] not in latest_fail),
    )
    return [
        {
            "interface": name,
            "failures": cnt,
            "latest": name in latest_fail,
        }
        for name, cnt in ranked[:top]
    ]

def _load_run_history(path: Path) -> list[dict[str, Any]]:
    """读回逐运行历史（A3 回归 + 通过率趋势）。文件不存在返回空列表。"""
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    except (ValueError, OSError):
        log.warning("report history unreadable: %s", path)
    return rows

def _mark_flaky_from_history(
    results: list[ResultRecord],
    prior_runs: list[dict[str, Any]],
    *,
    min_obs: int = 3,
) -> None:
    """把跨 run 通/败抖动的接口标为 flaky（rule-based，无 LLM）。

    规则（提案 §4.2）：同一接口在历史 + 本次运行里累计观测数 ≥ ``min_obs``，
    且既有 PASS 又有 FAIL（说明结果不可复现、随运行抖动 → 判定 flaky）。
    就地修改 ``results`` 里对应记录的 ``flaky`` 字段（与"经自愈"口径合并）。
    """
    # 历史与当前都按用例名对齐（report_history.jsonl 的 cases 以 name 为键）。
    def _interface_key(r: ResultRecord) -> str:
        return r.name

    # 累计接口级观测：{key: {"PASS": n, "FAIL": n}}
    counts: dict[str, dict[str, int]] = {}
    for run in prior_runs:
        for name, res in (run.get("cases") or {}).items():
            if res not in ("PASS", "FAIL"):
                continue
            bucket = counts.setdefault(name, {"PASS": 0, "FAIL": 0})
            bucket[res] += 1
    for r in results:
        if r.result not in ("PASS", "FAIL"):
            continue
        bucket = counts.setdefault(_interface_key(r), {"PASS": 0, "FAIL": 0})
        bucket[r.result] += 1
    for r in results:
        key = _interface_key(r)
        bucket = counts.get(key)
        if not bucket:
            continue
        total = bucket["PASS"] + bucket["FAIL"]
        jitter = total >= min_obs and bucket["PASS"] > 0 and bucket["FAIL"] > 0
        if jitter:
            r.flaky = True

def _append_run_history(path: Path, record: dict[str, Any]) -> None:
    """把本次运行追加进逐运行历史（一行一条 JSON）。"""
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        log.warning("failed to append report history: %s", path)


def _semantic_verdicts_agg(results: list[ResultRecord]) -> dict[str, Any]:
    """汇聚全部记录的 ``assert_llm`` 语义判定（R3 报告/CI 落点）。

    从 ``llm_notes["assert_llm"]``（单条 dict 或 list of dict）汇总
    SKIP/PASS/UNVERIFIED 计数与需人工复核（flagged）数。无任何判定时输出
    全 0 的确定性骨架（hermetic 友好，可被 CI 空态消费）。不做硬闸。
    """
    agg = {"SKIP": 0, "PASS": 0, "UNVERIFIED": 0, "flagged": 0}
    for r in results:
        notes = (r.llm_notes or {}) if hasattr(r, "llm_notes") else {}
        vs = notes.get("assert_llm")
        if isinstance(vs, dict):
            vs = [vs]
        if not isinstance(vs, list):
            continue
        for v in vs:
            if not isinstance(v, dict):
                continue
            verdict = v.get("verdict")
            if verdict in agg:
                agg[verdict] += 1
            if v.get("flagged"):
                agg["flagged"] += 1
    return agg


#: 框架新增功能能力矩阵（编号 1–14,17 为既有能力；15,16,18–22 为本次 7 项
#: 测试框架缺口：重试/timeout/并行/profile/调试·OpenAPI/软断言/占位符）。
#: 每行：id/name 功能点、module 归属模块、cmd 对应 `ah` 子命令。used 在
#: :func:`_capability_matrix` 按本次运行信号动态打标。
_CAPABILITY_ROWS: list[dict[str, str]] = [
    {"id": 1, "name": "自主涌现闭环", "module": "evolution", "cmd": "run / evolve",
     "desc": "LLM 分析历史失败 → 涌现候选用例，自动归纳变更/风险场景"},
    {"id": 2, "name": "候选持久化（写回 yaml / undo）", "module": "evolution", "cmd": "evolve",
     "desc": "涌现/采纳的候选写回用例 yaml，支持 undo 回滚；失败不受扰动"},
    {"id": 3, "name": "变更驱动（git diff）", "module": "evolution", "cmd": "evolve --diff",
     "desc": "解析 git diff → 受影响的接口/用例，聚焦回归目标"},
    {"id": 4, "name": "质量门控", "module": "evolution", "cmd": "evolve",
     "desc": "采纳候选前跑质量闸（通过率/失败类别），不达标拒绝并留痕"},
    {"id": 5, "name": "业务项目脚手架", "module": "scaffold", "cmd": "scaffold",
     "desc": "一键生成业务项目骨架（conftest/report/数据目录隔离）"},
    {"id": 6, "name": "测试入口生成", "module": "scaffold", "cmd": "scaffold --gen-entry",
     "desc": "从用例集生成可直接跑的 pytest 入口脚本（test_*.py）"},
    {"id": 7, "name": "自然语言 → 用例", "module": "scaffold", "cmd": "add-case",
     "desc": "用自然语言描述接口/场景，LLM 生成结构化 yaml 用例"},
    {"id": 8, "name": "回归调度", "module": "scheduler", "cmd": "scheduled-run",
     "desc": "按 suite/env 计划自动跑回归，可 --stub 离线判闸"},
    {"id": 9, "name": "变更触发", "module": "scheduler", "cmd": "ci-trigger",
     "desc": "CI 变更触发回归（diff 定位受影响用例）"},
    {"id": 10, "name": "flaky 隔离仓", "module": "scheduler", "cmd": "flaky-quarantine",
     "desc": "连续 flaky 用例自动隔离，避免噪音污染主判闸"},
    {"id": 11, "name": "失败自动动作（bug 单草稿）", "module": "evolution", "cmd": "evolve",
     "desc": "失败用例自动生成 bug 单草稿，附根因分析"},
    {"id": 12, "name": "回归计划执行", "module": "evolution", "cmd": "evolve / run",
     "desc": "生成并按优先级执行回归计划（历史失败率加权）"},
    {"id": 13, "name": "趋势闸", "module": "evolution", "cmd": "trend-gate",
     "desc": "据历史运行趋势做稳定性判闸（insufficient_data 则跳过）"},
    {"id": 14, "name": "LLM 成本聚合", "module": "telemetry", "cmd": "telemetry",
     "desc": "每次 LLM 调用落账 llm_usage.jsonl，聚合成本/成功率/token"},
    {"id": 17, "name": "Hypothesis 属性探索", "module": "property", "cmd": "property",
     "desc": "基于属性的参数空间探索（@seed 确定性回归）"},
    {"id": 15, "name": "运行时可配置重试 retry", "module": "framework", "cmd": "scheduled-run",
     "desc": "用例级 retries/retry_backoff 配置，对瞬时错误（5xx/超时/连接）退避重试"},
    {"id": 16, "name": "用例级 timeout", "module": "framework", "cmd": "scheduled-run",
     "desc": "每个用例可配置请求超时（秒），覆盖客户端默认值"},
    {"id": 18, "name": "并行执行 parallel", "module": "framework",
     "cmd": "scheduled-run --parallel",
     "desc": "相互独立的用例链并发执行（ThreadPoolExecutor，同场景链内保序）"},
    {"id": 19, "name": "多环境 profile", "module": "config", "cmd": "scheduled-run --profile",
     "desc": "按 profile 切换 base_url/domains（AH_PROFILE 或 --profile 覆盖配置）"},
    {"id": 20, "name": "调试 / OpenAPI 导入", "module": "debug", "cmd": "debug / openapi",
     "desc": "ah debug 单用例 dump；ah openapi 把 OpenAPI paths 导入为最小用例"},
    {"id": 21, "name": "软断言 soft_assert", "module": "framework", "cmd": "scheduled-run",
     "desc": "用例级 soft_assert：断言失败只记录不判失败，供非阻断性检查"},
    {"id": 22, "name": "时间/随机占位符", "module": "context", "cmd": "scheduled-run",
     "desc": "模板生成 ${rand:uuid}/${rand:int:min-max}/${date:now}/${date:+Nd} 等占位值"},
    {"id": 23, "name": "决策模型前置层 Laya", "module": "framework", "cmd": "AH_LAYA=…",
     "desc": "LLM-judge 决策点前 LayaGate 高置信分流；低置信/未开闸回退原流程"},
    {"id": 24, "name": "失败治理链 governor", "module": "failure", "cmd": "run",
     "desc": "triage 6 分类 → 动作映射（heal/observe/block）决策链，零 LLM"},
    {"id": 25, "name": "失败簇群状态机 cluster", "module": "failure", "cmd": "flaky-quarantine",
     "desc": "指纹聚合失败簇 → active/flaky_watch/quarantined/recovered/resolved 落盘"},
    {"id": 26, "name": "无侵入观察恢复 observe", "module": "failure", "cmd": "run",
     "desc": "observe 临时恢复只重跑原步骤，成功不回写脚本（铁律）"},
    {"id": 27, "name": "失败知识库 knowledge", "module": "failure", "cmd": "run",
     "desc": "resolved/known_issue 草稿→发布→检索命中改分类动作→使用反馈闭环"},
    {"id": 28, "name": "调度簇群隔离", "module": "scheduler", "cmd": "flaky-quarantine",
     "desc": "隔离仓合并簇群 quarantined 状态用例（标签 ∪ 簇群）"},
]

#: 本次运行会真实触发的功能点集合（其余标记「可用·未用于本次运行」）。
#: evolution 族由 `_evolution_log.jsonl` 轮次 > 0 判定；LLM 成本由账本调用数 > 0 判定。
_EVOLUTION_IDS = {1, 2, 4, 11, 12}
_TELEMETRY_IDS = {14}


def _capability_matrix(
    evolution: Optional[dict[str, Any]],
    telemetry: Optional[dict[str, Any]],
    results: Optional[list[ResultRecord]] = None,
) -> dict[str, Any]:
    """由「演进/成本 + 本次运行记录」给能力点打 used 标 + 详情，返回能力矩阵。

    动态判定来源分两类：
    - 文件源：evolution 族（持久化/门控/bug 单/回归计划）仅当演进日志 rounds>0；
      LLM 成本仅当账本 requests>0（telemetry 已写库）。
    - 运行记录源（``results``）：重试（15）由 ``llm_notes.retried`` 触发判定；
      timeout（16）由用例配置了 case.timeout 判定；软断言（21）由捕获的
      soft_failures>0 判定。
    其余功能点（scaffold/scheduler/diff/趋势闸/property/parallel/profile/占位符
    等）是 CLI/调度层能力，本次 `test_*.py` 运行不会直接触发——如实标
    「可用·未用于本次运行」，绝不虚报。已用功能点附本次运行证据（evidence）。
    """
    used_ids: set[int] = set()
    if evolution and evolution.get("rounds", 0) > 0:
        used_ids |= _EVOLUTION_IDS
    if telemetry and telemetry.get("requests", 0) > 0:
        used_ids |= _TELEMETRY_IDS
    # ---- 运行记录驱动的能力：retry / timeout / soft_assert / Laya ----
    if not results:
        results = []
    _laya_hits = 0
    _laya_stages: list[str] = []
    _retried = 0
    _soft = 0
    _timeout_n = 0
    if results:
        for rec in results:
            notes = rec.llm_notes or {}
            if notes.get("retried"):
                _retried += 1
            if int(
                notes.get("soft_failures")
                or getattr(rec, "soft_failures", 0)
                or 0
            ) > 0:
                _soft += 1
            if getattr(rec, "timeout", None) is not None:
                _timeout_n += 1
            # —— Laya 决策模型信号（laya_* 注记 / 接口级 signal）——
            _notes = rec.llm_notes or {}
            if _notes.get("laya_triage") or _notes.get("laya_tool_risk") \
                    or _notes.get("laya_no_more_value"):
                _laya_hits += 1
            if getattr(rec, "laya_signal", None):  # 被 Laya 采纳打分的接口级信号
                _laya_hits += 1
                _laya_stages.extend(getattr(rec, "laya_signal", {}).keys())
        if _retried:
            used_ids.add(15)
        if _soft:
            used_ids.add(21)
        if _timeout_n:
            used_ids.add(16)
        # Laya 信号或显式开启 AH_LAYA → 决策模型能力点打 used；
        # 仅当确有信号产出或环境开启时才标，避免虚报。
        from ah.evidence.laya_gate import laya_enabled as _laya_env_on

        if _laya_hits or _laya_env_on():
            used_ids.add(23)
    def _laya_enabled_env() -> bool:
        """AH_LAYA 显式开启（非 off）→ 决策模型能力点视为本次可用。"""
        from ah.evidence.laya_gate import laya_enabled

        return laya_enabled()


    def _laya_evidence() -> Optional[str]:
        """决策模型能力点（23）证据：本次运行真实产生的 Laya 信号。"""
        if not (_laya_hits or _laya_enabled_env()):
            return None
        stages = "、".join(sorted(set(_laya_stages))) if _laya_stages else "—"
        return (f"触发条件：{'AH_LAYA 已开启' if _laya_enabled_env() else '本次存在 Laya 信号'}"
                f"；执行：LLM-judge 决策点前 LayaGate 高置信分流，低置信/未开闸回退原流程；"
                f"产物：{_laya_hits} 次 Laya 信号（triage/tool_risk/stop/接口风险评分，"
                f"环节 {stages}）")
    def _evo_evidence(cap_id: int) -> Optional[str]:
        """构造 evolution 族某功能点「本次运行」的触发/执行/产物描述。

        只描述本次真实发生的事（failures/candidates/plan 等均来自演进日志），
        未触发的子步骤如实写「未进入」，绝不虚报。
        """
        if not evolution:
            return None
        _r = evolution.get("rounds", 0) or 0
        _suite = evolution.get("planned_suite") or "—"
        _fail = evolution.get("failures", 0) or 0
        _cand = evolution.get("candidates", 0) or 0
        _acc = evolution.get("accepted", 0) or 0
        _rej = evolution.get("rejected", 0) or 0
        _cnames = evolution.get("candidate_names") or []
        _anames = evolution.get("accepted_names") or []
        _rnames = evolution.get("rejected_names") or []
        _focused = evolution.get("plan_focused") or []
        _allp = evolution.get("plan_all") or []
        def _join(xs: list) -> str:
            """渲染当前的接口画像和历史日志中的接口名称。"""
            labels = (x.get("interface") if isinstance(x, dict) else x for x in xs)
            return "、".join(str(label) for label in labels if label) or "无"
        if cap_id == 1:  # 自主涌现闭环
            trig = f"历史失败基线或本轮失败（本轮 {_fail} 例）"
            do = (f"按 {_suite} 计划分析失败 → LLM 涌现候选，关注计划 {_join(_focused)}"
                  if _focused else f"按 {_suite} 计划无历史失败源 → 无候选可涌现")
            prod = f"涌现候选 {_cand} 个：{_join(_cnames)}"
        elif cap_id == 2:  # 候选持久化
            trig = f"存在已采纳候选（本轮采纳 {_acc}）"
            do = "候选写回用例 yaml 并记录 undo 回滚点" if _acc else "候选为空 → 无写回"
            prod = f"修改用例 yaml {_acc} 处：{_join(_anames)}"
        elif cap_id == 4:  # 质量门控
            trig = f"存在待采纳候选（本轮候选 {_cand}）"
            do = ("跑质量闸（通过率/失败类别），裁定采纳或拒绝"
                  if _cand else "候选为空 → 质量门控未进入")
            prod = f"采纳 {_acc}（{_join(_anames)}）· 拒绝 {_rej}（{_join(_rnames)}）"
        elif cap_id == 11:  # 失败自动动作（bug 单草稿）
            trig = f"存在失败用例（本轮失败 {_fail}）"
            do = "识别失败用例 → 生成 bug 单草稿 + 根因分析" if _fail else "无失败用例 → 无草稿"
            prod = f"生成 bug 单草稿 {_fail} 份"
        elif cap_id == 12:  # 回归计划执行
            trig = f"已生成回归计划（focus {len(_focused)} / 全量 {len(_allp)}）"
            do = f"按失败记录生成 {_suite} 回归计划"
            prod = f"计划覆盖接口：{_join(_allp)}"
        else:  # 兜底：无专门描述
            trig = f"演进 {_r} 轮已运行"
            do = "按演进管道执行"
            prod = f"候选 {_cand} / 采纳 {_acc} / 拒绝 {_rej}"
        return (f"触发条件：{trig}；执行：{do}；产物：{prod} "
                f"（演进 {_r} 轮 · {_suite}）")

    def _tel_evidence() -> Optional[str]:
        """LLM 成本聚合功能点的本次证据：按用途(task)明细。"""
        if not telemetry or telemetry.get("requests", 0) <= 0:
            return None
        _parts = [
            f"{k} {v.get('requests', 0)} 次"
            for k, v in (telemetry.get("by_task") or {}).items()
        ]
        _by_task = "；".join(_parts) if _parts else "—"
        return (f"触发条件：LLM 调用发生；执行：每次调用落账 llm_usage.jsonl；"
                f"产物：聚合 {telemetry.get('requests', 0)} 次调用，"
                f"token {telemetry.get('total_tokens', 0)}，成本 "
                f"${telemetry.get('cost', 0.0):.4f}（用途 {_by_task}）")
    def _retry_evidence() -> Optional[str]:
        """重试能力（15）证据：本次运行触发配置重试的用例数。"""
        if not _retried:
            return None
        return (f"触发条件：本轮存在瞬时错误（5xx/超时/连接）；"
                f"执行：按用例级 retries/retry_backoff 退避重试并成功；"
                f"产物：{_retried} 个用例经配置重试恢复（llm_notes.retried）")

    def _timeout_evidence() -> Optional[str]:
        """用例级 timeout（16）证据：显式配置了超时的用例数。"""
        if not _timeout_n:
            return None
        return (f"触发条件：用例配置了 request timeout；"
                f"执行：将超时透传给 HTTPClient.request；"
                f"产物：{_timeout_n} 个用例显式配置了请求超时")

    def _soft_evidence() -> Optional[str]:
        """软断言（21）证据：软断言用例实际捕获的非阻断失败条数。"""
        if not _soft:
            return None
        return (f"触发条件：存在 soft_assert 用例触发失败断言；"
                f"执行：失败只记录不判用例失败（result.succeeded 仍为 True）；"
                f"产物：捕获 {_soft} 个用例的非阻断软断言失败（soft_failures）")

    rows = []
    for r in _CAPABILITY_ROWS:
        used = r["id"] in used_ids
        evidence = None
        if used:
            if r["id"] in _EVOLUTION_IDS:
                evidence = _evo_evidence(r["id"])
            elif r["id"] in _TELEMETRY_IDS:
                evidence = _tel_evidence()
            elif r["id"] == 15:
                evidence = _retry_evidence()
            elif r["id"] == 16:
                evidence = _timeout_evidence()
            elif r["id"] == 21:
                evidence = _soft_evidence()
            elif r["id"] == 23:
                evidence = _laya_evidence()
        rows.append(
            {
                "id": r["id"],
                "name": r["name"],
                "module": r["module"],
                "cmd": r["cmd"],
                "desc": r["desc"],
                "used": used,
                "evidence": evidence,
            }
        )
    return {
        "total": len(_CAPABILITY_ROWS),
        "used": len(used_ids),
        "rows": rows,
    }


def _capability_md(cap: Optional[dict[str, Any]]) -> list[str]:
    """把能力矩阵渲染成报告 md 段（无数据/空时返回 []，section 被隐藏）。

    md 无原生折叠，已用功能点的详情（desc + 本次证据）放在矩阵表之后的
    缩进列表，紧凑呈现；HTML 侧用折叠 `<details>` 默认收起。
    """
    if not cap:
        return []
    lines = ["", "## 框架能力"]
    lines += [
        "",
        f"- **本次运行已用 {cap['used']} / {cap['total']} 项**；其余为实现但本次未触发的工具。",
        "",
        "| # | 功能点 | 模块 | 子命令 | 状态 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in cap["rows"]:
        status = "✅ 已用" if r["used"] else "· 可用"
        lines += [
            f"| {r['id']} | {r['name']} | {r['module']} | "
            f"`{r['cmd']}` | {status} |"
        ]
    used_rows = [r for r in cap["rows"] if r["used"]]
    if used_rows:
        lines += ["", "### 已用功能点详情", ""]
        for r in used_rows:
            lines += [f"- **[{r['id']}] {r['name']}**：{r['desc']}"]
            if r["evidence"]:
                # 证据按「触发条件 / 执行 / 产物」分小节呈现，更易读。
                lines += [
                    f"  - {_seg.strip()}"
                    for _seg in r["evidence"].split("；")
                    if _seg.strip()
                ]
            else:
                lines += ["  - 本次运行未产生可直接量化的产物。"]
    return lines


def _project_insights(report_dir: str | Path) -> dict[str, Any]:
    """聚合项目级“新能力”数据，供报告展示（自主演进 + LLM 成本）。

    数据源均为项目本地产物，缺失/损坏时返回确定性空骨架（hermetic 友好，
    绝不因文件缺失崩溃报告链路）：
    - ``自主演进``：项目根 ``<project>/_evolution_log.jsonl`` 的每轮审计记录
      （rounds/candidates/adopted/rejected/dedup_skipped/planned_suite）。
    - ``LLM 成本``：``<report_dir>/llm_usage.jsonl`` 账本经 telemetry 聚合
      （requests/success/errors/cost/success_rate/by_model）。

    返回 ``{"evolution": {...} | None, "telemetry": {...} | None}``；
    某源不可用则对应键为 ``None``（报告按需隐藏该段）。
    """
    out_dir = Path(report_dir or settings.report_dir)
    # 演进日志在项目根（report_dir 的父目录）。
    project_root = out_dir.parent
    evolving = None
    evo_log = project_root / "_evolution_log.jsonl"
    try:
        import json as _json

        if evo_log.is_file():
            rows = [
                _json.loads(line)
                for line in evo_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if rows:
                _last = rows[-1] if rows else {}

                # 名称聚合：与上方计数字段口径一致（跨全部分组去重，非仅末轮），
                # 否则"候选 7/采纳 4"与"名称：无"自相矛盾。
                def _names(key: str) -> list[str]:
                    gathered: list[str] = []
                    for _row in rows:
                        items = _row.get(key)
                        if isinstance(items, list):
                            for _n in items:
                                name = (
                                    (_n.get("name") or _n.get("id") or str(_n))
                                    if isinstance(_n, dict)
                                    else str(_n)
                                )
                                if name and name not in gathered:
                                    gathered.append(name)
                    return gathered
                evolving = {
                    "rounds": len(rows),
                    "planned_suite": _last.get("planned_suite"),
                    # candidates/accepted/rejected 可能是 int 计数或名称 list。
                    "candidates": int(
                        sum(
                            (len(r.get("candidates"))
                             if isinstance(r.get("candidates"), list)
                             else int(r.get("candidates", 0) or 0))
                            for r in rows
                        )
                    ),
                    "accepted": int(
                        sum(
                            (len(r.get("accepted"))
                             if isinstance(r.get("accepted"), list)
                             else int(r.get("accepted", 0) or 0))
                            for r in rows
                        )
                    ),
                    "rejected": int(
                        sum(
                            (len(r.get("rejected"))
                             if isinstance(r.get("rejected"), list)
                             else int(r.get("rejected", 0) or 0))
                            for r in rows
                        )
                    ),
                    "dedup_skipped": int(
                        sum(r.get("dedup_skipped", 0) or 0 for r in rows)
                    ),
                    "stop_reason": _last.get("stop_reason"),
                    # 每轮明细供功能点详情：失败数 + 候选/采纳/拒绝名称 + 关注/全量计划。
                    "failures": int(_last.get("failures", 0) or 0),

                    "candidate_names": _names("candidates"),
                    "accepted_names": _names("accepted"),
                    "rejected_names": _names("rejected"),
                    "plan_focused": list(_last.get("plan", {}).get("focused", []))
                    if isinstance(_last.get("plan"), dict)
                    else [],
                    "plan_all": list(_last.get("plan", {}).get("all_profiles", []))
                    if isinstance(_last.get("plan"), dict)
                    else [],
                }
    except Exception:  # noqa: BLE001 - 读取/解析失败按“无演进数据”处理。
        evolving = None

    telemetry = None
    try:
        from ah.telemetry import aggregate_llm_stats, default_ledger_path, load_ledger

        _ledger = default_ledger_path(out_dir)
        if _ledger.is_file():
            _records = load_ledger(_ledger)
            _stats = aggregate_llm_stats(_records)
            telemetry = {
                "requests": int(_stats["requests"]),
                "success": int(_stats["success"]),
                "errors": int(_stats["errors"]),
                "cost": float(_stats["cost"]),
                "success_rate": float(_stats["success_rate"]),
                "by_model": _stats.get("by_model", {}),
                "prompt_tokens": int(_stats.get("prompt_tokens", 0)),
                "completion_tokens": int(_stats.get("completion_tokens", 0)),
                "total_tokens": int(_stats.get("total_tokens", 0)),
            }
            # LLM 用途分布：按 task 标签分组（调用数 + 平均/最大时延）。
            # latency_ms 由提供方返回，缺失按 0；task 缺失按 "unknown"。
            _by_task: dict[str, dict[str, float | int]] = {}
            for _rec in _records:
                _task = str(_rec.get("task", "unknown")) or "unknown"
                _lat = float(_rec.get("latency_ms") or 0.0)
                _bucket = _by_task.setdefault(
                    _task, {"requests": 0, "latency_sum_ms": 0.0, "max_latency_ms": 0.0}
                )
                _bucket["requests"] = int(_bucket["requests"]) + 1
                _bucket["latency_sum_ms"] = float(_bucket["latency_sum_ms"]) + _lat
                _bucket["max_latency_ms"] = max(float(_bucket["max_latency_ms"]), _lat)
            for _bucket in _by_task.values():
                _n = max(int(_bucket["requests"]), 1)
                _bucket["avg_latency_ms"] = round(
                    float(_bucket["latency_sum_ms"]) / _n, 1
                )
                _bucket.pop("latency_sum_ms", None)
            if _by_task:
                telemetry["by_task"] = {
                    k: {
                        "requests": int(v["requests"]),
                        "avg_latency_ms": float(v["avg_latency_ms"]),
                        "max_latency_ms": round(float(v["max_latency_ms"]), 1),
                    }
                    for k, v in _by_task.items()
                }
    except Exception:  # noqa: BLE001 - telemetry 缺失按空处理。
        telemetry = None


    return {"evolution": evolving, "telemetry": telemetry}


def build_report(
    results: list[ResultRecord],
    trace_urls: Optional[dict[str, str]] = None,
    *,
    report_dir: Optional[str] = None,
    history_rows: Optional[list[dict[str, Any]]] = None,
    analysis: Optional[str] = None,
    basename: str = "report",
    llm: Optional[Any] = None,
) -> Path:
    """在报告目录下写入 ``{basename}.md`` + ``{basename}.json``；返回 md 路径。

    ``basename`` 为输出文件名主干（不含扩展名），默认 ``report``；传不同值可
    让每次运行写不同的文件（如 ``test-tcc-m-api-20261008_153000``），避免
    覆盖。历史累积（``report_history.jsonl``）、``history_rows``、``analysis``
    的参数与语义均不变。

    ``history_rows`` 为可选的历史结果（:class:`ah.predict.history` 读回）；
    提供时报告末尾追加「预测分析」段（风险榜 + 回归必测推荐）与
    ``report.json["prediction"]``。缺省读 ``report_dir/history.jsonl``
    （不存在则该段为空，保持向后兼容）。

    ``analysis`` 为可选的运行分析 markdown（报告 NL 摘要，见
    :class:`ah.llm.agents.AgentReport.summarize_records`）；缺省用确定性
    计数版（hermetic、零 LLM）生成，写入 ``{basename}.md`` 的「运行分析」段
    与 ``{basename}.json["analysis"]``。
    """
    from ah.core.orchestrator import summarize_results

    execution_counts = summarize_results(results)
    out_dir = Path(report_dir or settings.report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 未显式提供 trace_urls 时：若真实 tracer 已启用，注入整次运行的 Langfuse
    # 深度链接（指向 get_tracer().run_trace_id），让报告一键直达该 run 的 trace。
    if not trace_urls:
        run_link = _run_trace_link()
        if run_link:
            trace_urls = {"langfuse": run_link}

    total = len(results)
    passed = sum(1 for r in results if r.result == "PASS")
    failed = sum(1 for record in results if record.result in {"FAIL", "ERROR"})

    from ah.llm.agents import AgentReport

    reporter = AgentReport(llm)  # llm 为 None（LLMOFF/未配置）→ 确定性计数
    if analysis is None:
        analysis = reporter.summarize_records(results)

    if history_rows is None:
        from ah.predict.history import HistoryStore

        history_rows = HistoryStore(out_dir / "history.jsonl").load()
    prediction: dict[str, Any] = {}
    plan = None
    if history_rows:
        from ah.predict.risk import rank_regression

        plan = rank_regression(history_rows)
        prediction = plan.to_dict()

    lines = ["# Test Report", ""]
    lines.append(f"- **Run time**: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- **Environment**: base_url={settings.base_url}")
    lines.append(f"- **Summary**: {passed}/{total} passed, {failed} failed")
    lines.append(
        f"- **执行口径**: planned={execution_counts['planned']}, "
        f"executed={execution_counts['executed']}, blocked={execution_counts['blocked']}, "
        f"skipped={execution_counts['skipped']}, unverified={execution_counts['unverified']}; "
        f"实际评估断言={execution_counts['assertions']}（非计划断言数）"
    )
    # —— 断言密度统计（后周期支柱 A）：按每 case 实际评估断言聚合，
    #    命中《诊治体系》"断言密度进报告"的短板诉求。
    _ad_counts = [len(r.assertions or []) for r in results]
    _ad_avg = round(sum(_ad_counts) / len(_ad_counts), 2) if _ad_counts else 0.0
    _ad_no_assert = [r.name for r in results if not (r.assertions or [])]
    _ad_hi = _ad_med = _ad_none = 0
    for _n in _ad_counts:
        if _n >= 3:
            _ad_hi += 1
        elif _n >= 1:
            _ad_med += 1
        else:
            _ad_none += 1
    lines.append(
        f"- **断言密度**: 平均 {_ad_avg} 断言/用例 · 高(≥3) {_ad_hi} 例"
        f" · 中(1-2) {_ad_med} 例 · 无(0) {_ad_none} 例"
    )
    if _ad_no_assert:
        lines.append(
            f"- **无有效断言用例**（{len(_ad_no_assert)} 例）: "
            f"{', '.join(_ad_no_assert)}"
        )
    # 框架能力本运行使用概况：按运行记录可靠判定的 3 项（重试/软断言/显式超时）。
    _cap_retry = sum(1 for r in results if (r.llm_notes or {}).get("retried"))
    _cap_soft = sum(
        1 for r in results
        if int(
            (r.llm_notes or {}).get("soft_failures")
            or getattr(r, "soft_failures", 0)
            or 0
        ) > 0
    )
    _cap_timeout = sum(1 for r in results if getattr(r, "timeout", None) is not None)
    lines.append(
        f"- **框架能力**: 重试 {_cap_retry} 例 · 软断言 {_cap_soft} 例"
        f" · 显式超时 {_cap_timeout} 例"
        "（并行/多环境/占位符在 #框架能力 矩阵，见下）"
    )
    if trace_urls:
        lines.append(f"- **Langfuse trace urls**: {json.dumps(trace_urls, indent=None)}")
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append("| Case | Result | Duration (s) | Failure | Self-heal / 根因 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for r in results:
        dur = f"{r.duration:.3f}"
        fail = (r.failure_msg or "").replace("|", "\\|").replace("\n", " ")[:120]
        heal = _heal_summary(r).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r.name} | {r.result} | {dur} | {fail} | {heal} |")
    prior_runs = _load_run_history(out_dir / "report_history.jsonl")
    _mark_flaky_from_history(results, prior_runs)  # 跨 run 抖动 → flaky 打标

    # 接口健康画像：状态码分布 + flaky + 耗时分布（avg/p95/p99 + ASCII 直方图）。
    _status_dist: dict[int, int] = {}
    _flaky_n = 0
    for _r in results:
        _st = (_r.response or {}).get("status_code")
        if isinstance(_st, int):
            _status_dist[_st] = _status_dist.get(_st, 0) + 1
        if _r.flaky:
            _flaky_n += 1
    _durs = [r.duration for r in results if r.duration]
    lines += ["", "## 接口健康"]
    lines += ["", f"- **用例数**: {total}，**flaky（非首过即绿）**: {_flaky_n}"]
    if _status_dist:
        _rows = ", ".join(f"{k}:{v}" for k, v in sorted(_status_dist.items()))
        lines += [f"- **HTTP 状态分布**: {_rows}"]
    if _durs:
        _avg = sum(_durs) / len(_durs) * 1000
        _p95d = (_percentile(_durs, 0.95) or 0.0) * 1000
        _p99d = (_percentile(_durs, 0.99) or 0.0) * 1000
        _maxd = max(_durs) * 1000
        lines += [
            f"- **耗时分布**: 平均 {_avg:.1f} ms · P95 {_p95d:.1f} ms · "
            f"P99 {_p99d:.1f} ms · 最大 {_maxd:.0f} ms",
        ]
        lines += ["", "| 耗时区间 | 用例数 | 分布 |", "| --- | --- | --- |"]
        _buckets = [(0, 5), (5, 20), (20, 100), (100, 500), (500, float("inf"))]
        _labels = ["<5ms", "5-20ms", "20-100ms", "100-500ms", ">500ms"]
        _counts = [0 for _ in _buckets]
        for _d in _durs:
            _ms = _d * 1000
            for _i, (_lo, _hi) in enumerate(_buckets):
                if _ms >= _lo and _ms < _hi:
                    _counts[_i] += 1
                    break
        _max_c = max(_counts) or 1
        for _lab, _c in zip(_labels, _counts):
            _bar = "█" * round((_c / _max_c) * 20)
            lines += [f"| {_lab} | {_c} | {_bar} |"]
    lines.append("")
    cur_cases = {r.name: r.result for r in results}

    key_interfaces = _key_interfaces(prior_runs, cur_cases)
    if (prediction and plan is not None) or key_interfaces:
        # 预测分析走 LLM：风险模型 + 本次结果 → 专业级下一步建议；关闭 LLM 时确定性回退
        llm_advice = reporter.predict_advice(prediction, key_interfaces, results)
        lines.extend(_prediction_section(plan, key_interfaces, llm_advice=llm_advice))
        if prediction:
            prediction["advice"] = llm_advice
    # 新能力洞察：自主演进 + LLM 成本（缺失时为空，hermetic 不报错）。
    insights = _project_insights(out_dir)
    evolving = insights.get("evolution")
    telemetry = insights.get("telemetry")
    if evolving:
        lines += ["", "## 自主演进"]
        lines += [
            "",
            f"- **轮次**: {evolving['rounds']}",
            f"- **规划用例集**: {evolving['planned_suite'] or '—'}",
            f"- **候选 / 采纳 / 拒绝 / 去重跳过**: "
            f"{evolving['candidates']}/{evolving['accepted']}/"
            f"{evolving['rejected']}/{evolving['dedup_skipped']}",
            f"- **停止原因**: {evolving['stop_reason'] or '—'}",
        ]
        # 演进候选质量：采纳率/去重率/质量提示（候选为 0 时如实标注，不虚报）。
        _cand = int(evolving["candidates"])
        _acc = int(evolving["accepted"])
        _dedup = int(evolving["dedup_skipped"])
        _adopt = (_acc / _cand * 100) if _cand else 0.0
        _dedup_rate = (_dedup / _cand * 100) if _cand else 0.0
        _qual_note = ""
        if _cand == 0:
            _qual_note = "本轮无候选产出（历史失败基线为空或全绿），演进未进入质量门控。"
        elif _cand == _acc:
            _qual_note = "候选全部采纳，质量门控未拦截。" if _cand else _qual_note
        elif _acc == 0:
            _qual_note = "候选全部被质量门控拒绝，需复查历史失败基线。"
        lines += [
            f"- **采纳率**: {_adopt:.0f}%（{_acc}/{_cand}）",
            f"- **去重率**: {_dedup_rate:.0f}%（{_dedup} 去重跳过）",
        ]
        if _qual_note:
            lines += [f"- **质量提示**: {_qual_note}"]
    if telemetry and telemetry["requests"]:
        _rate = f"{telemetry['success_rate'] * 100:.1f}%"
        lines += ["", "## LLM 成本"]
        lines += [
            "",
            f"- **调用数**: {telemetry['requests']}（成功 {telemetry['success']} / "
            f"失败 {telemetry['errors']}）",
            f"- **Token**: {telemetry.get('total_tokens', 0)}（输入 "
            f"{telemetry.get('prompt_tokens', 0)} / 输出 "
            f"{telemetry.get('completion_tokens', 0)}）",
            f"- **总成本**: ${telemetry['cost']:.4f}",
            f"- **成功率**: {_rate}",
        ]
        if telemetry["by_model"]:
            lines += [
                "",
                "| 模型 | 调用 | Token | 成本 | 成功 | 失败 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for model, b in telemetry["by_model"].items():
                lines += [
                    f"| {model} | {b['requests']} | "
                    f"{b.get('total_tokens', 0)} | "
                    f"${float(b['cost']):.4f} | "
                    f"{b['success']} | {b['errors']} |"
                ]
        if telemetry.get("by_task"):
            lines += [
                "",
                "### LLM 用途分布 · 时延",
                "",
                "| 用途(task) | 调用 | 平均时延(ms) | 最大时延(ms) |",
                "| --- | --- | --- | --- |",
            ]
            for _task, _b in sorted(telemetry["by_task"].items()):
                lines += [
                    f"| {_task} | {_b['requests']} | "
                    f"{_b.get('avg_latency_ms', 0.0):.1f} | "
                    f"{_b.get('max_latency_ms', 0.0):.1f} |"
                ]
    capabilities = _capability_matrix(evolving, telemetry, results=results)
    lines += _capability_md(capabilities)

    if analysis:
        lines += ["", analysis]
    md_text = "\n".join(lines) + "\n"
    md_path = out_dir / f"{basename}.md"
    md_path.write_text(md_text, encoding="utf-8")

    report_json = {
        "run_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "environment": {"base_url": settings.base_url},
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            # 断言密度（后周期支柱 A）：平均断言数 + 分布桶 + 无有效断言用例。
            "assert_density": {
                "avg_per_case": _ad_avg,
                "total_asserts": int(sum(_ad_counts)),
                "high": _ad_hi,
                "medium": _ad_med,
                "none": _ad_none,
                "no_assert_cases": _ad_no_assert,
            },
        },
        "execution_counts": execution_counts,
        "trace_urls": trace_urls or {},
        "results": [asdict(r) for r in results],
        "semantic_verdicts": _semantic_verdicts_agg(results),
    }
    _ih_durs = [_d * 1000 for _d in _durs]
    report_json["interface_health"] = {
        "total": total,
        "flaky": _flaky_n,
        "status_dist": {str(k): v for k, v in sorted(_status_dist.items())},
        "duration_ms": {
            "avg": round(sum(_ih_durs) / len(_ih_durs), 1) if _ih_durs else 0.0,
            "p95": round((_percentile(_durs, 0.95) or 0.0) * 1000, 1) if _durs else 0.0,
            "p99": round((_percentile(_durs, 0.99) or 0.0) * 1000, 1) if _durs else 0.0,
            "max": round(max(_ih_durs), 1) if _ih_durs else 0.0,
        },
    }

    if evolving:
        report_json["evolution"] = evolving
    if capabilities:
        report_json["capabilities"] = capabilities
    if telemetry and telemetry["requests"]:
        report_json["telemetry"] = telemetry
    if prediction:
        report_json["prediction"] = prediction
    if analysis:
        report_json["analysis"] = analysis
    json_path = out_dir / f"{basename}.json"
    json_path.write_text(json.dumps(report_json, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("report written: %s (+report.json)", md_path)
    return md_path

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>API Test Harness · 运行报告</title>
<style>
:root{--canvas:#fdf6e6;--surface:#faf4dd;--subtle:#f7ecd3;--strong:#f0e6c9;
  --wash:#faf4dd;
  --forest:#3c3836;--emerald:#076678;--jade:#076678;
  --mint:#f7ecd3;--mint-soft:#f7ecd3;--mint-wash:#fdf6e6;--green-line:#d5c4a1;
  --ink:#3c3836;--muted:#665c54;--line:#d5c4a1;
  --green:#427b58;--amber:#af3a03;--red:#9d0006;--blue:#076678;--purple:#8f3f71;
  --shadow:0 3px 12px rgb(60 56 54/.06);
  --sans:"Avenir Next","PingFang SC","Hiragino Sans GB",
    "Microsoft YaHei",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --serif:"Avenir Next","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--canvas);
  color:var(--ink);font:400 16px/1.7 var(--sans);
  -webkit-font-smoothing:antialiased;text-wrap:pretty}
main{width:min(100% - 32px,1600px);margin:auto;padding:22px 0 40px}
h1,h2,h3,h4,p{margin-top:0}
h1{font-family:var(--sans);font-weight:800;font-size:28px;line-height:1.3;margin:0}
h2{font-family:var(--sans);font-weight:750;font-size:22px;line-height:1.35;margin-bottom:16px;color:var(--forest)}
​.content h2{font-size:20px}
​.content details.panel>summary h2{font-size:19px;margin:0}
h3{font-family:var(--sans);font-weight:700;font-size:17px;margin-bottom:10px}
h4{font-family:var(--sans);font-weight:700;font-size:13px;color:var(--muted);letter-spacing:.04em;margin-bottom:7px}
code,pre,.metric b,.case-id,.pipe code{font-family:var(--mono)}
code{font-size:.92em;background:var(--wash);border:1px solid var(--line);
  border-radius:4px;padding:1px 5px;color:var(--emerald)}
.top{background:var(--surface);border:1px solid var(--line);border-radius:8px;
  color:var(--ink);box-shadow:var(--shadow);display:flex;justify-content:space-between;
  gap:24px;align-items:flex-start;margin-bottom:18px;padding:20px 22px}
​.top .eyebrow{color:var(--muted);font:700 11px/1 var(--mono);letter-spacing:.14em;
  text-transform:uppercase;margin:0 0 8px}

.top .sub{color:var(--ink);margin:6px 0 0}
.top .tagline{color:var(--muted);margin:10px 0 0;font-size:12.5px;max-width:72ch}
.badge-slot{display:flex;align-items:flex-start;gap:10px;flex-direction:column}
.badge{display:inline-flex;align-items:center;padding:4px 8px;border:1px solid var(--line);
  border-radius:999px;background:var(--wash);font:600 11px/1.3 var(--mono);white-space:nowrap}
.badge.pass,.badge.ok{color:var(--green);background:#e9efe2;border-color:#cfe0cd}
.badge.fail{color:var(--red);background:#fbe6e4;border-color:#ecc9c0}
.badge.skip{color:var(--amber);background:#fbf0d9;border-color:#e8d3a9}
.badge.neutral{color:var(--muted);background:#f2e5bc;border-color:#d5c4a1}
.badge.dot::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;
  background:currentColor;margin-right:6px;opacity:.9}
.top .badge.pass,.top .badge.ok,.top .badge.fail,.top .badge.neutral{color:var(--ink);
  background:var(--wash);border-color:var(--line)}
.download-group{display:flex;gap:7px;flex-wrap:wrap}
button.btn{padding:8px 11px;border:1px solid var(--line);border-radius:6px;
  background:var(--surface);color:var(--ink);font:600 12px/1 var(--sans);cursor:pointer}
button.btn:hover,button.btn:focus-visible{border-color:var(--accent,#076678);
  color:var(--accent,#076678);outline:none}
.top button.btn{color:var(--ink);background:var(--wash);border-color:var(--line)}
.top button.btn.primary{color:#fff;background:var(--emerald);border-color:var(--emerald)}
.report-shell{display:grid;grid-template-columns:300px minmax(0,1fr);gap:18px;align-items:start}
.sidebar{position:sticky;top:18px;display:grid;gap:12px;min-width:0}
.side-nav{display:grid;gap:3px;background:var(--wash)}
.side-nav .nav-label{font:700 10px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);padding:10px 10px 4px}
.side-nav a{display:flex;justify-content:space-between;padding:8px 10px;border-radius:6px;
  color:var(--ink);border-left:3px solid transparent;text-decoration:none;
  font-size:13px;transition:background .18s ease,border-color .18s ease,transform .18s ease}
.side-nav a b{color:var(--emerald);font-weight:600}
.side-nav a:hover,.side-nav a:focus-visible{border-left-color:var(--emerald);
  background:var(--mint);transform:translateX(2px);outline:none}
​.content{display:grid;gap:16px;min-width:0}
.tabs{position:sticky;top:12px;z-index:8;display:flex;flex-wrap:wrap;gap:9px;
  background:var(--surface);border:1px solid var(--line);border-radius:8px;
  padding:10px 11px;box-shadow:0 8px 22px rgb(41 37 24 / .09)}
.tabs a{padding:9px 17px;border-radius:7px;font-size:13px;font-weight:600;
  color:var(--ink);background:var(--wash);
  border:1px solid var(--line);text-decoration:none;white-space:nowrap;
  transition:background .18s ease,color .18s ease,border-color .18s ease}
.tabs a:hover,.tabs a:focus-visible{background:var(--mint);border-color:var(--emerald);
  color:var(--forest);outline:none}
details.panel>summary{list-style:none;cursor:pointer;display:flex;align-items:center;
  gap:6px;flex-wrap:wrap}
details.panel>summary::-webkit-details-marker{display:none}
details.panel>summary h2{margin:0;display:inline-flex;align-items:center;gap:9px}
details.panel:not([open])>summary h2::after{content:"▸";font-size:11px;color:var(--emerald)}
details.panel[open]>summary h2::after{content:"▾";font-size:11px;color:var(--muted)}
​details.panel>summary{list-style:none;cursor:pointer;display:flex;align-items:center;
​  gap:8px}
​details.panel>summary::-webkit-details-marker{display:none}
​details.panel>summary h2{margin:0}
​details.panel:not([open])>summary h2::after{content:"▸ 展开";font:600 11px/1 var(--mono);
​  color:var(--muted);margin-left:auto}
​details.panel[open]>summary h2::after{content:"▾ 收起";font:600 11px/1 var(--mono);
​  color:var(--muted);margin-left:auto}
.panel{background:var(--surface);
  border:1px solid var(--line);border-radius:8px;padding:18px;
  box-shadow:var(--shadow);min-width:0}
.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.metric{padding:10px;border:1px solid var(--line);border-radius:6px;
  background:var(--wash);min-width:0}
.metric small{display:block;color:var(--muted);font-size:11px}
.metric b{display:block;margin-top:3px;overflow-wrap:anywhere;color:var(--forest)}
.metric.wide{grid-column:1/-1}
.metric .hint{color:var(--muted);font-size:11px;margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--surface)}
th,td{text-align:left;vertical-align:top;padding:8px 10px;border-bottom:1px solid var(--line);
  overflow-wrap:anywhere}
thead th{position:sticky;top:0;background:var(--wash);
  color:var(--forest);font-size:11px;z-index:1}
tbody tr:last-child th,tbody tr:last-child td{border-bottom:0}
.result-table-wrap,.scroll{overflow:auto;border:1px solid var(--line);border-radius:6px}
.result-table-wrap{border-color:var(--line);box-shadow:0 5px 16px rgb(41 37 24 / .06)}
table.kv th{width:150px;background:var(--wash);
  color:var(--muted);font-size:11px}
table.kv td{background:var(--surface)}
.case-list{display:grid;gap:12px}
.case-card{border:1px solid var(--line);border-radius:7px;overflow:hidden;background:var(--surface);
  box-shadow:0 5px 16px rgb(41 37 24 / .06)}
.case-card>header{display:flex;justify-content:space-between;gap:12px;align-items:center;
  padding:12px 14px;background:var(--wash);
  border-bottom:1px solid var(--line)}
.case-card h3{display:inline;margin:0 0 0 9px;color:var(--forest)}
.case-id{color:var(--emerald);font-size:12px}
.case-head-stats{display:flex;gap:14px;align-items:center;color:var(--muted);font-size:12px}
.case-columns{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0}
.case-columns section{padding:12px 14px;border-right:1px solid var(--line)}
.case-columns section:last-child{border-right:0}
.case-columns section:first-child{grid-column:1/-1;border-bottom:1px dashed var(--line)}
.case-columns section:not(:first-child){grid-column:span 1}
.mrow{font-size:12px;margin:0 0 6px;overflow-wrap:anywhere}
.mrow .lbl{display:inline-block;width:52px;color:var(--muted);font-weight:600;font-size:11px}
.mrow .lbl.ok{color:var(--green)}
.mrow code{word-break:break-all}
.mrow a.trace-link{color:var(--blue);font-family:var(--mono);font-size:11px;
  text-decoration:underline;word-break:break-all}
.mrow a.trace-link:hover{text-decoration:none;color:#065f70}
details.details-section{background:var(--surface);border:1px solid var(--line);border-radius:8px;
  overflow:hidden;box-shadow:0 10px 28px rgb(41 37 24 / .1)}
pre.dump{font-family:var(--mono);font-size:11px;white-space:pre-wrap;word-break:break-all;
  background:#17212b;color:#e7edf2;border-radius:6px;padding:12px;margin:4px 0 0;
  max-height:260px;overflow:auto}
details.details-section>summary{display:flex;flex-wrap:wrap;align-items:center;
  gap:8px 12px;padding:13px 16px;cursor:pointer;list-style:none;
  background:var(--wash)}
details.details-section>summary::-webkit-details-marker{display:none}
details.details-section>summary h2{margin:0;display:inline-flex;align-items:center;
  flex-wrap:wrap;gap:8px;font-size:15px}
details.details-section>summary .fold{color:var(--muted);font-size:12px;
  font-weight:600;margin-left:2px}
.case-n{color:var(--muted);font:600 11px/1 var(--mono)}
.fail-msg{color:var(--red);font-size:11px;font-family:var(--mono);
  background:#fbeae8;border:1px solid #f0cdc7;border-radius:5px;
  padding:2px 7px;overflow-wrap:anywhere}
details.details-section .inner{padding:0 16px 16px;
  background:var(--wash)}
details.details-section .inner>.panel{margin-top:12px;box-shadow:none}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.query-list{margin:0;padding-left:20px}.query-list li{margin:5px 0}
.place-text{color:var(--muted);font-size:12px}
.foot{margin-top:20px;color:var(--muted);font-size:12px}
.answhead h2::before{content:"";display:inline-block;width:7px;height:20px;margin:0 9px -3px 0;
  border-radius:999px;background:var(--emerald);
  box-shadow:0 0 0 4px var(--mint)}
/* ── 框架能力 / Loop 工程 / 分析 ── */
.cap-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.cap{display:flex;gap:11px;align-items:flex-start;padding:11px;border:1px solid var(--line);
  border-radius:7px;background:var(--surface);
  min-width:0;overflow-wrap:anywhere;word-break:break-word}
.cap.ic-forest .icn{background:linear-gradient(135deg,#3c3836,#665c54)}
.cap.ic-jade .icn{background:linear-gradient(135deg,#076678,#83a598)}
.cap.ic-slate .icn{background:linear-gradient(135deg,#665c54,#928374)}
.cap.ic-blue .icn{background:linear-gradient(135deg,#076678,#83a598)}
.cap.ic-amber .icn{background:linear-gradient(135deg,#af3a03,#fe8019)}
.cap.ic-purple .icn{background:linear-gradient(135deg,#8f3f71,#d3869b)}
.cap.ic-red .icn{background:linear-gradient(135deg,#9d0006,#cc241d)}
.cap .icn{flex:0 0 34px;width:34px;height:34px;display:grid;place-items:center;border-radius:8px;
  font:700 14px/1 var(--mono);color:#fff}
.cap h3{font-size:13px;margin:1px 0 3px;color:var(--forest)}
.cap p{margin:0;font-size:12px;color:var(--muted);min-width:0;
  overflow-wrap:anywhere;word-break:break-word}
.cap code{font-size:11px;word-break:break-all}
.pair-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));
  gap:10px;margin-top:12px}
.pair-grid .cap{display:block}
.extra-tbl{width:100%;border-collapse:collapse;font-size:11px;margin-top:6px}
.extra-tbl th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--muted);border-bottom:1px solid var(--line)}
.extra-tbl td{border-bottom:1px solid var(--line);padding:4px 6px 4px 0;color:var(--ink);
  vertical-align:top}
.extra-tbl td:first-child{width:74px;white-space:nowrap}
.extra-tbl td:nth-child(2){width:118px;white-space:nowrap}
.loop-pay{margin-top:14px;padding:12px 14px;border:1px solid #e8d3a9;border-radius:8px;
  background:var(--wash)}
.loop-pay h3{font-size:13px;margin:0 0 6px;color:var(--amber)}
.loop-pay .place-text{font-size:12px}
.pay-list{margin:8px 0 0;padding-left:18px;font-size:12px;color:#5f4a1f}
.pay-list li{margin:5px 0}
.pay-list b{color:var(--amber)}
.pay-list code{font-size:11px}
.pipe{display:flex;flex-wrap:wrap;gap:7px;align-items:center}
.pipe .arrow{color:var(--emerald);font-weight:700;opacity:.75}
.stage{display:inline-flex;flex-direction:column;gap:2px;flex:1 1 auto;min-width:118px;
  padding:8px 10px;border:1px solid var(--line);border-radius:6px;
  background:var(--surface);font-size:12px}
.stage small{font:600 10px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted)}
.stage b{color:var(--forest);font-size:12px}
.stage .sub{color:var(--muted);font-size:11px}
.stage.alt{background:var(--mint-soft);
  border-color:#e8d3a9}
.stage.alt small{color:var(--amber)}.stage.alt b{color:var(--amber)}
.loop-row{display:flex;flex-wrap:wrap;gap:14px;margin-top:14px}
.loop-col{flex:1 1 300px;min-width:0}
.failbars{display:grid;gap:8px}
.failbar{display:grid;grid-template-columns:170px minmax(0,1fr) 46px;gap:10px;align-items:center;
  font-size:12px}
.failbar .track{height:12px;border-radius:999px;background:#ece2d3;overflow:hidden}
.failbar .track i{display:block;height:100%;border-radius:999px;
  background:linear-gradient(90deg,#d8998f,#b2453a)}
.failbar .n{color:var(--muted);font:600 11px/1 var(--mono);text-align:right}
.agent-tbl code{font-size:11px}
.chips{display:flex;gap:7px;flex-wrap:wrap}
.chips .chip{padding:4px 9px;border-radius:999px;font:600 11px/1.3 var(--mono);
  border:1px solid var(--line);background:var(--wash);color:var(--emerald)}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:8px}
.legend .lg{display:inline-flex;align-items:center;gap:6px}
.legend .sw{width:11px;height:11px;border-radius:3px;display:inline-block}
.hint-bar{border-left:3px solid var(--emerald);background:var(--mint);border-radius:0 6px 6px 0;
  padding:9px 12px;font-size:12px;color:var(--ink);margin-top:10px}
.hint-bar.warn{border-left-color:var(--amber);background:#fbf0d9}
.env-table td code{font-size:11px}
.badge.flaky{color:var(--amber);background:#fbf0d9;border-color:#e8d3a9}
.badge.review{color:var(--purple);background:#f3ecf3;border-color:#d9c6d9}
.analysis-md,.llm-advice{margin:8px 0 0;padding:14px 16px;background:var(--wash);
  border:1px solid var(--line);border-radius:8px;font:12.5px/1.65 var(--sans);
  color:var(--ink);overflow-wrap:anywhere}
.analysis-md h3,.llm-advice h3{margin:4px 0 8px;font-size:14px;color:var(--forest);
  border-bottom:1px solid var(--line);padding-bottom:4px}
.analysis-md h4,.llm-advice h4{margin:10px 0 4px;font-size:12.5px;color:var(--emerald)}
.analysis-md h5,.llm-advice h5{margin:8px 0 3px;font-size:12px;color:var(--muted)}
.analysis-md p,.llm-advice p{margin:5px 0}
.analysis-md ul,.llm-advice ul{margin:5px 0;padding-left:20px}
.analysis-md li,.llm-advice li{margin:3px 0}
.analysis-md li ul,.llm-advice li ul{margin:2px 0 2px 8px}
.analysis-md strong,.llm-advice strong{color:var(--forest)}
.analysis-md code,.llm-advice code{font:11px/1.5 var(--mono);background:var(--wash);
  border:1px solid var(--line);border-radius:4px;padding:1px 4px;color:var(--emerald);
  word-break:break-all}
.copy-btn{position:absolute;top:8px;right:8px;font:600 11px/1 var(--mono);
  color:var(--muted);background:rgb(41 37 24 / .06);border:1px solid rgb(41 37 24 / .12);
  border-radius:6px;padding:4px 8px;cursor:pointer}
.copy-btn:hover{color:var(--ink);border-color:var(--line);background:var(--wash)}
.trunc-note{font-size:11px;color:var(--amber);margin:3px 0 0}
.cmp{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px}
.cmp .side{border:1px solid var(--line);border-radius:7px;padding:8px 10px;font-size:12px;
  background:var(--surface);overflow-wrap:anywhere}
.cmp .side h4{margin:0 0 4px;font-size:11px;color:var(--muted);letter-spacing:.04em}
.cmp .side.actual{background:#fbe6e4;border-color:#ecc9c0}
.cmp .side.actual h4{color:var(--red)}
.cmp .side code{word-break:break-all;white-space:pre-wrap}
.assert-table{width:100%;border-collapse:collapse;font-size:12px;margin-top:10px}
.assert-table th,.assert-table td{border:1px solid var(--line);padding:6px 9px;text-align:left;
  vertical-align:top;overflow-wrap:anywhere}
.assert-table th{background:var(--wash);color:var(--forest)}
.assert-table td code{font-size:11px;word-break:break-all;white-space:pre-wrap}
.assert-table tr.bad{background:#fbe6e4}
.assert-table tr.bad td{color:#9d0006}
.fail-table{width:100%;border-collapse:collapse;font-size:12px;margin-top:10px}
.fail-table th,.fail-table td{border:1px solid var(--line);padding:7px 9px;text-align:left;
  vertical-align:top}
.fail-table th{background:var(--wash);color:var(--forest)}
.fail-table a{color:var(--emerald);text-decoration:none;font-weight:600}
.fail-table a:hover{text-decoration:underline}
.fail-table .cat{font-family:var(--mono);font-size:11px;color:var(--amber);
  background:#fbf0d9;border-radius:5px;padding:1px 7px;display:inline-block}
.metric .sd{display:block;font-size:10px;color:var(--amber);font-weight:600;margin-top:2px}
.fail-table .cat.warn{background:#fbe6e4;color:var(--red)}
.fail-table td.slow-dur{font-family:var(--mono);font-size:11px;color:var(--emerald);
  white-space:nowrap}
.metric-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:10px;margin:6px 0 10px}
.metric{min-width:0;border:1px solid var(--line);border-radius:6px;padding:8px 10px;
  background:var(--wash);overflow-wrap:anywhere;
  word-break:break-word}
.metric.warn{border-color:#ecc9c0;background:#fbefec}
.metric .ml{display:block;font-size:11px;color:var(--muted);font-weight:600}
.metric .mc{display:block;font:700 18px/1.2 var(--mono);color:var(--forest)}
.metric.warn .mc{color:var(--red)}
.metric .mv{display:block;font-size:11px;color:var(--muted);margin-top:2px;
  overflow-wrap:anywhere;word-break:break-word}
.trend{display:grid;gap:5px;margin-top:8px}
.trend .t{display:grid;grid-template-columns:44px minmax(0,1fr) 42px;gap:8px;
  align-items:center;font:600 11px/1 var(--mono);color:var(--muted)}
.trend .tm{font-size:10px}
.trend .tr-track{height:9px;border-radius:999px;background:#ece2d3;overflow:hidden}
.trend .tr-track i{display:block;height:100%;border-radius:999px;
  background:linear-gradient(90deg,var(--emerald),#3d7a89)}
.trend .tr-track i.low{background:linear-gradient(90deg,var(--amber),var(--red))}
.trend .pr{text-align:right}
.trend-svg{display:block;width:100%;height:auto;max-width:640px;margin-top:8px}
.trend-svg .tp{stroke:var(--blue);stroke-width:2;stroke-linejoin:round;
  stroke-linecap:round}
.trend-svg .tg{stroke:#e3dfd0;stroke-width:1;stroke-dasharray:3 3}
.trend-svg .tv{font:500 10px/1 var(--mono);fill:var(--muted)}
.trend-svg .tt{font:600 10px/1 var(--mono);fill:#8a8f85}
.trend-svg .trend-dot{cursor:pointer}
.trend-svg .trend-dot:hover{fill:var(--blue)!important;r:5.5px}
.trend-tip{position:fixed;z-index:99;pointer-events:none;max-width:min(300px,80vw);
  padding:5px 9px;border-radius:6px;font:11px/1.4 var(--mono);color:#fff;
  background:color-mix(in srgb,var(--fg,#1a2b26) 88%,transparent);
  box-shadow:0 2px 8px rgb(0 0 0 / .25);white-space:nowrap}
.trend-tip[hidden]{display:none}
@media(max-width:1050px){.report-shell{grid-template-columns:240px minmax(0,1fr)}
  .case-columns{grid-template-columns:1fr}
  .case-columns section{border-right:0;border-bottom:1px solid var(--line)}
  .case-columns section:first-child{border-bottom:1px dashed var(--line)}
  .failbar{grid-template-columns:130px minmax(0,1fr) 40px}}
@media(max-width:760px){main{width:min(100% - 20px,1600px);padding-top:12px}
  .top{display:block}.badge-slot{margin-top:12px}.download-group{margin-top:9px}
  .report-shell{display:block}.sidebar{position:static;margin-bottom:14px}
  .content{gap:12px}.panel{padding:14px}.grid{grid-template-columns:1fr}
  .metrics{grid-template-columns:repeat(2,minmax(0,1fr))}
  .case-columns section:first-child{grid-column:1/-1}
  .failbar{grid-template-columns:110px minmax(0,1fr) 36px}}
/* ── 语义分域配色：醒目但有层次（gruvbox-light 奶油底上按面板上色）── */
/* 各面板标题点色 + 左侧 accent 条 */
#summary>summary h2{color:var(--green)}
#summary>summary h2::before{background:var(--green);box-shadow:0 0 0 4px #e9efe2}
#capabilities>summary h2{color:var(--blue)}
#capabilities>summary h2::before{background:var(--blue);box-shadow:0 0 0 4px #e2ecf2}
#interface-health>summary h2{color:var(--green)}
#interface-health>summary h2::before{background:var(--green);box-shadow:0 0 0 4px #e9efe2}
#llm-usage>summary h2{color:var(--purple)}
#llm-usage>summary h2::before{background:var(--purple);box-shadow:0 0 0 4px #f0e9f0}
#detail>summary h2{color:var(--green)}
#detail>summary h2::before{background:var(--green);box-shadow:0 0 0 4px #e9efe2}
#failures>summary h2{color:var(--red)}
#failures>summary h2::before{background:var(--red);box-shadow:0 0 0 4px #fbe6e4}
#harness>summary h2{color:var(--amber)}
#harness>summary h2::before{background:var(--amber);box-shadow:0 0 0 4px #fbf0d9}
/* 回归对比 / 通过率趋势（非 details 面板）标题点色 */
.panel h3{color:var(--green)}
/* metric 值按语义区着色（保持 warn 语义优先） */
#summary .metric .mc{color:var(--green)}
#summary .metric.warn .mc{color:var(--red)}
#interface-health .metric .mc{color:var(--green)}
#interface-health .metric.warn .mc{color:var(--red)}
.metric-grid .metric.warn .mc{color:var(--amber)}
/* 面板内表格表头底：按面板分色 tint */
#capabilities table thead th{background:#e2ecf2;color:#0f5c6e}
#interface-health table th{background:#e9efe2;color:#3c5d50}
#llm-usage .fail-table th{background:#f0e9f0;color:#6e4f73}
#failures .fail-table th{background:#fbe6e4;color:var(--red)}
/* 用例结果表头 tint（detail 面板） */
#detail table thead th{background:#e9efe2;color:#5a6411}
/* 全局表格斑马纹（even 行淡 tint，cream 暖调） */
table:not(.kv) tbody tr:nth-child(even){background:#f5ecd7}
table.kv tbody tr:nth-child(even) td{background:#f5ecd7}
/* 能力矩阵行 hover 凸显 */
.cap-grid .cap{transition:border-color .15s ease,box-shadow .15s ease}
.cap-grid .cap:hover{border-color:#c9c4b4;box-shadow:0 3px 10px rgb(41 37 24/.08)}
/* Harness / 能力矩阵卡：彩色左边条 + 淡 tint 底 */
.cap.ic-forest{border-left:3px solid #3c3836}
.cap.ic-jade{border-left:3px solid #076678}
.cap.ic-slate{border-left:3px solid #665c54}
.cap.ic-blue{border-left:3px solid #076678}
.cap.ic-amber{border-left:3px solid #af3a03}
.cap.ic-purple{border-left:3px solid #8f3f71}
.cap.ic-red{border-left:3px solid #9d0006}
.cap.ic-forest{background:#ede9da}
.cap.ic-jade{background:#e2ecf2}
.cap.ic-slate{background:#ece7d8}
.cap.ic-blue{background:#e2ecf2}
.cap.ic-amber{background:#fbf0d9}
.cap.ic-purple{background:#f0e9f0}
.cap.ic-red{background:#fbe6e4}
/* 用例结果卡(实际为 details.details-section)：左侧 accent 条 */
#detail .details-section{border-left:4px solid #cfe0cd}
/* 状态点色 stay 低饱和（badge/tag） */
.badge.pass,.badge.ok{background:#e9efe2;color:var(--green)}
.badge.fail{background:#fbe6e4;color:var(--red)}
.badge.flaky{background:#fbf0d9;color:var(--amber)}
/* ── 区块背景分色：按阅读区块上淡彩底，奶油感上可区隔 ── */
/* 1) API 测试运行报告 header：奶油绿渐变 + 顶部 accent 条 */
.top{background:linear-gradient(160deg,#d7e0d0 0%,#e4e8d8 50%,#efe9d0 100%);
  border:1px solid #c8d2bf;border-top:3px solid var(--green)}
/* 2) 运行信息 侧栏卡片：奶油黄底 */
.sidebar .panel .metric{background:#f3ecdb;border-color:#e4d9bd}
/* 3) tab 条：奶油容器 */
.tabs{background:#f1ecdd;border-color:#e0d8c2}
/* 4a) 运行概览 #summary 卡：绿 wash */
#summary .metric{background:#e9efe2;border-color:#c6dfcc}
#summary .metric.warn{background:#fbe6e4;border-color:#ecc3bd}
/* 4b) 接口健康画像 #interface-health 卡：青 wash */
#interface-health .metric{background:#e7f0f4;border-color:#c2dae4}
/* 4c) 回归对比 metric-grid 卡（在 #summary 内的嵌 panel 中）：琥珀 wash */
#summary > .panel .metric-grid .metric{background:#faf0dd;border-color:#ead8a9}
#summary > .panel .metric-grid .metric.warn{background:#f8e8de;border-color:#e8c4a8}
/* 4d) Harness 本次闭环证据 卡：蓝 wash（在 #harness 面板内）*/
#harness .metric-grid .metric{background:#eef2f9;border-color:#ccd9ed}
/* 4e) Loop 工程 管道 stage 卡：铜 wash */
#loop .stage{background:#f7edde;border-color:#e4cfa9}
#loop .stage.alt{background:#f1e4cd;border-color:#dcc096}
/* ── 5) 卡片级 gruvbox-light 多色（参考 Harness cap 分层卡效果）── */
/* 运行信息 侧栏 7 卡：每卡独立色（tint 底 + 3px 左条 + 同色数值） */
.sidebar .metrics .metric:nth-child(1){background:#d3e2ea;
  border-color:#9fb6c2;border-left:3px solid #076678}
.sidebar .metrics .metric:nth-child(1) b{color:#076678}
.sidebar .metrics .metric:nth-child(2){background:#e6e3b8;
  border-color:#c2be7e;border-left:3px solid #8b8d0a}
.sidebar .metrics .metric:nth-child(2) b{color:#8b8d0a}
.sidebar .metrics .metric:nth-child(3){background:#ecc5bd;
  border-color:#d18d82;border-left:3px solid #9d0006}
.sidebar .metrics .metric:nth-child(3) b{color:#9d0006}
.sidebar .metrics .metric:nth-child(4){background:#f2dfc6;
  border-color:#dca45f;border-left:3px solid #af3a03}
.sidebar .metrics .metric:nth-child(4) b{color:#af3a03}
.sidebar .metrics .metric:nth-child(5){background:#d9e6c6;
  border-color:#a3ba85;border-left:3px solid #427b58}
.sidebar .metrics .metric:nth-child(5) b{color:#427b58}
.sidebar .metrics .metric:nth-child(6){background:#ecd8dd;
  border-color:#c79ca6;border-left:3px solid #8f3f71}
.sidebar .metrics .metric:nth-child(6) b{color:#8f3f71}
.sidebar .metrics .metric:nth-child(7){background:#d9d2c0;
  border-color:#a89f86;border-left:3px solid #665c54}
.sidebar .metrics .metric:nth-child(7) b{color:#665c54}
/* 运行概览 10 卡：每卡独立 gruvbox-light 色 */
#summary .metrics .metric:nth-child(1){background:#d3e2ea;
  border-color:#9fb6c2;border-left:3px solid #076678}
#summary .metrics .metric:nth-child(1) b{color:#076678}
#summary .metrics .metric:nth-child(2){background:#e6e3b8;
  border-color:#c2be7e;border-left:3px solid #8b8d0a}
#summary .metrics .metric:nth-child(2) b{color:#8b8d0a}
#summary .metrics .metric:nth-child(3){background:#ecc5bd;
  border-color:#d18d82;border-left:3px solid #9d0006}
#summary .metrics .metric:nth-child(3) b{color:#9d0006}
#summary .metrics .metric:nth-child(4){background:#d9e6c6;
  border-color:#a3ba85;border-left:3px solid #427b58}
#summary .metrics .metric:nth-child(4) b{color:#427b58}
#summary .metrics .metric:nth-child(5){background:#f0e2b8;
  border-color:#d3b877;border-left:3px solid #b57614}
#summary .metrics .metric:nth-child(5) b{color:#b57614}
#summary .metrics .metric:nth-child(6){background:#ecd8dd;
  border-color:#c79ca6;border-left:3px solid #8f3f71}
#summary .metrics .metric:nth-child(6) b{color:#8f3f71}
#summary .metrics .metric:nth-child(7){background:#d9d2c0;
  border-color:#a89f86;border-left:3px solid #665c54}
#summary .metrics .metric:nth-child(7) b{color:#665c54}
#summary .metrics .metric:nth-child(8){background:#f2dfc6;
  border-color:#dca45f;border-left:3px solid #af3a03}
#summary .metrics .metric:nth-child(8) b{color:#af3a03}
#summary .metrics .metric:nth-child(9){background:#e2bdb8;
  border-color:#c98a84;border-left:3px solid #9d0006}
#summary .metrics .metric:nth-child(9) b{color:#9d0006}
#summary .metrics .metric:nth-child(10){background:#d6cfbb;
  border-color:#a89f86;border-left:3px solid #665c54}
#summary .metrics .metric:nth-child(10) b{color:#665c54}
/* warn 语义优先：任何被标 warn 的卡回到红系 */
.sidebar .metrics .metric.warn,#summary .metrics .metric.warn{
  background:#ecc5bd;border-color:#d18d82;border-left:3px solid #9d0006}
.sidebar .metrics .metric.warn b,#summary .metrics .metric.warn b{color:#9d0006}
/* 运行概览卡：数值加深醒目 + 单行不折行 */
#summary .metrics .metric{display:flex;flex-direction:column;gap:1px;
  justify-content:center}
#summary .metrics .metric small{font:700 11px/1.1 var(--sans);color:var(--muted);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#summary .metrics .metric b{font:800 22px/1.1 var(--mono);letter-spacing:.3px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#summary .metrics .metric .hint{font-size:10px;color:var(--muted);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
</style>
</head>
<body>
<div class="trend-tip" id="trend-tip" hidden></div>
<main>
<header class="top"><div><div class="eyebrow">API Test Harness · 分层接口测试框架</div>
  <h1>API 测试运行报告</h1>
  <p class="sub">{{ run_time }} · 目标={{ target or base_url }}</p>
  <p class="sub">{{ passed }}/{{ total }} passed, {{ failed }} failed · 通过率 {{ pass_rate }}%</p>
  <p class="sub">计划 {{ execution_counts.planned }} · 执行 {{ execution_counts.executed }} ·
    阻断 {{ execution_counts.blocked }} · 跳过 {{ execution_counts.skipped }} ·
    未验证 {{ execution_counts.unverified }} · 实际评估断言 {{ execution_counts.assertions }}</p>
  <p class="sub">断言密度：平均 {{ assert_density.avg_per_case }}/用例 ·
    高(≥3) {{ assert_density.high }} · 中(1-2) {{ assert_density.medium }} ·
    无(0) {{ assert_density.none }}{% if assert_density.no_assert_cases %}
    · 无断言: {{ assert_density.no_assert_cases | join(', ') }}{% endif %}</p>
  {% if hero_line %}<p class="sub">{{ hero_line }}</p>{% endif %}
  {% if version %}<p class="sub">framework v{{ version }} · llm={{ llm_mode }}</p>{% endif %}
  <p class="tagline">pytest 参量化的单接口 / 场景流用例 · 变量上下文与断言提取 · 令牌生命周期 ·
    L1 确定性重试 + L2 LLM 自愈反思循环 · 多 agent 意图调度 · 风险预测分析 · Langfuse 追踪。</p>
</div>
<div class="badge-slot">
  <div class="chips">
    {% if execution_counts.planned > 0 and execution_counts.passed == execution_counts.planned %}
    <span class="badge pass dot">ALL GREEN</span>
    {% elif execution_counts.failed > 0 %}
    <span class="badge fail dot">FAILED</span>
    {% else %}
    <span class="badge review dot">UNVERIFIED</span>
    {% endif %}
    <span class="badge neutral dot">LLM: {{ llm_mode }}</span>
    {% if trace_enabled %}<span class="badge neutral dot">Trace: on</span>{% endif %}
  </div>
  <div class="download-group">
  <button class="btn primary" onclick="downloadHtml()">HTML</button>
  <button class="btn" onclick="downloadMd()">Markdown</button>
  <button class="btn" onclick="downloadJson()">JSON</button></div>
</div></header>
<div class="report-shell">
<aside class="sidebar">
<section class="panel"><h2>运行信息</h2>
  <div class="metrics">
    <div class="metric"><small>总用例</small><b>{{ total }}</b></div>
    <div class="metric"><small>通过</small><b>{{ passed }}</b></div>
    <div class="metric"><small>失败</small><b>{{ failed }}</b></div>
    <div class="metric"><small>通过率</small><b>{{ pass_rate }}%</b></div>
    <div class="metric"><small>自愈成功</small><b>{{ healed_count }}</b></div>
    <div class="metric"><small>根因分析</small><b>{{ triage_count }}</b></div>
    <div class="metric wide"><small>运行时间</small><b>{{ run_time }}</b></div>
  </div>
</section>
<nav class="panel side-nav" aria-label="报告导航">
  <div class="nav-label">总览</div>
  <a href="#summary"><span>运行概览</span><b>→</b></a>
  <div class="nav-label">框架</div>
  <a href="#harness"><span>能力架构 · Harness</span><b>→</b></a>
  <a href="#loop"><span>运行循环 · Loop 工程</span><b>→</b></a>
  <div class="nav-label">结果</div>
  <a href="#detail"><span>用例结果</span><b>→</b></a>
  <a href="#failures"><span>失败分析</span><b>→</b></a>
  {% if prediction %}<a href="#prediction"><span>预测分析</span><b>→</b></a>{% endif %}
</nav>
</aside>
<div class="content">

<nav class="tabs" aria-label="快速跳转">
  <a href="#summary">运行概览</a>
  <a href="#detail">用例结果</a>
  <a href="#failures">失败分析</a>
  <a href="#harness">框架</a>
  <a href="#loop">Loop</a>
  {% if prediction %}<a href="#prediction">预测</a>{% endif %}
</nav>

<details class="panel answhead" id="summary" open>
<summary><h2>运行概览</h2></summary>
<div class="metrics">
   <div class="metric"><small>总用例</small><b>{{ total }}</b>
     <div class="hint">单接口 + 场景流</div></div>
   <div class="metric"><small>通过</small><b>{{ passed }}</b>
     <div class="hint">含自愈后通过</div></div>
   <div class="metric"><small>失败</small><b>{{ failed }}</b>
     <div class="hint">待修复</div></div>
   <div class="metric"><small>自愈成功</small><b>{{ healed_count }}</b>
     <div class="hint">L1 重试 / L2 LLM</div></div>
   <div class="metric"><small>非首过（flaky）</small><b>{{ flaky_count }}</b>
     <div class="hint">自愈后通过 或 跨次运行通败抖动</div></div>
   <div class="metric"><small>LLM 根因分析</small><b>{{ triage_count }}</b>
     <div class="hint">失败解释</div></div>
   <div class="metric"><small>LLM 模式</small><b>{{ llm_mode }}</b>
     <div class="hint">on: 调 LLM 生成分析；off: 走确定性回退</div></div>
   {% if evo_insight %}
   <div class="metric"><small>自主演进</small><b>{{ evo_insight.rounds }} 轮</b>
     <div class="hint">候选 {{ evo_insight.candidates }} · 采纳 {{ evo_insight.accepted }}
       · 拒绝 {{ evo_insight.rejected }}{% if evo_insight.planned_suite %}
       · 规划 {{ evo_insight.planned_suite }}{% endif %}{% if evo_insight.candidates %}
       · 采纳率 {{ '%.0f'|format(evo_insight.accepted / evo_insight.candidates * 100) }}%
       · 去重跳过 {{ evo_insight.dedup_skipped }}{% else %}
       · 本轮无候选{% endif %}</div></div>
   {% endif %}
   {% if tel_insight %}
   <div class="metric {{ 'warn' if tel_insight.errors > 0 else '' }}"><small>LLM 成本</small>
     <b>${{ '%.4f'|format(tel_insight.cost) }}</b>
     <div class="hint">{{ tel_insight.requests }} 调用 · 成功率
       {{ '%.0f'|format(tel_insight.success_rate * 100) }}%{% if tel_insight.errors %}
       · {{ tel_insight.errors }} 失败{% endif %}
  {% endif %}
  {% if laya_insight %}
  <div class="metric"><small>决策模型 Laya</small>
    <b>{{ laya_insight.stages|length }} 环节</b>
    <div class="hint">{{ '开闸·' if laya_insight.enabled else '' }}分流
      {{ laya_insight.signal_types }} 次 · 严重度采纳
      {{ laya_insight.severity_adopted }}</div></div>
  {% endif %}
  {% if capabilities %}
   <div class="metric"><small>框架能力</small>
     <b>{{ capabilities.used }}/{{ capabilities.total }}</b>
     <div class="hint">本次已用新功能点数</div></div>
   {% endif %}
</div>
{% if capabilities %}
<details class="panel answhead" id="capabilities" style="margin-top:14px">
  <summary><h2>框架能力矩阵（{{ capabilities.used }}/{{ capabilities.total }} 已用）</h2></summary>
  <p class="place-text">本次运行已用 {{ capabilities.used }} 项新功能；其余为实现但
    本次未触发的工具。数据源：自主演进 = <code>_evolution_log.jsonl</code>，
    LLM 成本 = <code>llm_usage.jsonl</code> 账本。</p>
  <table class="fail-table" style="margin-top:8px">
    <thead><tr><th>#</th><th>功能点</th><th>模块</th><th>子命令</th><th>状态</th></tr></thead>
    <tbody>
    {% for c in capabilities.rows %}
      <tr>
        <td>{{ c.id }}</td>
        <td>{{ c.name | e }}</td>
        <td>{{ c.module | e }}</td>
        <td><code>{{ c.cmd | e }}</code></td>
        <td>{% if c.used %}<span class="cat">已用</span>{% else %}可用{% endif %}</td>
      </tr>
      {% if c.used %}
      <tr class="cap-detail">
        <td></td>
        <td colspan="4">
          <details>
            <summary>{{ c.name | e }} · 本次运行详情</summary>
            <div class="hint">{{ c.desc | e }}</div>
            {% if c.evidence %}
            {% for seg in c.evidence.split('；') %}
              {% if seg %}<div class="hint">· {{ seg | e }}</div>{% endif %}
            {% endfor %}
            {% else %}
            <div class="hint">本次运行未产生可直接量化的产物。</div>
            {% endif %}
          </details>
        </td>
      </tr>
      {% endif %}
    {% endfor %}
    </tbody>
  </table>
</details>
{% endif %}
{% if laya_insight %}
<details class="panel answhead" id="laya-gate" style="margin-top:14px">
  <summary><h2>决策模型前置层 · Laya（{{ laya_insight.stages|length }} 环节）</h2></summary>
  <p class="place-text">LLM-judge 决策点前增设 LayaGate 高置信分流：对确定性可判的
    环节短路放行，低置信 / 未开闸一律回退原流程。
    {{ '本次 AH_LAYA 已开闸。' if laya_insight.enabled else '本次未显式开闸，仅按真实信号计数。' }}
    本项目开关默认关，未开闸时字节等价、零网络。</p>
  <div style="display:flex;gap:12px;flex-wrap:wrap;margin:10px 0">
    <div class="metric"><small>分流信号</small><b>{{ laya_insight.signal_types }}</b>
      <div class="hint">triage / tool_risk / stop / 接口风险评分</div></div>
    <div class="metric"><small>严重度采纳</small><b>{{ laya_insight.severity_adopted }}</b>
      <div class="hint">报告失败分级采纳 Laya 裁决</div></div>
    <div class="metric"><small>涉及环节</small><b>{{ laya_insight.stages|length }}</b>
      <div class="hint">{{ laya_insight.points|join(' · ') if laya_insight.points
        else '—' }}</div></div>
  </div>
  {% if laya_insight.stages %}
  <table class="fail-table" style="margin-top:8px">
    <thead><tr><th>决策环节</th></tr></thead>
    <tbody>
    {% for s in laya_insight.stages %}
      <tr><td><code>{{ s }}</code>{% if s == 'laya_triage' %} · 失败解释编排{% endif %}
        {% if s == 'laya_tool_risk' %} · 多服务路由风险前置{% endif %}
        {% if s == 'laya_no_more_value' %} · 演进停止条件{% endif %}
        {% if s == 'risk_score' %} · 接口风险打分{% endif %}</td></tr>
    {% endfor %}
    </tbody>
  </table>
  {% endif %}
</details>
{% endif %}
{% if interface_health %}
<details class="panel" id="interface-health" style="margin-top:14px">
  <summary><h2>接口健康画像</h2></summary>
  <p class="place-text">本次 {{ interface_health.total }} 例接口运行的健康摘要：状态码分布、
    flaky（非首过即绿）与耗时分位数。</p>
  <div style="display:flex;gap:12px;flex-wrap:wrap;margin:10px 0">
    <div class="metric"><small>HTTP 状态</small>
      <b>{% for k, v in interface_health.status_dist.items() %}{{ k }}:{{ v }} ·
        {% endfor %}</b>
      <div class="hint">成功 {{ interface_health.status_dist.get('200', 0) }}</div></div>
    <div class="metric"><small>flaky</small><b>{{ interface_health.flaky }}</b>
      <div class="hint">非首过即绿</div></div>
    <div class="metric"><small>平均耗时</small><b>{{ interface_health.avg_ms }} ms</b>
      <div class="hint">P95 {{ interface_health.p95_ms }} ms</div></div>
    <div class="metric"><small>P99</small><b>{{ interface_health.p99_ms }} ms</b>
      <div class="hint">最大 {{ interface_health.max_ms }} ms</div></div>
  </div>
  {% if interface_health.has_hist %}
  <table class="fail-table" style="margin-top:8px">
    <thead><tr><th>耗时区间</th><th>用例数</th><th>占比</th><th>分布</th></tr></thead>
    <tbody>
    {% for h in interface_health.hist_rows %}
      <tr><td>{{ h.label }}</td><td>{{ h.count }}</td><td>{{ h.pct }}%</td>
        <td>{{ '█' * (h.pct_10 or 0) }}</td></tr>
    {% endfor %}
    </tbody>
  </table>
  {% endif %}
</details>
{% endif %}
{% if tel_usage_rows %}
<details class="panel" id="llm-usage" style="margin-top:14px">
  <summary><h2>LLM 用途分布 · 时延</h2></summary>
  <p class="place-text">按 LLM 调用任务标签（task）分组的调用数与时延（ms），数据源
    <code>llm_usage.jsonl</code>。</p>
  <table class="fail-table" style="margin-top:8px">
    <thead><tr><th>用途 (task)</th><th>调用</th><th>平均时延</th><th>最大时延</th></tr></thead>
    <tbody>
    {% for t in tel_usage_rows %}
      <tr><td>{{ t.task | e }}</td><td>{{ t.requests }}</td>
        <td>{{ t.avg_ms }} ms</td><td>{{ t.max_ms }} ms</td></tr>
    {% endfor %}
    </tbody>
  </table>
</details>
{% endif %}
{% if slow_top %}
<h3 style="margin-top:14px">耗时分布 · 最慢 {{ slow_top|length }} 例</h3>
<table class="fail-table" style="margin-top:6px">
  <thead><tr><th>用例</th><th>耗时</th></tr></thead>
  <tbody>
  {% for s in slow_top %}
    <tr><td>{{ s.name | e }}</td><td class="slow-dur">{{ s.dur_ms }} ms</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endif %}
<table class="kv env-table">
  <thead><tr><th>维度</th><th>值</th></tr></thead>
  <tbody>
    <tr><th>目标 base URL</th><td><code>{{ target or base_url | e }}</code></td></tr>
    {% if llm_provider %}<tr><th>LLM 提供方</th><td><code>{{ llm_provider | e }}</code>
      （cheap: {{ llm_cheap | e }} / strong: {{ llm_strong | e }}）</td></tr>{% endif %}
    <tr><th>令牌存储</th><td><code>{{ token_store | e }}</code></td></tr>
    <tr><th>日志目录</th><td><code>{{ log_dir | e }}</code></td></tr>
    <tr><th>报告目录</th><td><code>{{ report_dir | e }}</code></td></tr>
    <tr><th>追踪</th><td><code>{{ 'enabled' if trace_enabled
      else 'disabled (NullTracer)' }}</code></td></tr>
    {% for k, v in (run_meta or {}).items() %}
      {% if k != 'trace_urls' %}
    <tr><th>{{ k | e }}</th><td><code>{{ v | e }}</code></td></tr>
      {% endif %}
    {% endfor %}
    {% if trace_urls %}
      {% for k, v in trace_urls.items() %}
    <tr><th>Trace · {{ k | e }}</th><td><a href="{{ v | e }}"
      target="_blank" rel="noopener">{{ v | e }}</a></td></tr>
      {% endfor %}
    {% endif %}
  </tbody>
</table>
{% if prev_run %}
<div class="panel" style="margin-top:14px"><h3>回归对比（vs 上一运行）</h3>
  {% if env_mismatch %}
  <div class="hint-bar warn">⚠ 与上一运行环境不同（{{ (prev_run.env or {}).base_url | e }}
    vs {{ base_url | e }}），以下指标可能不可直接对比。</div>
  {% endif %}
  {% if metric_cards %}
  <div class="metric-grid">
  {% for c in metric_cards %}
    <div class="metric {{ 'warn' if c.warn else '' }}">
      <span class="ml">{{ c.label | e }}</span>
      <span class="mc">{{ c.cur | e }}</span>
      <span class="mv">prev {{ c.prev | e }}{% if c.delta is not none %}
        · {{ '%+d'|format(c.delta) }}{{ c.unit | e }}{% endif %}
        {% if c.sub %}<span class="sd">{{ c.sub | e }}</span>{% endif %}</span>
    </div>
  {% endfor %}
  </div>
  {% endif %}
  {% if new_failures %}
  <div class="hint-bar warn">⚠ 新增失败/转红（{{ new_failures|length }}）：
    {% for n in new_failures -%}
      <code>{{ n | e }}</code>{% if not loop.last %}、{% endif -%}
    {%- endfor %}
  </div>
  {% endif %}
  {% if fixed %}
  <div class="hint-bar">已修复（{{ fixed|length }}）：
    {% for n in fixed %}<code>{{ n | e }}</code>{% if not loop.last %}、{% endif %}{% endfor %}
  </div>
  {% endif %}
  {% if case_diffs %}
  <table class="fail-table" style="margin-top:10px">
    <thead><tr><th>用例</th><th>状态变化</th><th>耗时 prev → cur</th></tr></thead>
    <tbody>
    {% for d in case_diffs %}
      <tr>
        <td>{{ d.name | e }}</td>
        <td><span class="cat {{ 'warn' if d.to != 'PASS' else '' }}">{{ d.from | e }}
          → {{ d.to | e }}</span></td>
        <td>{% if d.prev_dur is not none %}{{ d.prev_dur }} ms{% else %}—{% endif %}
          → {% if d.cur_dur is not none %}{{ d.cur_dur }} ms{% else %}—{% endif %}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% endif %}
  {% if not new_failures and not fixed and not case_diffs %}
  <p class="place-text">与上一运行相比无结果状态与耗时变化。</p>
  {% endif %}
</div>
{% endif %}
{% if trend and trend|length > 1 %}
<div class="panel" style="margin-top:14px"><h3>通过率趋势（近 {{ trend|length }} 次运行）</h3>
  {% if trend_svg %}
  {{ trend_svg | safe }}
  {% else %}
  <div class="trend">
  {% for t in trend %}
    <div class="t"><span class="tm">{{ t.run_time[-5:] }}</span>
      <div class="tr-track"><i class="{{ 'low' if t.pass_rate is not none
        and t.pass_rate < 90 else '' }}"
        style="width:{{ t.pass_rate or 0 }}%"></i></div>
      <span class="pr">{{ t.pass_rate or 0 }}%</span>
    </div>
  {% endfor %}
  </div>
  {% endif %}
</div>
{% endif %}
</details>

<details class="panel answhead" id="detail" open>
<summary><h2>用例结果</h2></summary>
<p class="place-text">逐用例结果 —— 点击用例行展开 请求 / 响应
  {% if has_detail %}/ 自愈{% endif %} 详情。</p>
{% for r in results %}
<details id="{{ r.detail_id }}" class="details-section"
  {{ 'open' if r.result != 'PASS' else '' }}>
<summary><h2><span class="case-n">{{ loop.index }}.</span>
  {{ r.name | e }}
  <span class="case-id">
    <span class="badge {{ r.badge_cls }}">{{ r.result }}</span>
    {% if r.flaky %}<span class="badge flaky">flaky · {{ r.flaky_kind }}</span>{% endif %}
    {% if r.semantic.flag %}<span class="badge review">语义需复核</span>{% endif %}
    · {{ "%.3f"|format(r.duration) }} s
    {% if r.result != "PASS" and r.failure_msg %}<span class="fail-msg">
      {{ r.failure_msg | e }}</span>{% endif %}
</h2><span class="fold">▸ 详情（请求/响应{% if has_detail %}/自愈{% endif %}）</span></summary>
<div class="inner">
<div class="case-columns">
{% if r.heal %}
<section><h3>自愈</h3>
  <p class="mrow"><code>{{ r.heal | e }}</code></p>
  {% for hf in r.heal_fields %}
  <p class="mrow heal-row"><span class="lbl">{{ hf.k | e }}</span>
    <code>{{ hf.v | e }}</code></p>
  {% endfor %}
</section>
{% endif %}
<section><h3>请求</h3>
  {% if r.request %}
  <p class="mrow"><span class="lbl">调用</span>
    <code>{{ r.request.method | e }} {{ r.request.path | e }}</code></p>
  {% if r.request.params %}<p class="mrow"><span class="lbl">Params</span>
    <code>{{ r.request.params | string | e }}</code></p>{% endif %}
  {% if r.request.json %}<p class="mrow"><span class="lbl">JSON</span>
    <code>{{ r.request.json | string | e }}</code></p>{% endif %}
  {% if r.request.data %}<p class="mrow"><span class="lbl">Data</span>
    <code>{{ r.request.data | string | e }}</code></p>{% endif %}
  {% else %}<p class="place-text">—</p>{% endif %}
</section>
<section><h3>响应</h3>
  {% if r.response %}
  <p class="mrow"><span class="lbl">状态</span>
    <code>status {{ r.response.status_code | string | e }}</code>
    {% if r.response.latency_ms is not none %} · {{ r.response.latency_ms }} ms{% endif %}
  </p>
  {% if r.trace_id %}
  <p class="mrow"><span class="lbl">追踪</span>
    {% if r.trace_url %}<a class="trace-link"
      href="{{ r.trace_url | e }}" target="_blank" rel="noopener"
      title="在 Langfuse 查看该用例链路">{{ r.trace_id | e }}</a>
    {% else %}<code>{{ r.trace_id | e }}</code>{% endif %}
  </p>
  {% endif %}
  {% if r.response.headers %}
  <p class="mrow"><span class="lbl">Headers</span>
    <code>{{ r.response.headers | string | e }}</code></p>
  {% endif %}
  {% if r.response.text %}
   <div class="dump-wrap"><pre class="dump" id="dump-{{ r.detail_id }}"
    >{{- r.response.text | e -}}</pre>
    <button class="copy-btn" data-target="dump-{{ r.detail_id }}"
      type="button">复制</button></div>
   {% if r.response.truncated %}
   <p class="trunc-note">⚠ 响应体已截断（原始 {{ r.response.full_len }}
     字符，仅展示前 500）——完整响应见执行日志。</p>
   {% endif %}
   {% endif %}
   {% else %}<p class="place-text">—</p>{% endif %}
</section>
</div>
{% if r.assertions %}
<section class="assertions"><h3>断言详情</h3>
  <table class="assert-table">
  <thead><tr><th>类型</th><th>逻辑</th><th>期望值</th><th>实际值</th><th>结果</th></tr></thead>
  <tbody>
  {% for a in r.assertions %}
  <tr class="{{ 'ok' if a.passed else 'bad' }}">
    <td><code>{{ a.type | e }}</code></td>
    <td><code>{{ a.logic | e }}</code></td>
    <td><code>{{ a.expected | e }}</code></td>
    <td><code>{{ a.actual | e }}</code></td>
    <td><span class="badge {{ 'pass' if a.passed else 'fail' }}">
      {{ '通过' if a.passed else '失败' }}</span></td>
  </tr>
  {% endfor %}
  </tbody>
  </table>
</section>
{% endif %}
{% if r.failure_msg %}
{% set healed = r.result == "PASS" %}
<p class="mrow" style="margin-top:12px">
  <span class="lbl {{ 'ok' if healed else '' }}">
    {{ '原始失败（已自愈）' if healed else '失败' }}</span>
  <code>{{ r.failure_msg | e }}</code></p>
{% if r.diff %}
<div class="cmp">
  <div class="side expected"><h4>期望（expected）</h4>
    <code>{{ r.diff.expected | e }}</code></div>
  <div class="side actual"><h4>实际（got）</h4>
    <code>{{ r.diff.actual | e }}</code></div>
</div>
{% endif %}
{% endif %}
</div>
</details>
{% endfor %}
</details>
<details class="panel answhead" id="failures"
  {{ 'open' if failed > 0 else '' }}>
<summary><h2>失败分析</h2></summary>
{% if fail_buckets %}
<div class="failbars">
{% for b in fail_buckets %}
  <div class="failbar"><span>{{ b.label | e }}</span>
    <div class="track"><i style="width:{{ b.pct }}%"></i></div>
    <span class="n">{{ b.count }}</span></div>
{% endfor %}
</div>
{% else %}
<p class="place-text">本次运行无失败用例 —— 全部通过（或经自愈恢复）。</p>
{% endif %}
{% if fail_details %}
<table class="fail-table">
  <thead><tr><th>用例</th><th>类别</th><th>根因</th><th>建议</th></tr></thead>
  <tbody>
  {% for d in fail_details %}
    <tr>
      <td><a href="#{{ d.detail_id }}">{{ d.name | e }}</a></td>
      <td><span class="cat">{{ d.category | e }}</span></td>
      <td>{{ d.root_cause | e }}</td>
      <td>{{ d.suggestion | e if d.suggestion else '—' }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% endif %}
{% if healed_count %}
<div class="hint-bar">自愈 {{ healed_count }} 例：L1 确定性重试吸收瞬时失败；
  L2 LLM 反思给出候选修复并重跑验证。</div>
{% endif %}
{% if triage_count %}
<div class="hint-bar warn">已执行 {{ triage_count }} 次根因分析（AgentTriage），
  失败原因记录到 llm_notes。</div>
{% endif %}
{% if gov_stats and (gov_stats.blocked or gov_stats.observe_recovered) %}
<div class="hint-bar warn">失败治理：<b>{{ gov_stats.blocked }}</b> 例
  product_defect 已 block 终止（等人工确认，不盲目自愈）；
  <b>{{ gov_stats.observe_recovered }}</b> 例环境/抖动已 observe 无侵入恢复
  （只重跑原步骤，不回写脚本）。</div>
{% endif %}
</details>

<details class="panel answhead" id="harness">
<summary><h2>能力架构 · Harness 分层</h2></summary>
<p class="place-text">框架按职责划分为 8 层；全部 LLM 能力以 helper 存在，默认隔离
  （LLMOFF=1 零网络）。</p>
<div class="cap-grid">
  <div class="cap ic-forest"><div class="icn">C</div><div><h3>core · 配置 / 编排</h3>
    <p><code>config.py</code> 单例 <code>Settings</code>（env 覆盖）；<code>orchestrator</code>
      驱动单次管道与自愈循环；<code>dispatch</code> 多 agent 意图路由。</p></div></div>
  <div class="cap ic-jade"><div class="icn">E</div><div><h3>environment · 令牌生命周期</h3>
    <p><code>token.py</code> 静态/可刷新令牌、<code>expires_at</code> 过期判定、
      线程安全写回存储。</p></div></div>
  <div class="cap ic-blue"><div class="icn">U</div><div><h3>utils · 变量上下文</h3>
    <p><code>context.py</code> 线程本地 <code>ContextManager</code>、<code>${var}</code>
      插值、全局并行安全。</p></div></div>
  <div class="cap ic-purple"><div class="icn">H</div><div><h3>harness · HTTP + 用例</h3>
    <p><code>http.py</code> 统一 <code>APIResponse</code>；<code>loader.py</code>/
      <code>registry.py</code> 解析 YAML 用例与场景流。</p></div></div>
  <div class="cap ic-amber"><div class="icn">M</div><div><h3>metrics · 断言 + 提取</h3>
    <p><code>assertions.py</code> <code>assert_response</code>
      （status/schema/jsonpath/contains）；<code>extract.py</code> jsonpath/regex 提取并回写。
      </p></div></div>
  <div class="cap ic-jade"><div class="icn">L</div><div><h3>llm · agents 助手</h3>
    <p><code>BaseLLM</code>/<code>LLMClient</code> cheap/strong 路由；4 agent：数据生成、
      诊断、自愈、报告分析；LLM 关闭时返回确定性 canned 值。</p></div></div>
  <div class="cap ic-red"><div class="icn">R</div><div><h3>reporting · 报告 + 预测</h3>
    <p><code>writer.py</code> md/json/html 报告；<code>predict</code> 历史风险回归、
      ASSIST 扩展 agent、运行分析。</p></div></div>
  <div class="cap ic-slate"><div class="icn">T</div><div><h3>tools · 命令行工具</h3>
    <p>统一的 <code>cli.py</code> 子命令仓库：<code>evolution</code> 自主涌现闭环、
      <code>scaffold</code> 项目脚手架、<code>scheduler</code> 回归调度/隔离、
      <code>telemetry</code> LLM 成本账本、<code>property</code> 参数空间探索。</p></div></div>
</div>
<div class="pair-grid">
  <div class="cap"><div><h3>为什么分层 · 每一层对应一个可验证的收益</h3>
    <p class="place-text">分层不是代码洁癖——每层都让"能不能跑、跑得对不对"可被
    单独证明，从而支撑 Hermetic 基准与 AI 辅助迭代：</p>
    <table class="extra-tbl">
      <thead><tr><th>层</th><th>解决什么</th><th>带来的收益</th></tr></thead>
      <tbody>
        <tr><td><code>core</code></td><td>配置散落、管道无主</td>
          <td>一次运行 = 一条确定性管道，可复现</td></tr>
        <tr><td><code>environment</code></td><td>令牌过期/并发写坏</td>
          <td>令牌生命周期自洽，跨用例不串</td></tr>
        <tr><td><code>utils</code></td><td>用例间上下文隔离</td>
          <td><code>${var}</code> 并行安全，无共享态污染</td></tr>
        <tr><td><code>harness</code></td><td>HTTP 封装不统一</td>
          <td>统一 <code>APIResponse</code>，请求可注入可替换</td></tr>
        <tr><td><code>metrics</code></td><td>断言/提取耦合用例</td>
          <td>断言即函数，可单测、可复用、可统计</td></tr>
        <tr><td><code>llm</code></td><td>LLM 把持主流程</td>
          <td>helper 化：关断走 canned，框架永不被 LLM 卡死</td></tr>
        <tr><td><code>reporting</code></td><td>结果不可回溯</td>
          <td>md/json/html + 趋势 + 预测，闭环可审计</td></tr>
        <tr><td><code>tools</code></td><td>能力散落、无统一入口</td>
          <td>五类子命令统一入口，可组合、可脚本化、可判闸</td></tr>
      </tbody>
    </table>
  </div></div>
  <div class="cap"><div><h3>本次闭环证据</h3>
    <p class="place-text">分层最终兑现为可度量的运行结果——本次管道、自愈与报告
    三层协同的实际产出：</p>
    <div class="metric-grid">
      <div class="metric"><span class="ml">用例</span>
        <span class="mc">{{ passed }}/{{ total }}</span><span class="mv">通过/总数</span></div>
      <div class="metric {{ 'warn' if healed_count else '' }}"><span class="ml">自愈</span>
        <span class="mc">{{ healed_count }}</span><span class="mv">L1/L2 吸收的失败</span></div>
      <div class="metric"><span class="ml">根因</span>
        <span class="mc">{{ triage_count }}</span><span class="mv">AgentTriage 分析</span></div>
      <div class="metric {{ 'warn' if flaky_count else '' }}"><span class="ml">非首过</span>
        <span class="mc">{{ flaky_count }}</span><span class="mv">抖动用例</span></div>
      <div class="metric"><span class="ml">LLM</span>
        <span class="mc">{{ llm_mode }}</span><span class="mv">
          {{ '自动增强' if llm_enabled else 'hermetic 确定性' }}</span></div>
    </div>
    <p class="place-text">{% if healed_count or flaky_count %}本次有
      <b>{{ healed_count + flaky_count }}</b> 例非首过：若普通框架直接判红，
      这 {{ healed_count + flaky_count }} 例会变成噪音误报；loop 把它们精确归为
      瞬时失败或自愈成功，让“已验证失败”只留下 {{ failed }} 例。{% elif passed == total %}
      本次全部一次通过，无自愈/抖动。{% else %}本次存在失败或未验证步骤，不能宣称全部通过。
      {% endif %}</p>
  </div></div>
</div>
</details>

<details class="panel answhead" id="loop">
<summary><h2>运行循环 · Loop 工程</h2></summary>
<p class="place-text">每次 <code>run_single</code> 走一遍确定性管道；失败后进入
  L1 重试 + L2 LLM 自愈反思循环。</p>
<div class="pipe">
  <div class="stage"><small>0 · 渲染</small><b>render(ctx)</b>
    <span class="sub">插值 ${var} | 上下文</span></div>
  <span class="arrow">→</span>
  <div class="stage"><small>1 · 造数</small><b>data_gen</b>
    <span class="sub">llm_gen 用例 | AgentDataGen</span></div>
  <span class="arrow">→</span>
  <div class="stage"><small>2 · 请求</small><b>HTTPClient</b>
    <span class="sub">token + 头 + 日志</span></div>
  <span class="arrow">→</span>
  <div class="stage"><small>3 · 提取</small><b>extract</b>
    <span class="sub">jsonpath / regex → ctx</span></div>
  <span class="arrow">→</span>
  <div class="stage"><small>4 · 断言</small><b>assert_response</b>
    <span class="sub">status/schema/jsonpath/contains</span></div>
  <span class="arrow">→</span>
  <div class="stage"><small>5 · 通过</small><b>StepResult</b>
    <span class="sub">succeeded / extracted / llm_notes</span></div>
</div>
<div class="hint-bar">失败分支：<b>L1</b> 原样重跑整条管道吸收瞬时失败 → 再失败则 <b>L2</b>
  诊断根因（AgentTriage）+ 反思自愈（AgentHeal ≤ {{ max_heal_attempts }} 次尝试，
  候选经重跑验证）。</div>
<div class="loop-row">
  <div class="loop-col"><h3>失败处理循环</h3>
    <div class="pipe" style="flex-direction:column;align-items:stretch">
      <div class="stage"><small>⑥ 失败</small><b>AssertionError / HTTP 错误</b>
        <span class="sub">标准化消息喂给 LLM</span></div>
      <span class="arrow" style="align-self:center">↓ L1 原样重跑</span>
      <div class="stage alt"><small>⑦ L1 重试</small><b>healed · L1 retry</b>
        <span class="sub">瞬时失败即自愈</span></div>
      <span class="arrow" style="align-self:center">↓ 仍失败 → triage 根因</span>
      <div class="stage alt"><small>⑧ L2 反思</small><b>heal ≤ {{ max_heal_attempts }}</b>
        <span class="sub">AgentHeal._propose_candidate → 重跑验证</span></div>
    </div>
  </div>
  <div class="loop-col"><h3>多 agent 调度 · dispatch</h3>
    <div class="scroll"><table class="agent-tbl">
      <thead><tr><th>意图</th><th>Agent</th><th>动作</th></tr></thead>
      <tbody>
        <tr><td><code>GENERATE_DATA</code></td><td><code>AgentDataGen</code></td>
          <td>为 llm_gen 用例合成请求体</td></tr>
        <tr><td><code>DIAGNOSE</code></td><td><code>AgentTriage</code></td><td>解释失败根因</td></tr>
        <tr><td><code>HEAL</code></td><td><code>AgentHeal</code></td><td>提出修正用例并重跑验证</td></tr>
        <tr><td><code>ANALYZE_REPORT</code></td><td><code>AgentReport</code></td>
          <td>分析结果 → markdown</td></tr>
      </tbody>
    </table></div>
  </div>
</div>
<div class="loop-pay">
  <h3>为什么 loop · 把"失败"从噪音改造成数据</h3>
  <p class="place-text">普通接口框架把一次失败当终态：红了就红，数量即结论。这里
  引入 loop 后，失败不再是一个布尔值，而是一条<b>被加工过的证据链</b>——每个失败
  都经过 ① L1 原样重跑吸收瞬时抖动 → ② L2 LLM 根因分析 → ③ 反思自愈并<b>重跑验证</b>
  → ④ 留下的真失败才进入统计。这一处理带来的三点实质优势：</p>
  <ul class="pay-list">
    <li><b>误报即根因</b>——瞬时失败（网络抖动/限流）被 L1 吸收，避免把环境噪音
      记成回归；L2 只对确定性失败动手，且自愈候选必须重跑通过才算数，LLM 不盲改。</li>
    <li><b>失败可归因可量化</b>——每个用例展开可见 请求/响应/自愈 三段 + 根因分析
      （<code>triage_count</code>）+ 自愈次数（≤ <code>{{ max_heal_attempts }}</code>），
      报告核减了什么、为什么核减全部可回溯审计。</li>
    <li><b>真失败 = 高信号</b>——本次 {{ total }} 例中 <code>{{ healed_count }}</code> 例
      自愈、<code>{{ flaky_count }}</code> 例非首过被精确归位后，只剩
      <code>{{ failed }}</code> 例进入修复队列——红灯是<b>值得看的红灯</b>，不是噪音。</li>
  </ul>
  <p class="place-text">闭环不依赖 LLM：<code>LLM_MODE=off</code> 时 L2 走确定性 canned
  回退，L1 重试与"自愈后重跑验证"的机制照常生效——loop 是框架的骨架，LLM 只是骨架上的
  一个可插拔增强。</p>
</div>
</details>

<details class="panel answhead" id="analysis" open>
<summary><h2>运行分析</h2></summary>
<p class="place-text">本次运行的确定性分析摘要（报告 NL 摘要；LLM 可用时追加
  自然语言观察，hermetic 下为纯数据驱动）。</p>
<div class="analysis-md">{{ analysis_html | safe }}</div>
</details>

{% if pred_has_history %}
<details class="panel answhead" id="prediction" open>
<summary><h2>预测分析 · 重点关注</h2></summary>
<p class="place-text">基于历史运行累积的风险概率（风险模型）与逐运行失败次数
  （经验回退）推演——供下次回归选型与方向判断。回归覆盖建议：
  <code>smoke</code>（冒烟）→ <code>regression</code>（回归）→ <code>prod</code>
  （生产回归），可用 <code>--ah-suite / --ah-env / --ah-priority</code> 筛选抽取。</p>
{% if key_interfaces %}
<h3>重点关注接口（历史累积失败最多）</h3>
<div class="result-table-wrap"><table>
<thead><tr><th>接口</th><th>历史失败次数</th><th>本次</th></tr></thead>
<tbody>
{% for k in key_interfaces %}
<tr>
  <td>{{ k.interface | e }}</td>
  <td>{{ k.failures }}</td>
  <td>{{ '未通过' if k.latest else '通过' }}</td>
</tr>
{% endfor %}
</tbody>
</table></div>
{% endif %}
{% if prediction %}
<h3>风险预测（回归必测，风险 &gt; 0）</h3>
<div class="result-table-wrap"><table>
<thead><tr><th>接口</th><th>风险</th><th>置信度</th><th>失败/总数</th>
  <th>主要失败类别</th></tr></thead>
<tbody>
{% for p in prediction %}
<tr>
  <td>{{ p.interface | e }}</td>
  <td>{{ "%.2f"|format(p.risk) }}</td>
  <td>{{ "%.2f"|format(p.confidence) }}</td>
  <td>{{ p.failures }}/{{ p.total }}</td>
  <td>{{ p.classes | e }}</td>
</tr>
{% endfor %}
</tbody>
</table></div>
{% else %}
<p class="place-text">历史库尚无失败样本，风险模型置信度不足（样本 &lt; {{
  3 }} 条/接口时置信度线性压低）；持续累积运行后自动启用。</p>
{% endif %}
{% if llm_advice %}
<h3>预测分析 · LLM 建议</h3>
<div class="llm-advice">{{ llm_advice_html | safe }}</div>
{% endif %}
</details>
{% endif %}
</div>
</div>
<footer class="foot">Generated by auto-api-test-harness reporting layer ·
  harness(core/environment/harness/metrics/llm/predict/reporting) ·
  loop(L1 retry + L2 LLM heal ≤ {{ max_heal_attempts }}) · v{{ version }}</footer>
</main>
<script>function _dl(blob,name){var a=document.createElement('a');a.href=URL.createObjectURL(blob);
a.download=name;document.body.appendChild(a);a.click();
setTimeout(function(){URL.revokeObjectURL(a.href);a.remove();},100);}
function downloadHtml(){_dl(new Blob([document.documentElement.outerHTML],
  {type:'text/html;charset=utf-8'}),'report.html');}
async function downloadMd(){var res=await fetch('data:text/markdown;charset=utf-8,'
  +encodeURIComponent(document.getElementById('md-data').textContent||''));
  _dl(new Blob([await res.text()],{type:'text/markdown;charset=utf-8'}),'report.md');}
function downloadJson(){var txt=(document.getElementById('json-data').textContent||'');
  _dl(new Blob([txt],{type:'application/json;charset=utf-8'}),'report.json');}
document.addEventListener('click',function(ev){var b=ev.target.closest('.copy-btn');
  if(!b)return;var pre=document.getElementById(b.getAttribute('data-target'));
  var txt=pre?pre.textContent:'';
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(txt)
    .then(function(){b.textContent='已复制';setTimeout(function(){b.textContent='复制';},1200);})
    .catch(function(){});}else if(window.execCommand){var ta=document.createElement('textarea');
  ta.value=txt;document.body.appendChild(ta);ta.select();try{document.execCommand('copy');}
  catch(e){}ta.remove();b.textContent='已复制';setTimeout(function(){b.textContent='复制';},1200);}});</script>
<script type="application/json" id="json-data">{{ json_payload }}</script>
<pre id="md-data" hidden>{{ md_payload | e }}</pre>
<script>
(function(){
  var tip=document.getElementById('trend-tip');
  if(!tip)return;
  var dots=document.querySelectorAll('.trend-dot');
  if(!dots.length)return;
  var cur=null;
  function move(ev){var x=ev.clientX+12,y=ev.clientY+12;
    var r=tip.getBoundingClientRect();
    if(x+r.width>innerWidth)x=ev.clientX-r.width-12;
    if(y+r.height>innerHeight)y=ev.clientY-r.height-12;
    tip.style.left=x+'px';tip.style.top=y+'px';}
  function show(el){tip.textContent=el.getAttribute('data-tip')||'';
    tip.hidden=false;cur=el;}
  function hide(){tip.hidden=true;cur=null;}
  for(var i=0;i<dots.length;i++)(function(el){
    el.addEventListener('mouseenter',function(ev){show(el);move(ev);});
    el.addEventListener('mousemove',move);
    el.addEventListener('mouseleave',hide);
  })(dots[i]);
  window.addEventListener('resize',hide);
})();
</script>
</body>
</html>
"""


def build_html_report(
    results: list[ResultRecord],
    *,
    report_dir: Optional[str] = None,
    history_rows: Optional[list[dict[str, Any]]] = None,
    trace_urls: Optional[dict[str, str]] = None,
    run_meta: Optional[dict[str, str]] = None,
    basename: str = "report",
    llm: Optional[Any] = None,
    gate: Any = None,
) -> Path:
    """在报告目录下写入单个自包含静态 ``{basename}.html``；返回其路径。

    ``basename`` 为输出文件名主干（不含扩展名），默认 ``report``；与
    :func:`build_report` 的 ``basename`` 保持一致，便于按运行留档不覆盖。

    HTML 完全内联（无外部资源依赖），可离线浏览；输入复用与
    :func:`build_report` 相同的 :class:`ResultRecord` 列表。``history_rows``
    与 :func:`build_report` 同义——提供时渲染「预测分析」区块。

    ``trace_urls`` 提供 Langfuse 等追踪链接（环境追踪层 C1）；
    ``run_meta`` 提供环境元数据（git commit / 运行命令，C2）。

    ``gate`` 注入 LayaGate（决策模型前置层）：非 None 且 AH_LAYA=report 开闸时，
    失败严重度分级经 LayaGate 高置信分流，报告据此渲染「决策模型前置层」区块；
    gate=None（默认/off）→ 完全跳过，字节等价、零网络。
    """
    from jinja2 import Template

    from ah.core.orchestrator import summarize_results

    execution_counts = summarize_results(results)
    # 断言密度（后周期支柱 A）：按每 case 实际评估断言聚合，供 HTML 报告展示。
    _ac = [len(r.assertions or []) for r in results]
    assert_density = {
        "avg_per_case": round(sum(_ac) / len(_ac), 2) if _ac else 0.0,
        "total_asserts": int(sum(_ac)),
        "high": sum(1 for n in _ac if n >= 3),
        "medium": sum(1 for n in _ac if 1 <= n <= 2),
        "none": sum(1 for n in _ac if n == 0),
        "no_assert_cases": [r.name for r in results if not (r.assertions or [])],
    }

    out_dir = Path(report_dir or settings.report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if history_rows is None:
        from ah.predict.history import HistoryStore

        history_rows = HistoryStore(out_dir / "history.jsonl").load()
    prediction: list[dict[str, Any]] = []
    plan_dict: dict[str, Any] = {}
    if history_rows:
        from ah.predict.risk import rank_regression

        plan = rank_regression(history_rows)
        plan_dict = plan.to_dict()
        prediction = [
            {
                "interface": p.interface,
                "risk": p.risk,
                "confidence": p.confidence,
                "failures": p.failures,
                "total": p.total,
                "classes": ", ".join(p.top_failure_classes[:3]) or "—",
            }
            for p in plan.focused
            if p.risk > 0
        ]

    total = len(results)
    passed = sum(1 for r in results if r.result == "PASS")
    failed = sum(1 for record in results if record.result in {"FAIL", "ERROR"})
    run_time = time.strftime("%Y-%m-%d %H:%M:%S")
    json_payload = json.dumps(
        {
            "run_time": run_time,
            "environment": {"base_url": settings.base_url},
            "summary": {"total": total, "passed": passed, "failed": failed},
            "execution_counts": execution_counts,
            "results": [asdict(r) for r in results],
            "semantic_verdicts": _semantic_verdicts_agg(results),
            **({"prediction": prediction} if prediction else {}),
        },
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    md_lines = [
        "# Test Report", "",
        f"- **Run time**: {run_time}",
        f"- **Environment**: base_url={settings.base_url}",
        f"- **Summary**: {passed}/{total} passed, {failed} failed", "",
        f"- **执行口径**: planned={execution_counts['planned']}, "
        f"executed={execution_counts['executed']}, blocked={execution_counts['blocked']}, "
        f"skipped={execution_counts['skipped']}, unverified={execution_counts['unverified']}",
        "## Results", "",
        "| Case | Result | Duration (s) | Failure | Self-heal / 根因 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in results:
        md_lines.append(
            f"| {r.name} | {r.result} | {r.duration:.3f} | "
            f"{(r.failure_msg or '').replace('|', chr(92)+'|').replace(chr(10), ' ')[:120]} | "
            f"{_heal_summary(r).replace('|', chr(92)+'|').replace(chr(10), ' ')} |"
        )
    heal_summaries = [_heal_summary(r) for r in results]
    has_heal = any(heal_summaries)
    has_detail = has_heal or any(r.request or r.response for r in results)
    # 新能力 MD 段：自主演进 + LLM 成本（与 build_report 输出一致）。
    _insights = _project_insights(out_dir)
    _evo, _tel = _insights.get("evolution"), _insights.get("telemetry")
    # HTML 专用指标上下文：LLM 用途分布（按调用数降序）。
    _tel_usage_rows = []
    if _tel and _tel.get("by_task"):
        _tel_usage_rows = sorted(
            (
                {
                    "task": k,
                    "requests": int(v.get("requests", 0)),
                    "avg_ms": float(v.get("avg_latency_ms", 0.0)),
                    "max_ms": float(v.get("max_latency_ms", 0.0)),
                }
                for k, v in _tel["by_task"].items()
            ),
            key=lambda x: x["requests"],
            reverse=True,
        )
    if _evo:
        md_lines += ["", "## 自主演进", ""]
        md_lines += [
            f"- **轮次**: {_evo['rounds']}",
            f"- **规划用例集**: {_evo['planned_suite'] or '—'}",
            f"- **候选 / 采纳 / 拒绝 / 去重跳过**: {_evo['candidates']}/"
            f"{_evo['accepted']}/{_evo['rejected']}/{_evo['dedup_skipped']}",
            f"- **停止原因**: {_evo['stop_reason'] or '—'}",
        ]
    if _tel and _tel["requests"]:
        md_lines += ["", "## LLM 成本", ""]
        md_lines += [
            f"- **调用数**: {_tel['requests']}（成功 {_tel['success']} / "
            f"失败 {_tel['errors']}）",
            f"- **Token**: {_tel.get('total_tokens', 0)}（输入 "
            f"{_tel.get('prompt_tokens', 0)} / 输出 "
            f"{_tel.get('completion_tokens', 0)}）",
            f"- **总成本**: ${_tel['cost']:.4f}",
            f"- **成功率**: {_tel['success_rate'] * 100:.1f}%",
        ]
        if _tel["by_model"]:
            md_lines += [
                "",
                "| 模型 | 调用 | Token | 成本 | 成功 | 失败 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for _m, _b in _tel["by_model"].items():
                md_lines += [
                    f"| {_m} | {_b['requests']} | "
                    f"{_b.get('total_tokens', 0)} | "
                    f"${float(_b['cost']):.4f} | "
                    f"{_b['success']} | {_b['errors']} |"
                ]
    capabilities = _capability_matrix(_evo, _tel, results=results)
    md_lines += _capability_md(capabilities)
    md_payload = "\n".join(md_lines) + "\n"
    from ah import __version__ as _framework_version
    from ah.llm.openai import llm_enabled as _llm_enabled

    healed_count = sum(
        1 for r in results if isinstance((r.llm_notes or {}).get("healed"), dict)
    )
    triage_count = sum(1 for r in results if (r.llm_notes or {}).get("triage"))
    # 失败治理统计：block 终止（等人工）与 observe 无侵入恢复（不回写脚本）。
    gov_blocks = 0
    gov_recovered = 0
    for _r in results:
        _gd = (_r.llm_notes or {}).get("failure_governance") or {}
        if _gd.get("action") == "block":
            gov_blocks += 1
        if isinstance((_r.llm_notes or {}).get("recovered"), dict):
            _rec = _r.llm_notes["recovered"]
            if _rec.get("level") == "observe":
                gov_recovered += 1
    gov_stats = {
        "blocked": gov_blocks,
        "observe_recovered": gov_recovered,
    }
    if failed:
        from collections import Counter


        counts = Counter(
            _failure_category(r) for r in results if r.result != "PASS"
        )
        top = counts.most_common()
        denominator = max(top[0][1], 1)
        fail_buckets = [
            {"label": label, "count": c, "pct": round(100 * c / denominator)}
            for label, c in top
        ]
    else:
        fail_buckets = []

    flaky_count = sum(1 for r in results if getattr(r, "flaky", False))
    pass_rate = round(100 * passed / total) if total else 0


    def _catch(key: str, r: ResultRecord) -> str:
        tri = (r.llm_notes or {}).get("triage")
        if isinstance(tri, dict):
            val = tri.get(key)
            return str(val) if val else ""
        return ""

    # A1 结构化失败明细（用例 | 类别 | 根因 | 建议），供失败分析表渲染。
    # severity 经 severity_classify 前置分级（gate 缺省 None → 确定性规则，
    # 保持既有报告字节等价；注入 LayaGate STAGE_REPORT 可做高置信分流）。
    fail_details = []
    _laya_sev_signals = 0
    for _idx, r in enumerate(results):
        if r.result == "PASS" or not r.failure_msg:
            continue
        _sev, _sig = severity_classify(
            r.failure_msg,
            (r.request or {}).get("method", ""),
            (r.request or {}).get("path", ""),
            gate=gate,
        )
        if _sig:
            _laya_sev_signals += 1
        fail_details.append(
            {
                "name": r.name,
                "detail_id": _case_slug(r.name, _idx),
                "category": _failure_category(r),
                "severity": _sev,
                "root_cause": _catch("root_cause", r)
                or _catch("analysis", r)
                or (r.failure_msg or ""),
                "suggestion": _catch("suggestion", r),
            }
        )
    # 决策模型前置层（Laya）运行摘要：本次真实产生的 triage/tool_risk/stop/
    # 接口风险信号 + 报告严重度分流采纳数；无信号且未开闸 → 不渲染该区块。
    from ah.evidence.laya_gate import laya_enabled as _laya_on

    _lay_stages: list[str] = []
    _lay_sigs = 0
    for _r in results:
        _n = _r.llm_notes or {}
        for _k in ("laya_triage", "laya_tool_risk", "laya_no_more_value"):
            if _n.get(_k):
                _lay_stages.append(_k)
                _lay_sigs += 1
        if getattr(_r, "laya_signal", None):
            _lay_stages.extend(getattr(_r, "laya_signal", {}).keys())
            _lay_sigs += 1
    _laya_insight = None
    if _lay_sigs or _laya_on() or gate is not None:
        _laya_insight = {
            "enabled": _laya_on() or gate is not None,
            "stages": sorted(set(_lay_stages)),
            "signal_types": _lay_sigs,
            "severity_adopted": _laya_sev_signals,
            "points": sorted({s.replace("laya_", "").replace("_", "/")
                              for s in _lay_stages if s}),
        }
    meta = dict(run_meta or {})
    # 未显式提供 trace_urls 时：若真实 tracer 已启用，注入整次运行的 Langfuse
    # 深度链接（指向 get_tracer().run_trace_id），让报告一键直达该 run 的 trace。
    if trace_urls:
        meta.setdefault("trace_urls", trace_urls)
    else:
        run_link = _run_trace_link()
        if run_link:
            meta.setdefault("trace_urls", {"langfuse": run_link})

    # A3 回归对比：把本次运行追加进 report_history.jsonl，并与上一运行对比。
    hist_file = out_dir / "report_history.jsonl"
    prior_runs = _load_run_history(hist_file)
    prev_run = prior_runs[-1] if prior_runs else None
    cur_cases = {r.name: r.result for r in results}
    _mark_flaky_from_history(results, prior_runs)  # 跨 run 抖动 → flaky 打标
    cur_dur = {r.name: r.duration for r in results}
    # ── 本次运行指标（落库 + 对比）──
    result_dist = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    status_dist: dict[int, int] = {}
    heal_count = 0
    for r in results:
        result_dist[r.result] = result_dist.get(r.result, 0) + 1
        st = (r.response or {}).get("status_code")
        if isinstance(st, int):
            status_dist[st] = status_dist.get(st, 0) + 1
        if r.flaky:
            heal_count += 1
    durs = [r.duration for r in results if r.duration]
    from ah.llm.agents import _semantic_verdict as _sv

    unverified_count = sum(1 for r in results if _sv(r).get("flag"))
    avg_dur = (sum(durs) / len(durs)) if durs else 0.0
    p95_dur = _p95(durs) if durs else 0.0
    total_dur = sum(durs)
    env = _env_fingerprint()
    new_failures = (
        [
            name
            for name, res in cur_cases.items()
            if res != "PASS"
            and prev_run
            and prev_run.get("cases", {}).get(name) != "FAIL"
        ]
        if prev_run
        else []
    )
    fixed = (
        [
            name
            for name, res in cur_cases.items()
            if res == "PASS" and prev_run and prev_run.get("cases", {}).get(name) == "FAIL"
        ]
        if prev_run
        else []
    )
    # A3 用例级 diff：状态变化 + 耗时变化（仅列有变化的用例）。
    case_diffs: list[dict[str, Any]] = []
    if prev_run:
        prev_cases = prev_run.get("cases", {})
        prev_dur = prev_run.get("per_case_dur_ms", {})
        for name in dict.fromkeys([*cur_cases, *prev_cases]):
            cto = cur_cases.get(name, "缺失")
            frm = prev_cases.get(name, "缺失")
            if cto == frm:
                continue
            pd = prev_dur.get(name)
            cd = cur_dur.get(name)
            case_diffs.append(
                {
                    "name": name,
                    "from": frm,
                    "to": cto,
                    "prev_dur": round(float(pd), 1) if isinstance(pd, (int, float)) else None,
                    "cur_dur": round(float(cd) * 1000, 1)
                    if isinstance(cd, (int, float))
                    else None,
                }
            )
    # 接口健康画像（HTML 段）：状态分布 + flaky(heal) + 耗时分箱直方图。
    _ih_buckets = [(0, 5), (5, 20), (20, 100), (100, 500), (500, float("inf"))]
    _ih_labels = ["<5ms", "5-20ms", "20-100ms", "100-500ms", ">500ms"]
    _ih_counts = [0 for _ in _ih_buckets]
    for _d in durs:
        _ms = _d * 1000
        for _i, (_lo, _hi) in enumerate(_ih_buckets):
            if _ms >= _lo and _ms < _hi:
                _ih_counts[_i] += 1
                break
    _ih_maxc = max(_ih_counts) or 1
    _ih_rows = []
    for _lab, _c in zip(_ih_labels, _ih_counts):
        _pct = round((_c / max(len(durs), 1)) * 100, 1) if durs else 0.0
        _ih_rows.append({"label": _lab, "count": _c, "pct": _pct, "pct_10": round(_pct / 10)})
    interface_health = {
        "total": total,
        "flaky": heal_count,
        "status_dist": {str(k): v for k, v in sorted(status_dist.items())},
        "avg_ms": round(avg_dur * 1000, 1),
        "p95_ms": round(p95_dur * 1000, 1),
        "p99_ms": round((_percentile(durs, 0.99) or 0.0) * 1000, 1),
        "max_ms": round(max(durs) * 1000, 1) if durs else 0.0,
        "hist_rows": _ih_rows,
        "has_hist": bool(durs),
    }
    # 环境守卫：上一运行与本次环境不同 → 标记不可直接对比。
    env_mismatch = bool(
        prev_run and prev_run.get("env") and prev_run.get("env") != env
    )
    trend = [
        {"run_time": row.get("run_time", ""), "pass_rate": row.get("pass_rate")}
        for row in prior_runs
    ][-8:]
    _append_run_history(
        hist_file,
        {
            "run_time": run_time,
            "total": total,
            "passed": passed,
            "failed": failed,
            "flaky": flaky_count,
            "pass_rate": pass_rate,
            "result_dist": result_dist,
            "status_dist": status_dist,
            "avg_duration_ms": round(avg_dur * 1000, 1),
            "p95_duration_ms": round(p95_dur * 1000, 1),
            "total_duration_ms": round(total_dur * 1000, 1),
            "per_case_dur_ms": {n: round(d * 1000, 1) for n, d in cur_dur.items()},
            "heal_count": heal_count,
            "env": env,
            "cases": cur_cases,
        },
    )

    # A3 指标对比卡：与上一运行逐个维度 diff（通过率 / 性能 / 稳定性 / 状态码）。
    metric_cards: list[dict[str, Any]] = []
    if prev_run:
        pr = prev_run
        avc, avp = round(avg_dur * 1000, 1), pr.get("avg_duration_ms")
        p5c, p5p = round(p95_dur * 1000, 1), pr.get("p95_duration_ms")
        tc, tp = round(total_dur * 1000, 1), pr.get("total_duration_ms")
        metric_cards = [
            {
                "label": "通过率",
                "cur": f"{pass_rate}%",
                "prev": f"{pr.get('pass_rate')}%",
                "delta": (pass_rate - pr.get("pass_rate", pass_rate)),
                "unit": "pp",
                "warn": pass_rate < 90,
            },
            {
                "label": "平均耗时",
                "cur": f"{avc} ms",
                "prev": f"{avp} ms" if isinstance(avp, (int, float)) else "—",
                "delta": (avc - avp) if isinstance(avp, (int, float)) else None,
                "unit": "ms",
                "warn": isinstance(avp, (int, float)) and avp and avc > avp * 1.2,
            },
            {
                "label": "P95 耗时",
                "cur": f"{p5c} ms",
                "prev": f"{p5p} ms" if isinstance(p5p, (int, float)) else "—",
                "delta": (p5c - p5p) if isinstance(p5p, (int, float)) else None,
                "unit": "ms",
                "warn": isinstance(p5p, (int, float)) and p5p and p5c > p5p * 1.2,
            },
            {
                "label": "总耗时",
                "cur": f"{tc} ms",
                "prev": f"{tp} ms" if isinstance(tp, (int, float)) else "—",
                "delta": (tc - tp) if isinstance(tp, (int, float)) else None,
                "unit": "ms",
                "warn": False,
            },
            {
                "label": "自愈/非首过",
                "cur": f"{heal_count}",
                "prev": f"{pr.get('heal_count', 0)}",
                "delta": heal_count - pr.get("heal_count", 0),
                "unit": "例",
                "warn": heal_count > pr.get("heal_count", 0) * 1.5
                and pr.get("heal_count", 0) > 0,
            },
            {
                "label": "状态码",
                "cur": "+".join(f"{k}:{v}" for k, v in sorted(status_dist.items()))
                or "—",
                "prev": "+".join(
                    f"{k}:{v}" for k, v in sorted(pr.get("status_dist", {}).items())
                )
                or "—",
                "delta": None,
                "unit": "",
                "warn": bool(status_dist) and any(
                    k >= 400 for k in status_dist
                ),
                "sub": _status_delta(status_dist, pr.get("status_dist", {})),
            },
        ]


    llm_on = _llm_enabled()
    llm_mode = "on (live)" if llm_on else "off (hermetic)"
    render_results: list[dict[str, Any]] = []
    for idx, (r, heal) in enumerate(zip(results, heal_summaries), start=1):
        tid = _trace_id_from(r.response)
        render_results.append(
            {
                **asdict(r),
                "heal": heal,
                "heal_fields": _heal_fields(r),
                "flaky_kind": "自愈" if r.heal_level else "跨次抖动",
                "badge_cls": {
                    "PASS": "pass", "SKIP": "skip", "BLOCKED": "skip", "UNVERIFIED": "review",
                }.get(r.result, "fail"),
                "detail_id": _case_slug(r.name, idx),
                "diff": _parse_diff(r.failure_msg) if r.failure_msg else None,
                "trace_id": tid,
                "trace_url": _trace_link(tid) if tid else None,
                "semantic": _sv(r),
            },
        )
    hero_extras: list[str] = []
    if healed_count:
        hero_extras.append(f"自愈 {healed_count}")
    if triage_count:
        hero_extras.append(f"LLM 根因 {triage_count}")
    if unverified_count:
        hero_extras.append(f"语义复核 {unverified_count}")
    if total_dur:
        hero_extras.append(f"总耗时 {round(total_dur * 1000, 1)} ms")
    slow_top = sorted(
        (
            {"name": r.name, "dur_ms": round(r.duration * 1000, 1)}
            for r in results
            if r.duration > 0
        ),
        key=lambda x: x["dur_ms"],
        reverse=True,
    )[:5]
    hero_line = " · ".join(hero_extras)
    key_interfaces = _key_interfaces(prior_runs, cur_cases)
    pred_has_history = bool(prediction or key_interfaces or prior_runs)
    from ah.llm.agents import AgentReport

    reporter = AgentReport(llm)  # llm 为 None（LLMOFF/未配置）→ 确定性计数
    analysis = reporter.summarize_records(results)
    # 预测分析走 LLM：风险模型 + 本次结果 → 专业级下一步建议；关闭 LLM 时确定性回退
    llm_advice = reporter.predict_advice(plan_dict, key_interfaces, results)
    env = Template(_HTML_TEMPLATE).render(
        run_time=run_time,
        base_url=settings.base_url,
        target=_actual_targets(results),
        total=total,
        passed=passed,
        failed=failed,
        execution_counts=execution_counts,
        assert_density=assert_density,
        pass_rate=pass_rate,
        hero_line=hero_line,
        healed_count=healed_count,
        triage_count=triage_count,
        fail_buckets=fail_buckets,
        version=_framework_version,
        llm_mode=llm_mode,
        llm_enabled=llm_on,
        # 隐私：不暴露 LLM 提供方 / 模型名，报告不渲染该维度整行。
        llm_provider="",
        llm_cheap="",
        llm_strong="",
        token_store=settings.token_store_path,
        log_dir=settings.log_dir,
        report_dir=settings.report_dir,
        trace_enabled=bool(settings.trace_enabled),
        max_heal_attempts=2,
        has_heal=has_heal,
        has_detail=has_detail,
        results=render_results,
        fail_details=fail_details,
        flaky_count=flaky_count,
        slow_top=slow_top,
        new_failures=new_failures,
        fixed=fixed,
        trend=trend,
        trend_svg=_trend_svg(trend),
        prev_run=prev_run,
        metric_cards=metric_cards,
        evo_insight=_evo,
        tel_insight=_tel,
        laya_insight=_laya_insight,
        tel_usage_rows=_tel_usage_rows,

        interface_health=interface_health,
        gov_stats=gov_stats,
        case_diffs=case_diffs,
        env_mismatch=env_mismatch,
        run_meta=meta,
        trace_urls=meta.get("trace_urls") or {},
        prediction=prediction,
        key_interfaces=key_interfaces,
        pred_has_history=pred_has_history,
        json_payload=json_payload,
        md_payload=md_payload,
        unverified_count=unverified_count,
        analysis=analysis,
        analysis_html=_md_to_html(analysis),
        llm_advice=llm_advice,
        llm_advice_html=_md_to_html(llm_advice if llm_advice else ""),
    )
    html_path = out_dir / f"{basename}.html"
    html_path.write_text(env, encoding="utf-8")
    log.info("html report written: %s", html_path)
    return html_path

class ReportWriter:
    """面向对象的报告写入器（镜像 reporting 层约定）。"""

    def __init__(self, report_dir: Optional[str] = None) -> None:
        self.report_dir = report_dir or settings.report_dir

    def write(
        self,
        results: list[ResultRecord],
        trace_urls: Optional[dict[str, str]] = None,
    ) -> Path:
        return build_report(
            results, trace_urls=trace_urls, report_dir=self.report_dir
        )
    def write_html(
        self,
        results: list[ResultRecord],
        trace_urls: Optional[dict[str, str]] = None,
        run_meta: Optional[dict[str, str]] = None,
    ) -> Path:
        """写入单文件静态 HTML 报告（同 ``write`` 的结果数据）。

        ``trace_urls`` 提供追踪链接（C1），``run_meta`` 提供环境元数据（C2）。
        """
        return build_html_report(
            results,
            report_dir=self.report_dir,
            trace_urls=trace_urls,
            run_meta=run_meta,
        )
