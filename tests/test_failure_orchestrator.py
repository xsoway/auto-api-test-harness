#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15
# @Filename : test_failure_orchestrator.py
# @Author   : Alan_Hsu
"""orchestrator 集成失败治理链 hermetic 验证。

验证 run_single 接上 FailureGovernor 后的三类分流：
- block（product_defect）→ 直接失败返回不尝试自愈，保留原错误 + 分类动作。
- observe（environment）→ 无侵入临时恢复成功 → recovered，且
  wrote_script=False（铁律不回写脚本），result.succeeded。
- heal（script_bug）→ 走既有 AgentHeal 闭环并 record_pass（簇群回移）。

注入 governor；control=0 时 governor=None → 走原流程字节等价。全部
MockTransport 零网络、零真模型。
"""

from __future__ import annotations

import httpx

from ah.core.orchestrator import LLMHooks, run_single
from ah.failure.cluster import ClusterStore
from ah.failure.governor import FailureGovernor
from ah.harness.http import HTTPClient
from ah.harness.loader import Case
from ah.utils.context import Context


def _case(*, name="gov-case", status=500) -> Case:
    """恒失败用例（断言期望 status，返回固定响应）。"""
    return Case(
        name=name,
        request={"method": "GET", "path": "/api/gov"},
        assert_={"status_code": status},
    )


def _client(*, body='{"ok": false}', status=503) -> HTTPClient:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(status, text=body)

    return HTTPClient("http://api.test",
                      client=httpx.Client(transport=httpx.MockTransport(handler)))


def _observe_ok(mode="retry-original"):
    """注入一个总是恢复成功的 observe 探测器（无脚本写回）。"""
    class _Ob:
        recovered = {"recovered": True, "mode": mode, "wrote_script": False}
        def recover(self, case, resp, error, ctx, attempt):
            return dict(self.recovered)
    return _Ob()


def _governor(*, observe=None, clusters=None):
    return FailureGovernor(
        clusters=clusters or ClusterStore(path=None), observe=observe,
    )


def _run(case, governor, triage=None):  # noqa: ANN001
    hooks = LLMHooks(triage=triage) if triage else None
    client = _client()
    try:
        return run_single(case, Context(), client, hooks=hooks, governor=governor)
    finally:
        client.close()


# ── block：产品缺陷终止 ─────────────────────────────────────────────────

def test_block_product_defect_fails_without_heal():
    """product_defect（显式 Laya 分类高置信）→ block：直接失败，不尝试自愈。"""
    # 显式分类走 governor 显式通道（模拟 Laya 高置信 product_defect）。
    g = _governor()
    case = _case()
    # 手动驱动一次 decide 并把动作置 block：模拟 orchestrator 已在失败路径
    # 用显式 product_defect 决策。此处直接调用 decide 验证分流语义。
    d = g.decide(method="GET", path="/api/gov", case_name=case.name,
                 error=AssertionError("data mismatch"),
                 explicit_category="product_defect")
    assert d["action"] == "block"
    assert g.should_block(d["action"])
    assert not g.should_heal(d["action"])


# ── observe：无侵入恢复 ─────────────────────────────────────────────────

def test_observe_recovers_without_writing_script():
    """observe 恢复成功 → succeeded，llm_notes recovered.wrote_script False。"""
    g = _governor(observe=_observe_ok())
    case = _case()
    # environment 5xx 走 rule → observe。
    d = g.decide(method="GET", path="/api/gov", case_name=case.name,
                 error=ConnectionError("timeout"), status_code=503)
    assert d["action"] == "observe"
    rec = g.observe_recover(case=case, resp=None, error=ConnectionError("timeout"),
                            ctx=Context(), attempt=1)
    assert rec["recovered"] is True
    assert rec["wrote_script"] is False  # 铁律：observe 不回写脚本


def test_run_single_with_governor_observe_marks_recovered():
    """run_single 接 observe-governor：失败 → observe 恢复成功 → succeeded。"""
    # 无显式分类 + 500 → rule → environment → observe。
    g = _governor(observe=_observe_ok())
    result = _run(_case(), g)
    assert result.succeeded
    recovered = result.llm_notes.get("recovered") or {}
    assert recovered.get("level") == "observe"
    assert recovered.get("wrote_script") is False  # 铁律


def test_run_single_governor_none_keeps_original_flow():
    """governor=None（默认）→ 原流程：不写 failure_governance/recovered。"""
    g = None  # 走原流程
    result = _run(_case(), g)
    assert not result.succeeded
    assert "failure_governance" not in result.llm_notes
    assert "recovered" not in result.llm_notes


# ── heal：AgentHeal 闭环 + record_pass ─────────────────────────────────

def test_heal_script_bug_goes_to_agent_heal_and_records_pass():
    """script_bug → heal；heal 成功 record_pass 驱动簇群可回移。"""
    c = ClusterStore(path=None)
    g = _governor(clusters=c)
    # 先让簇走 5 次失败到 quarantined（heal 前的失败积累）。
    for _ in range(5):
        g.decide(method="GET", path="/api/gov", case_name="gov-case",
                 error=AssertionError("x"), explicit_category="script_bug")
    assert c.quarantined_keys()
    # heal 成功后 record_pass → 回移 recovered。
    for _ in range(2):
        g.record_pass(method="GET", path="/api/gov", case_name="gov-case",
                      failure_class="script_bug", error=AssertionError("x"))
    assert c.states().get("recovered", 0) == 1
    assert not c.quarantined_keys()


# ── block：run_single 分流 ──────────────────────────────────────────────

class _BlockGovernor:
    """把任意失败都决策为 block 的替身，验证 run_single 分流终止。"""

    def decide(self, **kw):  # noqa: ANN001
        return {
            "action": "block", "category": "product_defect", "source": "explicit",
            "cluster_state": "active", "cluster_key": "k",
            "knowledge": {"hit": False, "kind": None, "knowledge_id": None,
                          "evidence": []},
            "signal_error": str(kw.get("error")),
        }

    def observe_recover(self, **kw):  # noqa: ANN001
        return {"recovered": False, "tried": [], "reason": "blocked"}

    def record_pass(self, **kw):  # noqa: ANN001
        return {"cluster_state": "active", "cluster_key": "k"}


def test_run_single_block_terminates_without_heal():
    """block 决策 → run_single 直接失败返回，不尝试自愈，带 failure_governance。"""
    g = _BlockGovernor()
    result = _run(_case(), g)
    assert not result.succeeded
    assert result.error is not None
    gd = result.llm_notes.get("failure_governance") or {}
    assert gd.get("action") == "block"
    # block 不落地 healed/recovered 标记。
    assert "healed" not in result.llm_notes
    assert "recovered" not in result.llm_notes
