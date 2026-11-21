"""LLM 助手 agents：数据生成、三分类、自愈、报告分析。

每个 agent 都是 *helper*。每个 agent 都有确定性的隔离回退，用于 LLM
被禁用（设置了 ``LLMOFF`` / 无 key）时——测试套件始终以零网络通过。
实时路径尽力而为，绝不做 CI 的门禁。任何 :class:`BaseLLM` 后端都可用
（实时的 :class:`LLMClient` 或隔离的 :class:`MockLLM`）。

- :class:`AgentDataGen` — 为 ``llm_gen`` 用例合成请求参数。
- :class:`AgentTriage` — 为报告与自愈根因失败。
- :class:`AgentHeal` — 反思循环：请强模型给出修正后的用例，
  通过 :func:`ah.core.orchestrator.run_single` 重新验证，仅当重跑通过时接受。
- :class:`AgentReport` — 将一批结果分析为 markdown 摘要。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ah.core.orchestrator import StepResult, run_single
from ah.failure.triage import (
    CATEGORY_DESC,
    FAILURE_CATEGORIES,
    action_for,
    classify,
    normalize_category,
)
from ah.harness.http import APIResponse
from ah.harness.loader import Case
from ah.utils.context import Context
from ah.utils.logging import get_logger

log = get_logger("agents")

#: LLM 关闭时的标记键（与 :func:`ah.llm.openai.llm_enabled` 的 LLMOFF 判定一致）。
LLM_OFF_MARKER = "LLM_OFF"


def _fallback_run_analysis(passed: int, total: int, failed: list, flaky: list) -> str:
    """确定性回退：LLM 不可用/失败时给出可落盘的计数摘要。

    与 :meth:`AgentReport._nl_observation` 的 JSON 结构对位，保证报告「运行分析」
    LLM 段在关闭/失败时仍有一致形态，绝不因缺 LLM 中断报告生成。
    """
    lines = []
    rate = f"{passed / total * 100:.1f}%" if total else "—"
    lines.append(f"- **总体研判**：本轮 {passed}/{total} 通过（{rate}，确定性计数）。")
    if failed:
        lines.append("- **评估维度**：")
        lines += [f"  - 失败 {len(failed)} 条，需逐条核验是否为真实缺陷。" for _ in (1,)]
        by: dict[str, int] = {}
        for r in failed:
            cause = (getattr(r, "failure_msg", None) or "unknown").split("\n")[0]
            by[cause] = by.get(cause, 0) + 1
        top = "；".join(f"`{c}`×{n}" for c, n in sorted(by.items(), key=lambda kv: -kv[1])[:3])
        lines.append(f"  - 失败 top：{top}")
    if flaky:
        names = "、".join(getattr(r, "name", "?") for r in flaky)
        lines.append(f"  - 稳定性：{names} 出现跨 run 抖动（flaky），建议优先去抖动。")
    lines.append("- **下一步建议**：")
    lines.append("  - 在启用 LLM（配置 api_key 且未置 LLMOFF）后自动生成专业分析建议。")
    return "\n".join(lines)


class AgentDataGen:
    """为 ``llm_gen`` 用例生成请求参数（隔离模式：返回 {}）。"""

    def __init__(self, llm: Any = None) -> None:
        self._llm = llm  # 可选 BaseLLM

    def generate_request_body(
        self, case_schema: dict[str, Any], example: Any = None
    ) -> dict[str, Any]:
        """返回请求体 dict；LLM 被禁用时返回确定性的 ``{}``。"""
        if self._llm is None:
            return {}
        prompt = (
            "Given this JSON schema for an API request body, produce one concrete "
            "example instance as a JSON object.\nSCHEMA:\n"
            f"{json.dumps(case_schema, ensure_ascii=False, indent=2)}\n"
        )
        if example is not None:
            prompt += f"REFERENCE EXAMPLE:\n{json.dumps(example, ensure_ascii=False)}\n"
        return self._llm.judge("data-gen", prompt, model="cheap")


class AgentTriage:
    """解释失败根本原因并解析分类与动作（隔离模式：确定性分类）。

    把失败分类成文章2 的 6 类（script_bug/product_defect/environment/flaky/
    known_issue/unknown），并按映射表收敛成三动作（heal/observe/block）。
    输出 ``{"root_cause", "category", "action", "suggestion"}``；
    LLM 被禁用时用确定性信号（status/error_text）推断分类，绝不触网。

    语义分类交给 LLM；确定性核验（status/error）用规则先行——对应 JEV
    "先明确哪些事实由代码核验，再把语义判断交给模型"。
    """

    def __init__(self, llm: Any = None) -> None:
        self._llm = llm

    def explain_failure(
        self, case: Case, resp: Optional[APIResponse], error: BaseException
    ) -> dict[str, Any]:
        """返回根因分析 + 分类 + 动作；LLM 被禁用时返回确定性结果。"""
        status = resp.status_code if resp else None
        body = (resp.text[:800] if resp else "")
        error_text = str(error)
        # 确定性分类兜底（LLM 缺席/失败时也有明确分类与动作，不裸奔 unknown）。
        rule = classify(
            status_code=status,
            error=error,
            error_text=error_text,
        )
        if self._llm is None:
            return {
                "llm_off": True,
                "root_cause": f"hard failure in {case.name}",
                "category": rule["category"],
                "action": rule["action"],
                "error": error_text,
            }
        status_code = status
        prompt = (
            f"Case {case.name!r} failed. status_code={status_code} error={error}\n"
            f"response_body={body!r}\n"
            "Classify the failure into EXACTLY ONE category "
            f"({', '.join(FAILURE_CATEGORIES)}).\n"
            "Categories: " + "; ".join(
                f"{c}={CATEGORY_DESC[c]}" for c in FAILURE_CATEGORIES
            ) + ".\n"
            "Return a JSON object: {\"root_cause\": str, \"category\": \"<one of "
            "the categories>\", \"suggestion\": str}. Prefer 'product_defect' when "
            "the response contradicts expected behavior; 'environment' for "
            "network/5xx/timeout; 'script_bug' when request/assert is malformed."
        )
        result = self._llm.judge("triage", prompt, model="strong")
        if not isinstance(result, dict):
            result = {}
        # LLM 给出的分类未必合法 → 归一化到合法集合；动作由分类解析。
        category = normalize_category(result.get("category", ""))
        return {
            "root_cause": result.get("root_cause") or "未知根因",
            "category": category,
            "action": action_for(category),
            "suggestion": result.get("suggestion") or rule["detail"],
        }

class AgentHeal:
    """反思循环：提出修正用例、重新运行、仅当通过时接受。

    ``heal(case, resp, error, attempt)`` 返回 ``(ok, meta)``，其中 ``ok`` 为 True
    表示修正用例的重跑通过。当 LLM 被禁用时，它绝不调用网络，并返回
    ``(False, {"llm_off": True, ...})``。
    """

    def __init__(
        self,
        client: Any,
        ctx: Context,
        llm: Any = None,
        *,
        max_heal_attempts: int = 2,
        history_rows: Optional[list[dict[str, Any]]] = None,
        min_samples: int = 3,
    ) -> None:
        self._client = client
        self._ctx = ctx
        self._llm = llm
        self.max_heal_attempts = max_heal_attempts
        self._history = history_rows or []
        self._min_samples = min_samples

    def _should_attempt(self, case: Case) -> bool:
        """自愈成功率护栏：接口长期治不愈（持续失败无一次通过）就跳过 L2 自愈。

        对应提案 §3（arXiv:2605.06737 可靠性感知自愈）：不盲目重试。从逐运行
        历史里按接口路径累计结果；样本足够且从未出现 PASS（持久性缺陷）则
        直接放弃自愈，把根因留给报告，而不是反复重放。
        """
        if not self._history:
            return True
        target = case.request_path
        outcomes = [
            res
            for run in self._history
            for name, res in (run.get("cases") or {}).items()
            if name == case.name or name == target
        ]
        if len(outcomes) < self._min_samples:
            return True  # 样本不足，不做激进闸断。
        return "PASS" in outcomes  # 只要从未通过 -> 持久性失败，跳过自愈。

    def heal(
        self, case: Case, resp: Optional[APIResponse], error: BaseException, attempt: int
    ) -> tuple[bool, dict[str, Any]]:
        """一次反思尝试；通过重跑用例进行再验证。"""
        if not self._should_attempt(case):
            return False, {
                "skipped": "persistent-failure",
                "action": "no-heal",
                "original_error": str(error),
            }
        if self._llm is None:
            return False, {
                "llm_off": True,
                LLM_OFF_MARKER: True,
                "action": "no-fix-available",
                "original_error": str(error),
            }
        try:
            candidate = self._propose_candidate(case, resp, error, attempt)
        except Exception as exc:
            return False, {"llm_error": str(exc), "action": "proposal-failed"}

        # 对一份近乎全新的上下文重跑候选用例，以再验证该候选。
        trial_ctx = self._ctx.copy()
        result = run_single(candidate, trial_ctx, self._client)
        if result.succeeded:
            return True, {
                "candidate": str(candidate.request),
                "assert": candidate.assert_,
                "validated_by": "re-run",
            }
        return False, {
            "candidate": str(candidate.request),
            "re_run_error": str(result.error),
            "action": "re-run-failed",
        }

    def _propose_candidate(
        self, case: Case, resp: Optional[APIResponse], error: BaseException, attempt: int
    ) -> Case:
        status = resp.status_code if resp else None
        body = (resp.text[:800] if resp else "")
        mandatory = json.dumps(case.request, ensure_ascii=False)
        prompt = (
            f"You are fixing a failing API test case. Preserve mandatory fields "
            f"exactly (method, path, and any fields the later asserts depend on).\n"
            f"CURRENT REQUEST: {mandatory}\n"
            f"CURRENT ASSERT: {json.dumps(case.assert_, ensure_ascii=False)}\n"
            f"CURRENT EXTRACT: {json.dumps(case.extract, ensure_ascii=False)}\n"
            f"FAILURE (attempt {attempt}): status={status} error={error}\n"
            f"RESPONSE_BODY: {body!r}\n"
            "Return a JSON object with optional overrides: "
            '{"request": {optional overrides}, "assert": {corrected asserts}, '
            '"extract": {optional corrections}}. Keep request.method/request.path. '
            "If no correction helps, return {\"give_up\": true}."
        )
        data = self._llm.judge("heal", prompt, model="strong")
        if data.get("give_up"):
            raise RuntimeError("heal: model gave up")
        corrected_request = {**case.request, **(data.get("request") or {})}
        # 绝不让模型改动有界身份字段。
        corrected_request["method"] = case.request.get("method")
        corrected_request["path"] = case.request.get("path")
        return Case(
            name=case.name,
            request=corrected_request,
            extract=data.get("extract") or case.extract,
            assert_=data.get("assert") or case.assert_,
            vars_req=case.vars_req,
            vars_output=case.vars_output,
            auth=case.auth,
            llm_gen=case.llm_gen,
            on_failure=case.on_failure,
            scenario=case.scenario,
            order=case.order,
            source_path=case.source_path,
        )

def _semantic_verdict(record: Any) -> dict[str, Any]:
    """从一条记录提取 ``assert_llm`` 语义判定 + 是否需人工复核。

    返回 ``{"verdict", "flag", "reason"}``。绝不影响 hermetic：无判定时
    ``flag=False``，即不算需复核，也不冒进。
    """
    notes = (record.llm_notes or {}) if hasattr(record, "llm_notes") else {}
    all_verdicts = notes.get("assert_llm")
    if isinstance(all_verdicts, dict):
        all_verdicts = [all_verdicts]
    if not isinstance(all_verdicts, list):
        return {"verdict": None, "flag": False, "reason": ""}
    worst = {"verdict": None, "flag": False, "reason": ""}
    for v in all_verdicts:
        if not isinstance(v, dict):
            continue
        verdict = v.get("verdict")
        if verdict == "UNVERIFIED" and v.get("flagged"):
            worst = {"verdict": "UNVERIFIED", "flag": True,
                     "reason": str(v.get("reason", "") or "语义判定需人工复核")}
            break
        if verdict == "UNVERIFIED" and worst["verdict"] is None:
            worst = {"verdict": "UNVERIFIED", "flag": False,
                     "reason": str(v.get("reason", ""))}
    return worst


class AgentReport:
    """将运行结果分析为 markdown 摘要（隔离模式：仅计数）。"""

    def __init__(self, llm: Any = None) -> None:
        self._llm = llm

    def analyze_results(self, results: list[StepResult]) -> str:
        """生成包含各用例 LLM 注记的 markdown 分析。"""
        total = len(results)
        # 执行成功并不等于已验证；历史/手工记录缺 verdict 时按未知处理。
        passed = sum(1 for r in results if r.verdict == "passed")
        failed_rows = [r for r in results if r.status == "executed" and not r.succeeded]
        unverified = sum(
            1 for r in results
            if r.verdict == "unverified" or (not r.verdict and r.succeeded)
        )
        lines = [
            "## Run analysis",
            "",
            f"- cases: **{passed}/{total} passed** "
            f"({(passed / total * 100 if total else 0):.1f}%)",
            f"- unverified: **{unverified}**",
        ]
        if failed_rows:
            lines.append("")
            lines.append("### Top failure causes")
            by_err: dict[str, int] = {}
            for r in failed_rows:
                cause = str(r.error).split("\n")[0] if r.error else "unknown"
                by_err[cause] = by_err.get(cause, 0) + 1
            for cause, cnt in sorted(by_err.items(), key=lambda kv: -kv[1]):
                lines.append(f"- `{cause}` — {cnt}")
        lines.append("")
        lines.append("### Per-case notes")
        lines.append("")
        for r in results:
            note = "ok"
            if r.status != "executed":
                note = f"{r.status.upper()}: {r.reason}"
            elif not r.succeeded or r.verdict == "failed":
                note = f"FAILED: {r.error}"
            elif r.verdict != "passed":
                note = f"UNVERIFIED: {r.reason or '缺少已验证断言证据'}"
            elif r.llm_notes:
                note = f"ok with llm: {json.dumps(r.llm_notes, ensure_ascii=False)}"
            lines.append(f"- **{r.name}** — {note}")
        return "\n".join(lines)


    def summarize_records(
        self, records: list[Any], *, title: str = "运行分析"
    ) -> str:
        """从报告记录生成可落盘的 markdown 摘要（报告 NL 摘要）。

        确定性部分（数据驱动、可复现，即使 ``llm=None`` 也完整）：通过率、
        失败根因 top、flaky 接口清单、语义判定 UNVERIFIED 复核项。当
        ``llm`` 可用时追加一段模型 prose 观察。全部纯本地/隔离，绝不影响
        hermetic 运行。
        """
        records = list(records)
        total = len(records)
        passed = sum(1 for r in records if r.result == "PASS")
        flaky = [r for r in records if getattr(r, "flaky", False)]
        unverified = [r for r in records if _semantic_verdict(r).get("flag")]
        lines = [f"## {title}", "", f"- **{passed}/{total} passed** "
                 f"({(passed / total * 100 if total else 0):.1f}%)"]
        if flaky:
            names = "、".join(r.name for r in flaky)
            lines.append(f"- **flaky（跨 run 抖动）**：{names}")
        if unverified:
            lines.append("- **语义判定 UNVERIFIED（需人工复核）**：")
            for r in unverified:
                v = _semantic_verdict(r)
                lines.append(f"  - {r.name} — {v['reason'] or '语义不一致'}")
        failed = [r for r in records if r.result != "PASS"]
        if failed:
            lines.append("")
            lines.append("### Top failure causes")
            by_err: dict[str, int] = {}
            for r in failed:
                cause = (r.failure_msg or "unknown").split("\n")[0]
                by_err[cause] = by_err.get(cause, 0) + 1
            for cause, cnt in sorted(by_err.items(), key=lambda kv: -kv[1]):
                lines.append(f"- `{cause}` — {cnt}")
        if self._llm is not None:
            prose = self._nl_observation(records)
            if prose:
                lines += ["", "### LLM 运行观察", "", prose]
        return "\n".join(lines)

    def _nl_observation(self, records: list) -> str:
        """调用强模型给出一段专业级运行分析；失败/畸形时返回空串（不中断）。

        输出结构：总体研判 + 分维度评估（回归/稳定性/风险）+ 下一步建议，
        供「运行分析」「预测分析」段的 LLM 总结使用。
        """
        passed = sum(1 for r in records if getattr(r, "result", "") == "PASS")
        total = len(records)
        failed = [r for r in records if getattr(r, "result", "") != "PASS"]
        flaky = [r for r in records if getattr(r, "flaky", False)]
        pass_rate = f"{passed / total * 100:.1f}%" if total else "—"
        summary = "\n".join(
            f"- {r.name}: {r.result}"
            + (f" | err: {r.failure_msg}" if getattr(r, "failure_msg", None) else "")
            + (" | flaky" if getattr(r, "flaky", False) else "")
            + (f" | dur: {r.duration:.3f}s" if getattr(r, "duration", 0) else "")
            for r in records
        )
        failed_txt = "\n".join(f"- {r.name}: {r.failure_msg}" for r in failed) or "（无）"
        flaky_txt = "、".join(r.name for r in flaky) or "（无）"
        prompt = (
            "你是一名资深 API 自动化测试/质量工程专家。请基于以下一次真实 API 巡检结果，"
            "从专业角度输出运行分析与下一步建议。要求：\n"
            "1) 总体研判（verdict）：一句话给出本次运行的健康度结论；\n"
            "2) 评估维度（assessment）：2-4 条，覆盖回归、失败归因、稳定性(flaky)、"
            "性能/风险，每条给出依据（引用具体接口与现象）；\n"
            "3) 风险项（risks）：当前最值得关注的 1-3 个风险点；\n"
            "4) 下一步建议（recommendations）：2-4 条可执行、按优先级排序的建议"
            "（如补用例、加断言、排查特定接口、稳定性治理）。\n"
            f"通过率: {pass_rate} ({passed}/{total})\n"
            f"失败用例:\n{failed_txt}\n"
            f"flaky 接口: {flaky_txt}\n"
            f"逐用例(名称: 结果 | 错误 | 耗时):\n{summary}\n"
            "仅输出 JSON 对象，四个键: verdict(str), assessment(list[str]), "
            "risks(list[str]), recommendations(list[str])。"
        )
        try:
            data = self._llm.judge("report-summary", prompt, model="cheap")
        except Exception:
            # judge 不可用时退回确定性计数摘要，保证报告始终可落盘。
            return _fallback_run_analysis(passed, total, failed, flaky)
        if not isinstance(data, dict):
            return _fallback_run_analysis(passed, total, failed, flaky)
        lines = [f"- **总体研判**：{data.get('verdict') or '—'}"]
        assessment = data.get("assessment") or []
        if assessment:
            lines.append("- **评估维度**：")
            lines += [f"  - {a}" for a in assessment]
        risks = data.get("risks") or []
        if risks:
            lines.append("- **风险项**：")
            lines += [f"  - {r}" for r in risks]
        recs = data.get("recommendations") or []
        if recs:
            lines.append("- **下一步建议**：")
            lines += [f"  - {r}" for r in recs]
        return "\n".join(lines)

    def predict_advice(
        self,
        plan_dict: dict[str, Any],
        key_interfaces: Optional[list[dict[str, Any]]],
        records: list,
    ) -> str:
        """对「预测分析 · 重点关注」给出一段专业级 LLM 建议（回归走向 + 下一步）。

        输入为风险模型的 ``plan.to_dict()`` 结果、经验回退的重点接口表，以及本次
        运行记录。LLM 不可用/失败时退回确定性计数建议，绝不中断报告生成。
        """
        focused = list(plan_dict.get("focused") or [])
        for k in key_interfaces or []:
            if not any(f["interface"] == k["interface"] for f in focused):
                focused.append(
                    {
                        "interface": k["interface"],
                        "risk": None,
                        "confidence": None,
                        "failures": k.get("failures", 0),
                        "total": None,
                        "top_failure_classes": [],
                        "_source": "historical",
                    }
                )
        if not focused:
            return ""
        focus_txt = "\n".join(
            f"- {f['interface']}: 风险={f.get('risk')}, 置信={f.get('confidence')}, "
            f"失败次数={f.get('failures')}"
            + (f", 类别={f.get('top_failure_classes')}" if f.get("top_failure_classes") else "")
            for f in focused
        )
        cur = "、".join(f"{r.name}({r.result})" for r in records) or "（无）"
        prompt = (
            f"你是一名资深 API 自动化测试/质量工程专家。以下是基于历史运行的风险模型"
            f"（回归必测接口）与本次巡检结果。请输出专业的「预测分析」与下一步建议，"
            f"要求：\n"
            f"1) next_cases：1-2 条建议优先回归的接口/方向及其理由；\n"
            f"2) attention：1-3 条需要重点排查/新增断言/稳定性治理的项；\n"
            f"3) strategy：一句整体质量策略建议（如先回归冒烟还是直接全量）。\n"
            f"风险模型(interface: 风险/置信/失败次数/失败类别):\n{focus_txt}\n"
            f"本次运行(名称:结果):\n{cur}\n"
            f"仅输出 JSON 对象，三个键: next_cases(list[str]), attention(list[str]), "
            f"strategy(str)。"
        )
        try:
            data = self._llm.judge("predict-advice", prompt, model="cheap")
        except Exception:
            return self._predict_fallback(focused)
        if not isinstance(data, dict):
            return self._predict_fallback(focused)
        lines = []
        strategy = data.get("strategy") or ""
        if strategy:
            lines.append(f"- **整体策略**：{strategy}")
        nxt = data.get("next_cases") or []
        if nxt:
            lines.append("- **建议优先回归**：")
            lines += [f"  - {n}" for n in nxt]
        att = data.get("attention") or []
        if att:
            lines.append("- **重点关注/排查**：")
            lines += [f"  - {a}" for a in att]
        return "\n".join(lines)

    def _predict_fallback(self, focused: list[dict[str, Any]]) -> str:
        """LLM 关闭/失败时的确定性预测建议回退。"""
        tops = "、".join(f.get("interface", "?") for f in focused[:5])
        lines = [f"- **建议优先回归**：`{tops}`（按风险模型/历史失败排序）。"]
        lines.append("- 启用 LLM（配置 api_key 且未置 LLMOFF）后自动生成专业预测分析。")
        return "\n".join(lines)
