#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/15 15:30
# @Filename : audit.py
# @Author   : Alan_Hsu
"""已入库用例复盘清洗（后周期支柱 A 的"AI 自我清洗迭代"落地）。

对应文章关键机制：**AI 推荐的断言/用例并不完全可靠**——批量解析海量代码会有
上下文偏差，大模型存在幻觉。如果只依赖 AI 产出而不做二次校验，这些不准确的
断言会留存成新隐患，"用新问题解决旧问题"。

本模块提供对**已入库用例库（含 AI 生成）**的复盘清洗：

1. **确定性复盘**（Hermetic，默认）：对全库逐条重跑健康度评定，输出低分/无效
   断言/一票否决清单与**优化/删除建议**——不依赖 LLM，稳定可复现。
2. **LLM 复盘**（可选分支）：注入 LLM 对低分用例给出精细化断言优化方案；
   LLM 关闭/输出无效时回退确定性建议，绝不触碰网络。

输出为结构化 ``AuditItem``（含判定、证据、建议动作），可落盘 JSON 供审查。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from ah.quality.health import (
    CaseHealth,
    HealthContext,
    assess_suite,
)

#: 复盘建议动作。
ACTION_OPTIMIZE = "optimize"   # 补/改断言
ACTION_CLEAN = "clean"         # 删除无效断言
ACTION_DELETE = "delete"       # 一票否决/低价值：建议下线
ACTION_OK = "ok"               # 健康，无需处理


@dataclass
class AuditItem:
    """一条用例的复盘结论。"""

    name: str
    health: CaseHealth
    action: str
    suggestion: str = ""
    llm_advisory: Optional[str] = None  # LLM 给出的优化方案（若有）


@dataclass
class AuditReport:
    """一次复盘清洗的聚合结果。"""

    total: int = 0
    to_optimize: int = 0
    to_clean: int = 0
    to_delete: int = 0
    healthy: int = 0
    items: list[AuditItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "to_optimize": self.to_optimize,
            "to_clean": self.to_clean,
            "to_delete": self.to_delete,
            "healthy": self.healthy,
            "items": [
                {
                    "name": it.name,
                    "action": it.action,
                    "score": it.health.score,
                    "veto": it.health.veto,
                    "assert_count": it.health.assert_count,
                    "suggestion": it.suggestion,
                    "llm_advisory": it.llm_advisory,
                }
                for it in self.items
            ],
        }


def _deterministic_action(h: CaseHealth) -> tuple[str, str]:
    """根据健康度评定给出确定性建议动作（不依赖 LLM）。"""
    if h.veto:
        return ACTION_DELETE, f"一票否决({h.veto}): {h.reasons[0] if h.reasons else '不合格'}"
    if not h.invalid_asserts and h.approved and h.assert_count >= 2:
        return ACTION_OK, "健康"
    if h.invalid_asserts:
        return (
            ACTION_CLEAN,
            "清理无效断言: " + "; ".join(inv["detail"] for inv in h.invalid_asserts),
        )
    if h.assert_count <= 1:
        return ACTION_OPTIMIZE, "断言过浅，建议补充业务/数据一致性断言"
    return ACTION_OK, "健康"


def _llm_optimize(
    llm: Any,
    case: Any,
    health: CaseHealth,
) -> Optional[str]:
    """调用 LLM 给出断言优化方案；失败/关闭返回 None（回退确定性）。

    Hermetic 铁律：仅当调用方显式传入 llm 且其可用时才调用；这里只做薄封装，
    绝不在此模块内创建网络连接。
    """
    judge = getattr(llm, "judge", None)
    if judge is None:
        return None
    try:
        req = getattr(case, "request", {}) or {}
        resp = judge(
            "quality_audit_optimize",
            json.dumps({
                "case_name": str(getattr(case, "name", "")),
                "method": str(req.get("method", "GET")),
                "path": str(req.get("path", "")),
                "assert": getattr(case, "assert_", {}) or {},
                "problems": health.reasons,
                "instruction": (
                    "输出该用例的精细化断言优化方案（覆盖数据一致性/业务规则/状态流转），"
                    "仅输出建议断言 YAML，禁止虚构字段或新接口。"
                ),
            }, ensure_ascii=False),
        )
        if isinstance(resp, dict) and resp.get("advice"):
            return str(resp["advice"])
    except Exception:
        return None
    return None


def run_audit(
    cases: Sequence[Any],
    ctx: Optional[HealthContext] = None,
    *,
    llm: Any = None,
    min_quality: float = 70.0,
) -> AuditReport:
    """对已入库用例库做复盘清洗。

    - 确定性：逐条 ``assess_suite``（评分 + 一票否决 + 无效断言清单），映射为建议动作。
    - LLM（可选）：仅对 ``assess`` 判定需优化/清洗的用例，注入 ``llm.judge`` 生成
      优化方案；无 LLM / 失败回退确定性建议。
    返回 :class:`AuditReport`（含全部条目与汇总计数）。
    """
    healths = assess_suite(cases, ctx, min_quality=min_quality)
    items: list[AuditItem] = []
    to_opt = to_clean = to_del = healthy = 0
    for h in healths:
        action, suggestion = _deterministic_action(h)
        advisory: Optional[str] = None
        if action in (ACTION_OPTIMIZE, ACTION_CLEAN) and llm is not None:
            advisory = _llm_optimize(llm, h.case, h)
            if advisory:
                suggestion = f"{suggestion} | LLM 建议: {advisory}"
        if action == ACTION_OPTIMIZE:
            to_opt += 1
        elif action == ACTION_CLEAN:
            to_clean += 1
        elif action == ACTION_DELETE:
            to_del += 1
        else:
            healthy += 1
        items.append(
            AuditItem(
                name=str(getattr(h.case, "name", "")) or "unknown",
                health=h,
                action=action,
                suggestion=suggestion,
                llm_advisory=advisory,
            )
        )
    return AuditReport(
        total=len(items),
        to_optimize=to_opt,
        to_clean=to_clean,
        to_delete=to_del,
        healthy=healthy,
        items=items,
    )
