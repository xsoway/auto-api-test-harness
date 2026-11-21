#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15
# @Filename : triage.py
# @Author   : Alan_Hsu
"""失败分类与动作映射（失败治理层）。

对应《别让自愈把红灯洗绿》的失败分类 ↔ 动作收敛模型：

- 失败分类（6 类）：
  - ``script_bug``   脚本本身（定位器/断言/逻辑）错误 → heal
  - ``product_defect`` 被测系统缺陷 → block（终止，等人工）
  - ``environment``  网络/5xx/崩溃等环境问题 → observe
  - ``flaky``        偶发抖动，根因未明 → observe
  - ``known_issue``  经确认的已知问题 → observe/block（按严重性）
  - ``unknown``      信息不足 → observe

- 动作（3 种）：
  - ``heal``   高置信脚本问题，生成修复候选，验证通过后应用（走现有 AgentHeal 闭环）
  - ``observe`` 悬而未决，隔离观察，不立即改脚本
  - ``block``  产品缺陷/阻塞性危急问题，终止执行，等人工介入

分类优先走确定性信号（状态码/异常类型），LLM 交给上层 AgentTriage 做语义补充；
本模块只定义分类模型与动作解析，不依赖 LLM，保证 hermetic。
"""

from __future__ import annotations

from typing import Any, Optional

# ── 失败分类 ────────────────────────────────────────────────────────────────
SCRIPT_BUG = "script_bug"
PRODUCT_DEFECT = "product_defect"
ENVIRONMENT = "environment"
FLAKY = "flaky"
KNOWN_ISSUE = "known_issue"
UNKNOWN = "unknown"

FAILURE_CATEGORIES: tuple[str, ...] = (
    SCRIPT_BUG,
    PRODUCT_DEFECT,
    ENVIRONMENT,
    FLAKY,
    KNOWN_ISSUE,
    UNKNOWN,
)

# 分类 → 中文说明（供 prompt / 报告 / 知识检索复用）。
CATEGORY_DESC: dict[str, str] = {
    SCRIPT_BUG: "脚本本身的定位器/操作/断言/逻辑编写错误",
    PRODUCT_DEFECT: "被测系统的缺陷（断言不符、逻辑错误），重试无效",
    ENVIRONMENT: "网络/5xx/超时/浏览器崩溃等环境问题",
    FLAKY: "偶发抖动，暂未定位稳定根因",
    KNOWN_ISSUE: "经确认过的已知问题或已知限制",
    UNKNOWN: "暂时无法判断的问题",
}

# 分类 → 默认动作（known_issue 默认 observe，按严重性可人工升为 block）。
DEFAULT_ACTION: dict[str, str] = {
    SCRIPT_BUG: "heal",
    PRODUCT_DEFECT: "block",
    ENVIRONMENT: "observe",
    FLAKY: "observe",
    KNOWN_ISSUE: "observe",
    UNKNOWN: "observe",
}

# 动作三态（对应文章收敛成三种动作）。
ACTION_HEAL = "heal"
ACTION_OBSERVE = "observe"
ACTION_BLOCK = "block"
ACTIONS: tuple[str, ...] = (ACTION_HEAL, ACTION_OBSERVE, ACTION_BLOCK)


def is_valid_category(cat: str) -> bool:
    """分类是否合法。"""
    return cat in FAILURE_CATEGORIES


def normalize_category(cat: Any) -> str:
    """把任意值规整为合法分类；非法回退 ``unknown``（安全默认 observe）。"""
    if isinstance(cat, str):
        c = cat.strip().lower()
        # 兼容历史值映射到新分类。
        legacy = {
            "assertion": SCRIPT_BUG,
            "extraction": SCRIPT_BUG,
            "server": ENVIRONMENT,
            "network": ENVIRONMENT,
            "config": ENVIRONMENT,
            "schema": PRODUCT_DEFECT,
            "timeout": ENVIRONMENT,
        }
        if c in legacy:
            return legacy[c]
        if c in FAILURE_CATEGORIES:
            return c
    return UNKNOWN


def action_for(category: str, *, severe: bool = False) -> str:
    """分类 → 默认动作；``known_issue`` 在严重时升为 block。"""
    cat = normalize_category(category)
    action = DEFAULT_ACTION.get(cat, ACTION_OBSERVE)
    if cat == KNOWN_ISSUE and severe:
        return ACTION_BLOCK
    return action


def classify(
    *,
    status_code: Optional[int] = None,
    error: Optional[BaseException] = None,
    error_text: str = "",
    category: Optional[str] = None,
    severe: bool = False,
) -> dict[str, Any]:
    """根据（可选的 LLM/知识给出的）分类或确定性信号解析出分类 + 动作 + 归因。

    - 显式 ``category`` 合法 → 直接采用（来源可为 AgentTriage / 知识库命中）。
    - 否则从确定性信号推断（status_code/异常类型）→ 仍是 heuristic 试探，
      结果标记 ``source="rule"``。
    - 无法确定 → ``unknown`` + ``observe``。

    返回 ``{"category", "action", "source", "detail"}``。绝不抛异常。
    """
    if is_valid_category(category or ""):
        return {
            "category": category,
            "action": action_for(category, severe=severe),
            "source": "explicit",
            "detail": CATEGORY_DESC.get(category, ""),
        }
    # 确定性信号推断（rule-based，无 LLM）。
    inferred: Optional[str] = None
    if status_code is not None and status_code >= 500:
        inferred = ENVIRONMENT  # 5xx/网关层，大概率环境侧
    # error_text 为空但传了 error 异常 → 用其文本推断（governor 常只传 error）。
    text = error_text or (str(error) if error is not None else "")
    if inferred is None and text:
        low = text.lower()
        if any(k in low for k in ("timeout", "timed out", "connection", "refused",
                                  "network", "dns", "ssl", "resolve")):
            inferred = ENVIRONMENT
    if inferred is not None:
        return {
            "category": inferred,
            "action": action_for(inferred),
            "source": "rule",
            "detail": CATEGORY_DESC.get(inferred, ""),
        }

    return {
        "category": UNKNOWN,
        "action": action_for(UNKNOWN),
        "source": "unknown",
        "detail": CATEGORY_DESC[UNKNOWN],
    }
