#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15
# @Filename : test_failure_triage.py
# @Author   : Alan_Hsu
"""失败治理：triage 6 分类 + 动作映射 hermetic 单测。

对应《让 Agent 学会判断》分类→动作映射表与《别让自愈把红灯洗绿》失败治理。
覆盖：
- 6 分类合法性 + 归一化（含历史值兼容映射）。
- 分类 → 默认动作映射（known_issue 严重时升 block）。
- classify 确定性推断（5xx → environment、超时/网络关键词 → environment、
  显式分类优先、未知 → unknown/observe）。

全部纯本地、零网络、零真模型。
"""

from __future__ import annotations

from ah.failure.triage import (
    ACTION_BLOCK,
    ACTION_HEAL,
    ACTION_OBSERVE,
    CATEGORY_DESC,
    DEFAULT_ACTION,
    ENVIRONMENT,
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

# ── 分类合法性 ───────────────────────────────────────────────────────────

def test_six_categories_are_valid():
    """6 个分类全部合法；旧 4 分类（transient/defect/config/other）已废弃。"""
    for cat in (SCRIPT_BUG, PRODUCT_DEFECT, ENVIRONMENT,
                FLAKY, KNOWN_ISSUE, UNKNOWN):
        assert is_valid_category(cat)
    # 非 6 分类皆非法
    assert not is_valid_category("transient")
    assert not is_valid_category("defect")
    assert not is_valid_category("")


def test_categories_have_descriptions():
    """每个分类都有中文说明（供 prompt / 报告 / 知识检索复用）。"""
    for cat in (SCRIPT_BUG, PRODUCT_DEFECT, ENVIRONMENT, FLAKY,
                KNOWN_ISSUE, UNKNOWN):
        assert CATEGORY_DESC[cat]

def test_normalize_accepts_new_categories():
    """新分类原样归一化。"""
    assert normalize_category(SCRIPT_BUG) == SCRIPT_BUG
    assert normalize_category("  product_defect ") == PRODUCT_DEFECT  # 去空格/大小写


def test_normalize_legacy_mapping():
    """历史值映射到新分类：assertion/extraction→script_bug，
    server/network/config/timeout→environment，schema→product_defect。"""
    assert normalize_category("assertion") == SCRIPT_BUG
    assert normalize_category("extraction") == SCRIPT_BUG
    assert normalize_category("server") == ENVIRONMENT
    assert normalize_category("network") == ENVIRONMENT
    assert normalize_category("config") == ENVIRONMENT
    assert normalize_category("timeout") == ENVIRONMENT
    assert normalize_category("schema") == PRODUCT_DEFECT


def test_normalize_invalid_falls_back_unknown():
    """非法值回退 unknown（安全默认）。"""
    assert normalize_category("bogus") == UNKNOWN
    assert normalize_category(None) == UNKNOWN
    assert normalize_category(123) == UNKNOWN


# ── 分类 → 动作映射 ──────────────────────────────────────────────────────

def test_default_action_mapping():
    """分类 → 默认动作映射表。"""
    assert DEFAULT_ACTION[SCRIPT_BUG] == ACTION_HEAL
    assert DEFAULT_ACTION[PRODUCT_DEFECT] == ACTION_BLOCK
    assert DEFAULT_ACTION[ENVIRONMENT] == ACTION_OBSERVE
    assert DEFAULT_ACTION[FLAKY] == ACTION_OBSERVE
    assert DEFAULT_ACTION[KNOWN_ISSUE] == ACTION_OBSERVE
    assert DEFAULT_ACTION[UNKNOWN] == ACTION_OBSERVE


def test_action_for_defaults():
    """action_for 返回各分类默认动作。"""
    assert action_for(SCRIPT_BUG) == ACTION_HEAL
    assert action_for(PRODUCT_DEFECT) == ACTION_BLOCK
    assert action_for(ENVIRONMENT) == ACTION_OBSERVE
    assert action_for(FLAKY) == ACTION_OBSERVE


def test_action_for_known_issue_severe_blocks():
    """known_issue 严重时升为 block；非严重保持 observe。"""
    assert action_for(KNOWN_ISSUE) == ACTION_OBSERVE
    assert action_for(KNOWN_ISSUE, severe=True) == ACTION_BLOCK


def test_action_for_invalid_observes():
    """非法分类安全回退 observe。"""
    assert action_for("bogus") == ACTION_OBSERVE


# ── classify 确定性推断 ─────────────────────────────────────────────────

def test_classify_explicit_category_priority():
    """显式合法分类 → 直接采用（source=explicit）。"""
    out = classify(category=PRODUCT_DEFECT, status_code=500)
    assert out["category"] == PRODUCT_DEFECT
    assert out["action"] == ACTION_BLOCK
    assert out["source"] == "explicit"


def test_classify_5xx_is_environment():
    """5xx → environment（server/网关层大概率环境侧）。"""
    out = classify(status_code=500)
    assert out["category"] == ENVIRONMENT
    assert out["action"] == ACTION_OBSERVE
    assert out["source"] == "rule"


def test_classify_network_keywords_environment():
    """错误文本含超时/网络关键词 → environment。"""
    out = classify(error_text="connection refused after 30s timeout")
    assert out["category"] == ENVIRONMENT
    assert out["source"] == "rule"


def test_classify_unknown_when_no_signal():
    """无信号 → unknown + observe（安全默认）。"""
    out = classify()
    assert out["category"] == UNKNOWN
    assert out["action"] == ACTION_OBSERVE
    assert out["source"] == "unknown"


def test_classify_uses_error_object_when_error_text_empty():
    """error_text 为空但传了 error 异常 → 用其文本推断。"""
    out = classify(error=ConnectionError("connection refused"))
    assert out["category"] == ENVIRONMENT
    assert out["source"] == "rule"
