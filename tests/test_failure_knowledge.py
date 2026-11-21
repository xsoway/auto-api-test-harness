#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15
# @Filename : test_failure_knowledge.py
# @Author   : Alan_Hsu
"""失败知识层：resolved / known_issue 检索闭环 hermetic 单测。

对应《别让自愈把红灯洗绿》步骤4：知识沉淀 → 草稿/发布 → 检索命中改分类动作
→ 使用反馈（hit/effective/score）。

覆盖：
- 草稿新增 / 发布 / drafts / published 查询。
- 命中 known_issue → 分类强制 known_issue，动作按严重性（observe/block）。
- 命中 resolved → 沿用根因/解法，分类动作按默认逻辑。
- 草稿不参与检索（only_published 默认 True），发布后才命中。
- 精确/宽松（忽略 error_sig）检索。
- hit_count / effective_count / score 反馈闭环。
- JSON 落盘 + 重启恢复。

全部纯内存或 tmp_path，零网络、零真模型。
"""

from __future__ import annotations

from ah.failure.knowledge import (
    TYPE_KNOWN_ISSUE,
    TYPE_RESOLVED,
    KnowledgeStore,
)


def _draft_known(store, *, error="timeout", workaround="重试并扩大超时"):
    return store.add_draft(
        method="GET", path="/api/pay", failure_class="environment",
        error=error, kind=TYPE_KNOWN_ISSUE,
        root_cause="支付网关外部波动", workaround=workaround,
    )


# ── 草稿 / 发布 ─────────────────────────────────────────────────────────

def test_draft_then_publish():
    """新知识先草稿，publish 后转 published，查询可分态。"""
    s = KnowledgeStore(path=None)
    key = _draft_known(s)
    assert s.drafts()
    assert not s.published()
    assert s.publish(key)
    assert s.published()
    assert not s.drafts()
    assert s.published()[0]["id"] == key


def test_publish_missing_key_returns_false():
    """publish 不存在的 key → False。"""
    s = KnowledgeStore(path=None)
    assert not s.publish("nope")


# ── 检索与应用 ──────────────────────────────────────────────────────────

def test_published_known_issue_apply_changes_category():
    """命中发布的 known_issue → 分类强制 known_issue，非严重动作 observe。"""
    s = KnowledgeStore(path=None)
    s.publish(_draft_known(s))
    out = s.apply(
        method="GET", path="/api/pay", failure_class="environment",
        error="timeout", default_category="environment", default_action="observe",
    )
    assert out["hit"] is True
    assert out["kind"] == TYPE_KNOWN_ISSUE
    assert out["category"] == "known_issue"
    assert out["action"] == "observe"
    assert out["evidence"]  # workaround 进入证据


def test_known_issue_severe_blocks():
    """known_issue 且 severe → block。"""
    s = KnowledgeStore(path=None)
    s.publish(_draft_known(s))
    out = s.apply(
        method="GET", path="/api/pay", failure_class="environment",
        error="timeout", default_category="environment",
        default_action="observe", severe=True,
    )
    assert out["hit"] is True
    assert out["action"] == "block"


def test_draft_not_hit_by_default():
    """草稿不参与检索（only_published 默认 True）→ no hit。"""
    s = KnowledgeStore(path=None)
    _draft_known(s)  # 仅草稿
    out = s.apply(
        method="GET", path="/api/pay", failure_class="environment",
        error="timeout", default_category="environment", default_action="observe",
    )
    assert out["hit"] is False


def test_resolved_apply_keeps_default_but_supplies_evidence():
    """命中 resolved → 分类动作沿用默认，附根因/解法证据。"""
    s = KnowledgeStore(path=None)
    key = s.add_draft(
        method="GET", path="/api/users", failure_class="environment",
        error="connection refused", kind=TYPE_RESOLVED,
        root_cause="DNS 配置错误", solution="修正 hosts 映射",
    )
    s.publish(key)
    out = s.apply(
        method="GET", path="/api/users", failure_class="environment",
        error="connection refused", default_category="environment",
        default_action="observe",
    )
    assert out["hit"] is True
    assert out["kind"] == TYPE_RESOLVED
    assert out["category"] == "environment"
    assert out["action"] == "observe"
    assert "DNS" in "".join(out["evidence"])


def test_relaxed_search_ignores_error_sig():
    """宽松检索：同 method+path+failure_class，error_sig 不同也命中。"""
    s = KnowledgeStore(path=None)
    s.publish(_draft_known(s, error="timeout"))
    out = s.search(
        method="GET", path="/api/pay", failure_class="environment",
        error="timed out after 30s",  # 不同 error_sig
    )
    assert out is not None
    assert out["id"].endswith("|timeout") or out["id"].endswith("|")


def test_no_hit_apply_returns_default():
    """未命中 → hit=False，分类动作回退默认。"""
    s = KnowledgeStore(path=None)
    out = s.apply(
        method="GET", path="/nope", failure_class="unknown",
        error="x", default_category="unknown", default_action="observe",
    )
    assert out["hit"] is False
    assert out["category"] == "unknown"
    assert out["action"] == "observe"


# ── 使用反馈闭环 ────────────────────────────────────────────────────────

def test_feedback_loop_hit_effective_score():
    """每次命中 hit_count+1；反馈 effective 影响 effective_count 与 score。"""
    s = KnowledgeStore(path=None)
    key = _draft_known(s)
    s.publish(key)
    s.apply(method="GET", path="/api/pay", failure_class="environment", error="timeout")
    s.apply(method="GET", path="/api/pay", failure_class="environment", error="timeout")
    assert s.published()[0]["hit_count"] == 2
    assert s.published()[0]["score"] == 0.0  # 尚无 effective 反馈
    s.record_effective(key, effective=True)
    assert s.published()[0]["effective_count"] == 1
    assert s.published()[0]["score"] == 1 / 2  # 1 effective / 2 hit
    s.record_effective(key, effective=True)
    assert s.published()[0]["effective_count"] == 2
    assert s.published()[0]["score"] == 1.0  # 2/2






def test_record_effective_unknown_key_noop():
    """feedback 不存在的 key → 无副作用。"""
    s = KnowledgeStore(path=None)
    s.record_effective("nope", effective=True)  # 不抛


# ── 落盘 ────────────────────────────────────────────────────────────────

def test_json_persist_roundtrip(tmp_path):
    """JSON 落盘 + 重启恢复草稿/发布态。"""
    p = tmp_path / "failure_knowledge.json"
    s = KnowledgeStore(path=p)
    key = _draft_known(s)
    s.publish(key)
    s2 = KnowledgeStore(path=p)
    assert s2.published()[0]["id"] == key
    assert p.exists()
