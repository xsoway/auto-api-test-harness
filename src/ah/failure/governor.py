#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15
# @Filename : governor.py
# @Author   : Alan_Hsu
"""失败治理编排器：把 triage + observe + cluster + knowledge 串成一条决策链。

这是文章2 模型在框架内的总装层。一次失败进入治理链：

1. 归一化分类（优先 LLM/知识的语义分类，回退确定性规则）。
2. 知识库检索命中 → 覆盖分类/动作（known_issue 直接改分类）。
3. 依动作分支：
   - ``heal``    → 交给上层 AgentHeal（本模块不生成修复）
   - ``observe`` → 尝试非侵入临时恢复（AgentObserve），成功只重跑原步骤不回写
   - ``block``   → 终止，不尝试自愈
4. 把这次结果记入失败簇群状态机（驱动 flaky_watch/quarantined/recovered）。

Governor 是纯编排，不含 LLM；所有子依赖可注入（hermetic 测试友好）。
"""

from __future__ import annotations

from typing import Any, Optional

from ah.failure.cluster import ClusterStore
from ah.failure.knowledge import KnowledgeStore
from ah.failure.observe import AgentObserve
from ah.failure.triage import (
    ACTION_BLOCK,
    ACTION_HEAL,
    UNKNOWN,
    action_for,
    classify,
    normalize_category,
)
from ah.utils.logging import get_logger

log = get_logger("failure_governor")


class FailureGovernor:
    """失败治理链的编排容器（可整体注入 run_single，缺省组件自动回退空行为）。

    Args:
        clusters: 失败簇群存储；None → 纯内存空簇（不落盘）。
        knowledge: 失败知识库；None → 空知识库。
        observe: AgentObserve；None → 不尝试临时恢复（observe 退化为纯记录）。
        triage_category_from: 可选函数，从运行结果里取语义分类
            （如 ``(result) -> category``），供 governor 在无 hooks 时也有依据。
    """

    def __init__(
        self,
        *,
        clusters: Optional[ClusterStore] = None,
        knowledge: Optional[KnowledgeStore] = None,
        observe: Optional[AgentObserve] = None,
    ) -> None:
        self.clusters = clusters or ClusterStore(path=None)
        self.knowledge = knowledge or KnowledgeStore(path=None)
        self.observe = observe

    def decide(
        self,
        *,
        method: str,
        path: str,
        case_name: str,
        error: Any,
        status_code: Optional[int] = None,
        explicit_category: Optional[str] = None,
        severe: bool = False,
        error_text: str = "",
    ) -> dict[str, Any]:
        """对一次失败给出治理决策（分类 + 动作 + 知识命中 + 簇群更新）。

        返回可埋点的结果字典；绝不抛异常。
        """
        # 1. 归一化分类：显式（LLM/Laya）优先 → 否则走确定性规则 → 知识覆盖。
        if explicit_category:
            category = normalize_category(explicit_category)
            source = "explicit"
        else:
            rule = classify(
                status_code=status_code, error=error, error_text=error_text
            )
            category = rule["category"]
            source = "rule"

        # 2. 知识库检索：命中 known_issue → 直接改分类/动作。
        kapp = self.knowledge.apply(
            method=method,
            path=path,
            failure_class=category,
            error=error,
            default_category=category or UNKNOWN,
            default_action=action_for(category),
            severe=severe,
        )
        if kapp.get("hit"):
            category = kapp["category"]
            source = "knowledge"
        action = kapp["action"] if kapp.get("hit") else action_for(category, severe=severe)

        # 3. 记入失败簇群（驱动状态机）。
        cluster = self.clusters.record(
            method=method,
            path=path,
            failure_class=category,
            error=error,
            passed=False,
            case_name=case_name,
        )

        return {
            "category": category,
            "action": action,
            "source": source,
            "severe": severe,
            "cluster_state": cluster.get("state"),
            "cluster_key": cluster.get("key"),
            "knowledge": {
                "hit": bool(kapp.get("hit")),
                "kind": kapp.get("kind"),
                "knowledge_id": kapp.get("knowledge_id"),
                "evidence": kapp.get("evidence", []),
            },
            "signal_error": error_text or str(error),
        }

    def record_pass(
        self,
        *,
        method: str,
        path: str,
        case_name: str,
        failure_class: str = "",
        error: Any = None,
        cluster_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """记录一次通过（用于 observe/heal 恢复后的簇群回移）。"""
        cluster = self.clusters.record(
            method=method,
            path=path,
            failure_class=failure_class or UNKNOWN,
            error=error,
            passed=True,
            case_name=case_name,
        )
        return {"cluster_state": cluster.get("state"), "cluster_key": cluster.get("key")}

    # ── observe 分支 ─────────────────────────────────────────────────────
    def observe_recover(
        self,
        *,
        case: Any,
        resp: Any,
        error: BaseException,
        ctx: Any,
        attempt: int,
    ) -> dict[str, Any]:
        """执行 observe 动作的临时恢复；返回是否恢复 + 模式。

        未配置 observe（AgentObserve 为 None）→ 直接返回未恢复
        （observe 仅记录，不改脚本）。
        """
        if self.observe is None:
            return {"recovered": False, "tried": [], "reason": "no-observer"}
        try:
            return self.observe.recover(case, resp, error, ctx, attempt)
        except Exception as exc:  # noqa: BLE001 — 恢复失败不阻断
            log.warning("observe 恢复异常: %s", exc)
            return {"recovered": False, "tried": [], "reason": "observer-error"}

    @staticmethod
    def should_heal(action: str) -> bool:
        """只有 heal 动作才走 AgentHeal（block 不治、observe 不写脚本）。"""
        return action == ACTION_HEAL

    @staticmethod
    def should_block(action: str) -> bool:
        return action == ACTION_BLOCK
