#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15
# @Filename : test_failure_governor.py
# @Author   : Alan_Hsu
"""失败治理编排器（FailureGovernor）决策链 hermetic 单测。

覆盖决策链：triage（显式分类/规则）→ knowledge 覆盖 → 动作分流
（heal/observe/block）→ cluster 记录。

- 显式分类（LLM/Laya）优先于确定性规则。
- 5xx → environment → observe。
- product_defect 显式 → block（should_block True，should_heal False）。
- script_bug → heal。
- knowledge 命中 known_issue → 覆盖分类/动作。
- observe_recover：注入 AgentObserve 成功 → recovered 且 wrote_script=False；
  未配置 observer → no-observer；observer 抛异常 → observer-error。
- record_pass 驱动簇群回移（quarantined → recovered）。

全部纯内存，零网络、零真模型。
"""

from __future__ import annotations

from ah.failure.cluster import ClusterStore
from ah.failure.governor import FailureGovernor
from ah.failure.knowledge import TYPE_KNOWN_ISSUE, KnowledgeStore
from ah.failure.triage import ACTION_BLOCK, ACTION_HEAL, ACTION_OBSERVE


def _governor(**kw):
    return FailureGovernor(**kw)
def _case(name="u"):
    """构造最小用例对象（供 observe 恢复调用）。"""
    class _C:
        request = {"method": "GET", "path": "/api/users"}
        def __init__(self) -> None:
            self.name = name
    return _C()


# ── 决策链：分类 → 动作 ─────────────────────────────────────────────────

def test_explicit_product_defect_blocks():
    """显式 product_defect → block（产品缺陷，终止等人工）。"""
    g = _governor()
    d = g.decide(method="GET", path="/api/users", case_name="u",
                 error=ValueError("bad"), explicit_category="product_defect")
    assert d["category"] == "product_defect"
    assert d["action"] == ACTION_BLOCK
    assert d["source"] == "explicit"
    assert g.should_block(d["action"])
    assert not g.should_heal(d["action"])


def test_explicit_script_bug_heals():
    """显式 script_bug → heal（交给 AgentHeal）。"""
    g = _governor()
    d = g.decide(method="GET", path="/api/users", case_name="u",
                 error=ValueError("bad"), explicit_category="script_bug")
    assert d["action"] == ACTION_HEAL
    assert g.should_heal(d["action"])


def test_5xx_rules_to_observe():
    """无显式分类 + 5xx → 规则推断 environment → observe。"""
    g = _governor()
    d = g.decide(method="GET", path="/api/users", case_name="u",
                 error=ConnectionError("timeout"), status_code=500)
    assert d["category"] == "environment"
    assert d["action"] == ACTION_OBSERVE
    assert d["source"] == "rule"


def test_unknown_no_signal_observes():
    """无信号 → unknown + observe（安全默认）。"""
    g = _governor()
    d = g.decide(method="GET", path="/api/users", case_name="u",
                 error=RuntimeError("x"))
    assert d["category"] == "unknown"
    assert d["action"] == ACTION_OBSERVE


def test_explicit_overrides_rule():
    """显式分类优先于 5xx 规则。"""
    g = _governor()
    d = g.decide(method="GET", path="/api/users", case_name="u",
                 error=ValueError("bad"), status_code=500,
                 explicit_category="script_bug")
    assert d["category"] == "script_bug"
    assert d["action"] == ACTION_HEAL


# ── knowledge 覆盖 ──────────────────────────────────────────────────────

def test_knowledge_known_issue_overrides_action():
    """命中发布 known_issue → 分类强制 known_issue，动作 observe。"""
    k = KnowledgeStore(path=None)
    k.publish(k.add_draft(
        method="GET", path="/api/users", failure_class="environment",
        kind=TYPE_KNOWN_ISSUE, workaround="重试并换节点",
    ))
    g = _governor(knowledge=k)
    d = g.decide(method="GET", path="/api/users", case_name="u",
                 error=ConnectionError("timeout"))
    assert d["knowledge"]["hit"] is True
    assert d["category"] == "known_issue"
    assert d["action"] == ACTION_OBSERVE
    assert d["source"] == "knowledge"


# ── observe 分支 ────────────────────────────────────────────────────────

class _FakeObserve:
    def __init__(self, ok=True, mode="retry-original"):
        self._ok = ok
        self._mode = mode
        self.calls = 0

    def recover(self, case, resp, error, ctx, attempt):
        self.calls += 1
        if not self._ok:
            return {"recovered": False, "tried": ["retry-original"]}
        return {
            "recovered": True, "mode": self._mode,
            "wrote_script": False, "case": getattr(case, "name", "u"),
            "original_error": str(error), "attempt": attempt,
        }


def test_observe_recover_success_no_script_write():
    """observe 恢复成功 → recovered，且 wrote_script=False（铁律不回写）。"""
    ob = _FakeObserve(ok=True)
    g = _governor(observe=ob)
    rec = g.observe_recover(case=_case(), resp=None, error=ValueError("x"),
                            ctx=None, attempt=1)
    assert rec["recovered"] is True
    assert rec["mode"] == "retry-original"
    assert rec["wrote_script"] is False  # 铁律
    assert ob.calls == 1


def test_observe_recover_no_observer():
    """未配置 observer → 返回 no-observer（observe 仅记录）。"""
    g = _governor(observe=None)
    rec = g.observe_recover(case=_case(), resp=None, error=ValueError("x"),
                            ctx=None, attempt=1)
    assert rec["recovered"] is False
    assert rec["reason"] == "no-observer"


def test_observe_recover_failure_returns_tried():
    """恢复失败 → recovered=False + tried（进隔离观察，不改脚本）。"""
    ob = _FakeObserve(ok=False)
    g = _governor(observe=ob)
    rec = g.observe_recover(case=_case(), resp=None, error=ValueError("x"),
                            ctx=None, attempt=1)
    assert rec["recovered"] is False
    assert rec["tried"] == ["retry-original"]


# ── cluster 记录 / record_pass 回移 ─────────────────────────────────────

def test_decide_records_cluster_failure():
    """decide 记录失败进簇群（fail_count 累加）。"""
    c = ClusterStore(path=None)
    g = _governor(clusters=c)
    g.decide(method="GET", path="/a", case_name="u", error=ValueError("x"),
             explicit_category="environment")
    g.decide(method="GET", path="/a", case_name="u", error=ValueError("x"),
             explicit_category="environment")
    states = c.states()
    assert states.get("active", 0) == 1  # 同一簇聚合
    assert len(c._clusters) == 1  # noqa: SLF001


def test_record_pass_drives_cluster_recovery():
    """隔离簇连续通过 → recovered（record_pass 回移）。"""
    c = ClusterStore(path=None)
    g = _governor(clusters=c)
    # 5 次失败 → quarantined
    for _ in range(5):
        g.decide(method="GET", path="/a", case_name="u",
                 error=ConnectionError("timeout"), explicit_category="environment")
    assert c.quarantined_keys()
    # 连续通过 2 次 → recovered
    for _ in range(2):
        g.record_pass(method="GET", path="/a", case_name="u",
                      failure_class="environment", error=ConnectionError("timeout"))
    assert c.states().get("recovered", 0) == 1
    assert not c.quarantined_keys()
