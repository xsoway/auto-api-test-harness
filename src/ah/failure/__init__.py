#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15
# @Filename : __init__.py
# @Author   : Alan_Hsu
"""失败治理层：失败分类与动作映射、簇群指纹状态机、知识沉淀、临时恢复。

对应《别让自愈把红灯洗绿》四步模型：
  1. 分类（triage）→ 动作收敛 heal/observe/block
  2. 临时恢复（observe）：无侵入恢复，不回写脚本
  3. 失败簇群（cluster）：指纹聚合 + 状态机
  4. 知识沉淀（knowledge）：resolved/known_issue 检索闭环

全部保持 hermetic（零网络、可离线程运行）。
"""

from ah.failure.cluster import (
    ACTIVE,
    DEFAULT_FLAKY_THRESHOLD,
    DEFAULT_RECOVER_PASSES,
    DEFAULT_WATCH_FAIL_THRESHOLD,
    FLAKY_WATCH,
    QUARANTINED,
    RECOVERED,
    RESOLVED,
    ClusterStore,
    load_clusters,
    normalize_error_sig,
)
from ah.failure.knowledge import (
    DRAFT,
    PUBLISHED,
    TYPE_KNOWN_ISSUE,
    TYPE_RESOLVED,
    KnowledgeStore,
    load_knowledge,
)
from ah.failure.observe import (
    OBSERVE_REAUTH,
    OBSERVE_RECONNECT,
    OBSERVE_RETRY,
    AgentObserve,
    observe_retry_original,
)
from ah.failure.triage import (
    ACTION_BLOCK,
    ACTION_HEAL,
    ACTION_OBSERVE,
    ACTIONS,
    CATEGORY_DESC,
    DEFAULT_ACTION,
    ENVIRONMENT,
    FAILURE_CATEGORIES,
    FLAKY,
    KNOWN_ISSUE,
    PRODUCT_DEFECT,
    SCRIPT_BUG,
    UNKNOWN,
    action_for,
    classify,
    is_valid_category,
    normalize_category,
)

__all__ = [
    # triage
    "SCRIPT_BUG", "PRODUCT_DEFECT", "ENVIRONMENT", "FLAKY",
    "KNOWN_ISSUE", "UNKNOWN", "FAILURE_CATEGORIES", "CATEGORY_DESC",
    "DEFAULT_ACTION", "ACTION_HEAL", "ACTION_OBSERVE", "ACTION_BLOCK",
    "ACTIONS", "is_valid_category", "normalize_category", "action_for",
    "classify",
    # observe
    "OBSERVE_RETRY", "OBSERVE_REAUTH", "OBSERVE_RECONNECT",
    "AgentObserve", "observe_retry_original",
    # cluster
    "ACTIVE", "FLAKY_WATCH", "QUARANTINED", "RECOVERED", "RESOLVED",
    "DEFAULT_FLAKY_THRESHOLD", "DEFAULT_WATCH_FAIL_THRESHOLD",
    "DEFAULT_RECOVER_PASSES", "ClusterStore", "load_clusters",
    "normalize_error_sig",
    # knowledge
    "DRAFT", "PUBLISHED", "TYPE_RESOLVED", "TYPE_KNOWN_ISSUE",
    "KnowledgeStore", "load_knowledge",
]
