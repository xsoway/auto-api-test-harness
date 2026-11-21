#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/06/26 13:30
# @Filename : receipt.py
# @Author   : Alan_Hsu

"""面向编码 Agent 的「验真回执」VerificationReceipt。

对齐《让 AI 编码 Agent 自己跑回归》的三字段契约：

- 字段一（跑没跑完）：``status`` + ``started_at``/``ended_at``，
  Agent 用结束时间与它的最后一次提交比对，判断这次回归覆盖了哪一版代码。
- 字段二（过没过）：``steps`` 与 ``assertions`` 分开计数，
  并内嵌硬规则——**断言通过数为 0 时禁止返回 PASS，只能返回 UNVERIFIED**，
  把「零断言假通过」挡在 Agent 的决策之外。
- 字段三（证据在哪）：``evidence`` 提供可读回的报告/日志/trace 地址，
  Agent 失败时能把现场直接交给人工，而不是一句「失败了」。

本模块不依赖 LLM、零网络，``LLMOFF=1`` 下可安全复用。输入采用鸭子类型
（凡具备 ``verdict`` / ``assertions`` / ``status`` 的对象即可），因此
既能消费 :class:`ah.core.orchestrator.StepResult`，也能消费
:class:`ah.reporting.writer.ResultRecord`。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

#: 回执可通过的判定：断言总数必须 > 0，且全过、无失败；否则不是 PASS。
_VERDICT_PASS = "PASS"
_VERDICT_FAIL = "FAIL"
_VERDICT_UNVERIFIED = "UNVERIFIED"
_VERDICT_BLOCKED = "BLOCKED"
_VERDICT_SKIP = "SKIP"
_VERDICT_ERROR = "ERROR"


def _iso_now() -> str:
    """UTC ISO 时间（``YYYY-mm-ddTHH:MM:SSZ``），用于回执时间戳。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _verdict_of(result: Any) -> str:
    """把一个结果对象归一成回执判定的五态 verdict。

    STATUS/BLOCKED 优先（未执行）、UNVERIFIED（零断言/未验证）、
    FAIL（含断言失败或终态失败）、其余为通过候选。返回大写的回执判定值。
    """
    status = str(getattr(result, "status", "executed") or "executed")
    if status == "blocked":
        return _VERDICT_BLOCKED
    if status == "skipped":
        return _VERDICT_SKIP

    verdict = str(getattr(result, "verdict", "") or "")
    # 历史/手工结果没有 verdict 时退化为 label 推断（兼容 ResultRecord.from_dict）。
    label = str(getattr(result, "result", "") or "")
    if not verdict:
        if label in {"FAIL", "ERROR"}:
            verdict = "failed"
        elif label in {"PASS", "SKIP", "BLOCKED", "UNVERIFIED"}:
            verdict = {"PASS": "passed", "SKIP": "skipped",
                       "BLOCKED": "blocked", "UNVERIFIED": "unverified"}[label]
        elif not getattr(result, "succeeded", False):
            verdict = "failed"
        else:
            verdict = "unverified"

    assertions = list(getattr(result, "assertions", None) or [])
    if verdict == "blocked":
        return _VERDICT_BLOCKED
    if verdict == "skipped":
        return _VERDICT_SKIP
    if verdict == "failed":
        return _VERDICT_FAIL
    # 断言数单独核验：即使上层标 passed，零断言也按硬规则判「未验证」。
    if not assertions or not any(a.get("passed") for a in assertions):
        return _VERDICT_UNVERIFIED
    if any(not a.get("passed") for a in assertions):
        return _VERDICT_FAIL
    return _VERDICT_PASS


def _counts(results: List[Any], key: str) -> Dict[str, int]:
    """把结果列表按某个判定态聚合成 ``{total, passed, failed}``。

    ``key`` 决定怎么分类每条：断言（一条断言一条）或步骤（一个结果一态）。
    步骤侧的 passed/failed 用 :func:`_verdict_of` 得到；断言侧从每条
    断言的 ``passed`` 布尔直接数。
    """
    out = {"total": 0, "passed": 0, "failed": 0}
    if key == "steps":
        for r in results:
            v = _verdict_of(r)
            out["total"] += 1
            if v == _VERDICT_PASS:
                out["passed"] += 1
            elif v in {_VERDICT_FAIL, _VERDICT_ERROR}:
                out["failed"] += 1
    elif key == "assertions":
        for r in results:
            for a in (getattr(r, "assertions", None) or []):
                out["total"] += 1
                if bool(a.get("passed")):
                    out["passed"] += 1
                else:
                    out["failed"] += 1
    return out


def build_verification_receipt(
    results: List[Any],
    *,
    execution_id: str = "",
    environment: str = "",
    started_at: str = "",
    ended_at: str = "",
    evidence: Optional[Dict[str, str]] = None,
    commit_sha: str = "",
) -> Dict[str, Any]:
    """构建一次回归的机器可读「验真回执」。

    :param results: 本次执行的各步骤/用例结果（鸭子类型，见模块 docstring）。
    :param execution_id: 执行 ID（缺省按结束时间戳自动生成）。
    :param environment: 环境标记（如 test/prod），区分 staging 与生产结论。
    :param started_at: 本次回归开始时间（ISO）；缺省用最早一条结果的开始时间。
    :param ended_at: 本次回归结束时间（ISO）；缺省用最晚一条的结束时间/当前时间。
    :param evidence: 证据地址映射（如 report_md/report_html/report_json/run_log/trace_url）。
    :param commit_sha: 被验证的代码 commit（供 Agent 与 ended_at 交叉印证）。
    :return: 符合三字段契约的字典，可直接 ``json.dumps`` 输出给 Agent。
    """
    steps = _counts(results, "steps")
    assertions = _counts(results, "assertions")

# ---- 判定主结果：先看断言，再看步骤 ----
    # 通过的门槛：所有步骤都真通过（passed == total），且至少有一条通过断言
    # （断言通过数为 0 → 只能「未验证」，绝不「通过」）。
    all_steps_passed = (
        steps["total"] > 0 and steps["passed"] == steps["total"] and steps["failed"] == 0
    )
    has_positive_assertions = assertions["total"] > 0
    no_assertion_failure = assertions["failed"] == 0

    if steps["total"] == 0:
        status = "not_executed"
        verdict = _VERDICT_SKIP
    elif all_steps_passed and has_positive_assertions and no_assertion_failure:
        verdict = _VERDICT_PASS
        status = "completed"
    elif not has_positive_assertions and not any(
        _verdict_of(r) in {_VERDICT_FAIL, _VERDICT_BLOCKED, _VERDICT_ERROR}
        for r in results
    ):
        # 硬规则：一条断言都没有（且无明确失败）→ 只能是「未验证」，不是「通过」。
        verdict = _VERDICT_UNVERIFIED
        status = "completed"
    else:
        verdict = _VERDICT_FAIL
        status = "completed"

    resolved_started = started_at or _earliest_start(results)
    resolved_ended = ended_at or _latest_end(results) or _iso_now()

    return {
        "receipt_schema": "verification-receipt/1",
        "execution_id": execution_id or f"run-{resolved_ended}",
        "environment": environment,
        "status": status,
        "verdict": verdict,
        "started_at": resolved_started,
        "ended_at": resolved_ended,
        "steps": steps,
        "assertions": assertions,
        "commit": commit_sha,
        "evidence": evidence or {},
    }


def _earliest_start(results: List[Any]) -> str:
    """取所有结果里最早的非空 started_at；都缺省返回空串。"""
    vals = [str(r.started_at) for r in results if getattr(r, "started_at", "")]
    return min(vals) if vals else ""


def _latest_end(results: List[Any]) -> str:
    """取所有结果里最晚的非空 ended_at；都缺省返回空串。"""
    vals = [str(r.ended_at) for r in results if getattr(r, "ended_at", "")]
    return max(vals) if vals else ""
