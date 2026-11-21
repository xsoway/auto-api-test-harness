#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/15 15:12
# @Filename : health.py
# @Author   : Alan_Hsu
"""全用例健康度评分（后周期支柱 D：自动化健康度量化）。

把 :func:`ah.evolution.heuristic_quality` 的 0-1 归一化评分升级为 **0-100 质量分**，
并叠加文章主张的：

- **断言密度**：单条用例的强断言数（status + jsonpath/schema/equals/contains 计数）。
- **无效断言清单**：无断言 / 仅状态码（弱） / jsonpath 指向契约中已废弃字段 →
  判定规则显式化，输出清洗清单。
- **一票否决**：无有效断言 / 覆盖接口已下线 / 连续 30 天失败无人维护 / 严重逻辑错误
  （script_bug 聚簇）→ 任一项直接判不合格（score=0 + veto_reason）。

评分语义：三部分加权（断言有效性 50 / 参数覆盖 30 / 维护成本 20），与
:func:`heuristic_quality` 保持一致，只是把权重放大到 0-100 便于「70 分门禁」直观应用。

Hermetic：评分与恶意检查全部确定性，LLM 关闭不依赖外部；LLM 分支（可选）交给
调用方注入，本模块只做确定性基座。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

#: 门禁默认阈值（文章「新增用例 70 分质量门禁」）。
DEFAULT_MIN_QUALITY = 70.0


#: 一票否决：连续失败无人维护的天数阈值（文章「连续 30 天执行失败无人维护」）。
VETO_STALE_DAYS = 30


#: 状态码断言键。
_STATUS_KEYS = ("status", "status_code")


#: 响应体断言键（强断言）。
_BODY_KEYS = ("jsonpath", "schema", "equals", "contains", "body")



def _has_status_assert(assert_: dict[str, Any]) -> bool:
    return any(k in assert_ for k in _STATUS_KEYS)


def _has_body_assert(assert_: dict[str, Any]) -> bool:
    return any(k in assert_ for k in _BODY_KEYS)


def _assert_count(assert_: dict[str, Any]) -> int:
    """统计强断言条数：status 计 1，jsonpath 的每个路径计 1，其余键计 1。"""
    if not assert_:
        return 0
    count = 0
    if _has_status_assert(assert_):
        count += 1
    jp = assert_.get("jsonpath")
    if isinstance(jp, dict):
        count += len(jp)
    for key in ("schema", "equals", "contains", "body"):
        if key in assert_:
            count += 1
    return count


def assert_density(case: Any) -> int:
    """单条用例的断言密度（强断言条数）。"""
    return _assert_count(getattr(case, "assert_", None) or {})


def list_asserts(case: Any) -> list[dict[str, str]]:
    """输出单条用例的断言清单（供报告/无效断言判定复用）。"""
    assert_ = getattr(case, "assert_", None) or {}
    out: list[dict[str, str]] = []
    if _has_status_assert(assert_):
        out.append({"kind": "status", "target": "status_code"})
    jp = assert_.get("jsonpath")
    if isinstance(jp, dict):
        for path in sorted(jp):
            out.append({"kind": "jsonpath", "target": str(path)})
    for key in ("schema", "equals", "contains", "body"):
        if key in assert_:
            out.append({"kind": key, "target": key})
    return out


# ───────────────────────── 0-100 质量分 ─────────────────────────


def quality_score(case: Any) -> float:
    """确定性健康度评分（0-100）。语义与 heuristic_quality 一致，权重放大。

    - 断言有效性（50 分）：无断言 0；仅 status 弱断言 25；含 body 强断言 50。
    - 参数覆盖（30 分）：有 params/json/data 30；模板路径 15；否则 0。
    - 维护成本（20 分）：命名有意义且不与路径重复 20；否则 10。
    """
    score = 0.0
    req = getattr(case, "request", {}) or {}
    name = str(getattr(case, "name", "") or "")
    assert_ = getattr(case, "assert_", None) or {}
    if assert_:
        if _has_status_assert(assert_):
            score += 25.0
        if _has_body_assert(assert_):
            score += 25.0
        else:
            score += 10.0  # 仅有状态码断言：弱但非恒真
    if req.get("params") or req.get("json") or req.get("data"):
        score += 30.0
    elif "{" in str(req.get("path", "")):
        score += 15.0
    if name and name != str(req.get("path", "")):
        score += 20.0
    else:
        score += 10.0
    return round(min(100.0, max(0.0, score)), 1)


@dataclass
class CaseHealth:
    """单条用例的健康度评定（含一票否决与无效断言清单）。"""

    case: Any
    score: float
    approved: bool
    veto: Optional[str] = None          # 一票否决原因；None = 未否决
    assert_count: int = 0               # 断言密度
    invalid_asserts: list[dict[str, str]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)  # 低分/清洗建议


# ───────────────────────── 无效断言判定 ─────────────────────────


def _invalid_asserts(case: Any, dead_fields: Optional[set[str]] = None) -> list[dict[str, str]]:
    """按规则识别无效断言：恒真断言 / 指向已废弃字段。

    - 无断言：整条无可校验点 → 记一条 invalid_assert（恒真风险最高）。
    - 仅 status 断言：弱断言，无法拦截数据一致性 → 记一条清洗建议。
    - jsonpath 指向 ``dead_fields``（契约已废弃）→ 记 invalid_assert。
    """
    out: list[dict[str, str]] = []
    assert_ = getattr(case, "assert_", None) or {}
    if not assert_:
        return [{"kind": "no_assert", "target": "*", "detail": "无断言，恒真风险最高"}]
    if not _has_body_assert(assert_):
        out.append({
            "kind": "weak_assert",
            "target": "status",
            "detail": "仅状态码断言，未覆盖数据一致性/业务规则",
        })
    if dead_fields:
        jp = assert_.get("jsonpath")
        if isinstance(jp, dict):
            for path in jp:
                leaf = str(path).split(".")[-1].strip("[]'\"")
                if leaf in dead_fields:
                    out.append({
                        "kind": "dead_field",
                        "target": str(path),
                        "detail": f"字段 {leaf} 在契约中已废弃/不存在",
                    })
    return out


# ───────────────────────── 低价值判定输入 ─────────────────────────


@dataclass
class HealthContext:
    """健康度评定的可选外部上下文（全部可缺省，保证 Hermetic）。

    - ``dead_fields``：契约中已废弃字段集合（来自 :mod:`ah.coverage.gap` 或契约挖掘）。
    - ``retired_paths``：已下线接口集合（method,path）。
    - ``stale_names``：连续失败无人维护的用例名集合（来自 history 时间窗）。
    - ``broken_names``：script_bug 聚簇的用例名集合（来自 failure.cluster）。
    """

    dead_fields: Optional[set[str]] = None
    retired_paths: Optional[set[tuple[str, str]]] = None
    stale_names: Optional[set[str]] = None
    broken_names: Optional[set[str]] = None


def assess_case(
    case: Any,
    ctx: Optional[HealthContext] = None,
    *,
    min_quality: float = DEFAULT_MIN_QUALITY,
) -> CaseHealth:
    """对单条用例做健康度评定（评分 + 一票否决 + 无效断言清单）。

    **一票否决规则**（文章铁律）：
    1. 无有效断言（无任何断言）→ veto=``no_assert``
    2. 覆盖接口已下线（method,path ∈ retired_paths）→ veto=``retired_interface``
    3. 连续 30 天失败无人维护（name ∈ stale_names）→ veto=``stale_unmaintained``
    4. 严重逻辑错误无法执行（name ∈ broken_names）→ veto=``broken_script``

    任一命中 → score=0、approved=False、veto 置原因。否则按 min_quality 判定通过。
    """
    ctx = ctx or HealthContext()
    name = str(getattr(case, "name", "") or "")
    req = getattr(case, "request", {}) or {}
    method = str((req or {}).get("method", "GET")).upper()
    path = str(req.get("path", ""))
    assert_ = getattr(case, "assert_", None) or {}

    # 一票否决优先：命中直接判不合格，不继续打分。
    if not assert_:
        return CaseHealth(
            case, 0.0, False, veto="no_assert", assert_count=0,
            invalid_asserts=[{"kind": "no_assert", "target": "*", "detail": "无断言"}],
            reasons=["一票否决：无有效断言"],
        )
    if ctx.retired_paths and (method, path) in ctx.retired_paths:
        return CaseHealth(
            case, 0.0, False, veto="retired_interface", assert_count=_assert_count(assert_),
            reasons=[f"一票否决：接口已下线 {method} {path}"],
        )
    if ctx.stale_names and name in ctx.stale_names:
        return CaseHealth(
            case, 0.0, False, veto="stale_unmaintained", assert_count=_assert_count(assert_),
            reasons=[f"一票否决：连续失败无人维护 {name}"],
        )
    if ctx.broken_names and name in ctx.broken_names:
        return CaseHealth(
            case, 0.0, False, veto="broken_script", assert_count=_assert_count(assert_),
            reasons=[f"一票否决：脚本严重逻辑错误 {name}"],
        )

    score = quality_score(case)
    invalid = _invalid_asserts(case, ctx.dead_fields)
    reasons: list[str] = []
    for inv in invalid:
        reasons.append(f"无效断言: {inv['detail']}")
    approved = score >= min_quality
    return CaseHealth(
        case=case,
        score=score,
        approved=approved,
        veto=None,
        assert_count=_assert_count(assert_),
        invalid_asserts=invalid,
        reasons=reasons,
    )


def assess_suite(
    cases: Sequence[Any],
    ctx: Optional[HealthContext] = None,
    *,
    min_quality: float = DEFAULT_MIN_QUALITY,
) -> list[CaseHealth]:
    """批量评定整个用例库。"""
    return [assess_case(c, ctx, min_quality=min_quality) for c in cases]


# ───────────────────────── 聚合报告 ─────────────────────────


@dataclass
class HealthReport:
    """全用例库健康度聚合结果。"""

    total: int = 0
    approved: int = 0
    vetoed: int = 0
    avg_score: float = 0.0
    avg_assert_density: float = 0.0
    by_veto: dict[str, int] = field(default_factory=dict)
    items: list[CaseHealth] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "approved": self.approved,
            "vetoed": self.vetoed,
            "avg_score": self.avg_score,
            "avg_assert_density": self.avg_assert_density,
            "veto_breakdown": dict(self.by_veto),
            "items": [
                {
                    "name": getattr(i.case, "name", ""),
                    "score": i.score,
                    "approved": i.approved,
                    "veto": i.veto,
                    "assert_count": i.assert_count,
                    "reasons": i.reasons,
                }
                for i in self.items
            ],
        }


def aggregate_health(items: Sequence[CaseHealth]) -> HealthReport:
    """把逐条评定聚合为套件健康度报告。"""
    if not items:
        return HealthReport()
    total = len(items)
    approved = sum(1 for i in items if i.approved)
    vetoed = sum(1 for i in items if i.veto)
    avg_score = round(sum(i.score for i in items) / total, 1)
    avg_density = round(sum(i.assert_count for i in items) / total, 2)
    by_veto: dict[str, int] = {}
    for i in items:
        if i.veto:
            by_veto[i.veto] = by_veto.get(i.veto, 0) + 1
    return HealthReport(
        total=total,
        approved=approved,
        vetoed=vetoed,
        avg_score=avg_score,
        avg_assert_density=avg_density,
        by_veto=by_veto,
        items=list(items),
    )
