"""AI 扩展助手：变更影响分析、NL→用例、失败聚类、覆盖缺口、用例质量评分。

这是 ``ah.predict`` 预测分析层的*上层*助手集。每个 agent 都是 **helper**：
- LLM 只负责*提议/评分*，执行与验证始终由框架确定性完成。
- :func:`ah.core.orchestrator.run_single` 是对外部提议的唯一验证器——
  生成/提供的用例必须重跑通过才被接受（与 :class:`Predictor` 同一闭环，不叠加自愈）。
- LLM 被禁用（``LLMOFF`` / 无 key）时不发网络，返回确定性空/固定结果——hermetic。

五类能力：
- :class:`AgentImpact` — 输入接口变更（diff/Swagger/提交说明）+ 历史 → 影响接口 +
  受影响断言路径 + 影响分。
- :class:`AgentNL2Case` — 自然语言 → 结构化的 YAML 用例（走 schema 校验 +
  ``run_single`` 验真；带人审护栏）。
- :class:`AgentCluster` — 历史失败记录 → 语义失败族（标题/成因/成员/修复建议）。
- :class:`AgentCoverage` — 现有用例集 → 未覆盖边界 → 新候选用例（验真后返回）。
- :class:`AgentQuality` — 每个用例的可靠性评分（断言有效性/参数覆盖/维护成本）。

约定：所有 ``judge`` 用 ``strong`` 模型（决策类）；``_llm is None`` 即刻返回空。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ah.core.orchestrator import LLMHooks, run_single
from ah.harness.http import HTTPClient
from ah.harness.loader import Case
from ah.predict.generator import GeneratedCase
from ah.utils.context import Context
from ah.utils.logging import get_logger

log = get_logger("predict.assist")


# ───────────────────────── AgentImpact ─────────────────────────
class AgentImpact:
    """基于接口变更（diff/Swagger/提交说明）预测受影响接口与断言路径。

    Hermetic：``llm is None`` 时返回空列表 ``[]``。
    """

    def __init__(self, llm: Any = None) -> None:
        self._llm = llm

    def analyze(
        self,
        *,
        cases: Optional[list[Case]] = None,
        history: Optional[list[dict[str, Any]]] = None,
        changes: str = "",
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """受影响接口列表（每项 interface/affected_assert_paths[]/impact_score）。"""
        if self._llm is None:
            return []

        cases = cases or []
        history = history or []
        interface_keys = sorted({c.request_path for c in cases} | {
            h.get("interface", "") for h in history if h.get("interface")
        })
        case_lines = [
            f"- {c.name}: {c.request.get('method', 'GET')} {c.request_path} "
            f"assert={c.assert_}"
            for c in cases
        ][:40]
        fail_lines = [
            f"- {h.get('interface', '')} result={h.get('result')} "
            f"failure_class={h.get('failure_class')} "
            f"msg={(h.get('failure_msg') or '')[:160]}"
            for h in history
            if h.get("result") != "PASS"
        ][:20]

        prompt = (
            "You analyze impact of a service change on an API test suite. "
            "Given the interfaces, existing cases, recent failures, and a change "
            "description (commit/diff/Swagger), list which interfaces are LIKELY to be "
            "impacted and which assertion paths may break.\n"
            f"INTERFACES: {', '.join(interface_keys)}\n"
            f"CASES:\n" + ("\n".join(case_lines) or "  (none)\n") +
            "\nRECENT FAILURES:\n" + ("\n".join(fail_lines) or "  (none)\n") +
            f"\nCHANGE_DESCRIPTION:\n{changes or '  (none provided)'}\n"
            f"Return JSON: {{\"interfaces\": [{{\"interface\": str, "
            '"affected_assert_paths": [str], "impact_score": number 0..1, '
            '"rationale": str}}]}}. Include only interfaces from the list given. '
            "Respond only with the JSON object."
        )
        try:
            data = self._llm.judge("impact", prompt, model="strong")
        except Exception as exc:
            log.warning("impact llm call failed: %s", exc)
            return []
        raw = data.get("interfaces")
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw[:top_k]:
            if not isinstance(item, dict) or not item.get("interface"):
                continue
            score = item.get("impact_score")
            try:
                score_f = float(score) if score else 0.0
            except (TypeError, ValueError):
                score_f = 0.0
            out.append(
                {
                    "interface": str(item["interface"]),
                    "affected_assert_paths": [
                        str(p) for p in (item.get("affected_assert_paths") or [])
                    ],
                    "impact_score": min(1.0, max(0.0, score_f)),
                    "rationale": str(item.get("rationale") or ""),
                }
            )
        return out


# ───────────────────────── AgentNL2Case ─────────────────────────
@dataclass
class NL2CaseResult:
    """从自然语言解析出的用例。``validated`` 表示是否已通过 ``run_single``。"""

    generated: GeneratedCase
    needs_human_review: bool = True  # 人审护栏：任何外部提议默认需人审
    review_reason: str = "externally proposed; human review recommended before persisting"


class AgentNL2Case:
    """把自然语言转成可执行的 YAML 用例。

    - LLM 提议一个 ``Case``，强制保留可信断言字段。
    - 若提供 ``client``，用 ``run_single`` 验真；否则仅返回结构化草案（``validated=False``）。
    - 产物始终带 ``needs_human_review=True``（人审护栏）——LLM 绝非无人监督的写入者。
    """

    def __init__(
        self,
        llm: Any = None,
        *,
        client: Optional[HTTPClient] = None,
        ctx: Optional[Context] = None,
    ) -> None:
        self._llm = llm
        self._client = client
        self._ctx = ctx

    def to_case(
        self,
        text: str,
        *,
        existing: Optional[list[Case]] = None,
        validate: bool = True,
        gate: Any = None,
    ) -> Optional[NL2CaseResult]:
        """返回解析出的用例；LLM 禁用时返回 None。"""
        if self._llm is None:
            return None

        existing = existing or []
        existing_lines = [
            f"- {c.name}: {c.request.get('method', 'GET')} {c.request_path} "
            f"assert={c.assert_}"
            for c in existing
        ][:30]
        prompt = (
            "You convert a natural-language request into ONE API test case. "
            "The case must have request.method, request.path (absolute URL or "
            "relative to existing base). Include assert.status_code when sensible "
            "and optional params/json/extract.\n"
            "EXISTING CASES (style reference):\n"
            + ("\n".join(existing_lines) or "  (none)\n") +
            f"\nUSER REQUEST: {text}\n"
            "Return JSON: {\"name\": str, \"request\": {\"method\": str, \"path\": str, "
            '"params": {…} optional, "json": {…} optional, "headers": {…} optional}, '
            '"assert": {…} optional, "extract": {…} optional}. '
            "Respond only with the JSON object."
        )
        try:
            data = self._llm.judge("nl2case", prompt, model="strong")
        except Exception as exc:
            log.warning("nl2case llm call failed: %s", exc)
            return None
        if not isinstance(data, dict):
            return None
        try:
            gen = GeneratedCase(
                name=str(data.get("name") or "nl-case"),
                request=dict(data.get("request") or {}),
                assert_=dict(data.get("assert") or {}),
                extract=dict(data.get("extract") or {}),
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("nl2case invalid candidate: %s", exc)
            return None
        if not gen.request.get("path"):
            return None

        result = NL2CaseResult(generated=gen)
        # LayaGate design 信号注记（默认关，不改判定路径、不短路生成）：
        # AUTO 高置信"请求清晰可生成"→ 记入 review_reason；生成仍归 LLM，人审护栏不变。
        if gate is not None:
            try:
                dec = gate.decide(
                    "design",
                    text,
                    {
                        "clear_enough": {
                            "type": "noul",
                            "instructions": (
                                "该自然语言请求是否足够清晰、"
                                "可转换为一个 API 测试用例？"
                            ),
                        }
                    },
                )
                if (
                    dec.action.value == "auto"
                    and isinstance(dec.value, dict)
                    and float(dec.value.get("clear_enough") or 0.0) >= 0.5
                ):
                    result.review_reason = (
                        "laya design ok: 请求清晰可生成; human review still recommended"
                    )
            except Exception:  # Laya 故障绝不破坏原流程
                pass
        if validate and self._client is not None:
            ctx = self._ctx or Context()
            step = run_single(gen.to_case(), ctx, self._client, hooks=LLMHooks())
            if step.succeeded:
                gen.validated = True
                result.generated = gen
            else:
                gen.validation_error = str(step.error)
        return result


# ───────────────────────── AgentCluster ─────────────────────────
class AgentCluster:
    """把历史失败聚成语义失败族（供 triage / heal / 报告复用）。"""

    def __init__(self, llm: Any = None) -> None:
        self._llm = llm

    def cluster(
        self, history: list[dict[str, Any]], *, max_clusters: int = 8
    ) -> list[dict[str, Any]]:
        """返回失败族列表：``[{id, title, members[], root_cause, fix_suggestion}]``。"""
        if self._llm is None:
            return []
        failures = [h for h in history if h.get("result") != "PASS"][:60]
        if not failures:
            return []
        rows = []
        for i, h in enumerate(failures):
            triage = h.get("llm_notes")
            triage = triage if isinstance(triage, dict) else {}
            cause = triage.get("root_cause") if isinstance(triage.get("triage"), dict) else None
            rows.append(
                f"{i}: {h.get('interface','')} [{h.get('failure_class','')}] "
                f"{(cause or h.get('failure_msg') or '')[:120]}"
            )
        prompt = (
            "Group these API test failures into semantic families (same root cause). "
            "Each family gets a title, the member indices, a root cause, and a fix "
            "suggestion for the test or code.\nFAILURES:\n"
            + "\n".join(rows)
            + f"\nReturn JSON: {{\"clusters\": [{{\"id\": str, \"title\": str, "
            '"member_indices": [int], "root_cause": str, "fix_suggestion": str}]}}. '
            f"At most {max_clusters} clusters. Respond only with the JSON object."
        )
        try:
            data = self._llm.judge("cluster", prompt, model="strong")
        except Exception as exc:
            log.warning("cluster llm call failed: %s", exc)
            return []
        raw = data.get("clusters")
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw[:max_clusters]:
            if not isinstance(item, dict):
                continue
            members = []
            for idx in (item.get("member_indices") or []):
                try:
                    i = int(idx)
                    if 0 <= i < len(failures):
                        members.append(failures[i])
                except (TypeError, ValueError):
                    continue
            if not members:
                continue
            out.append(
                {
                    "id": str(item.get("id") or ""),
                    "title": str(item.get("title") or ""),
                    "members": members,
                    "root_cause": str(item.get("root_cause") or ""),
                    "fix_suggestion": str(item.get("fix_suggestion") or ""),
                }
            )
        return out


# ───────────────────────── AgentCoverage ─────────────────────────
class AgentCoverage:
    """找出测试盲区并提议未覆盖边界用例（复用 Predictor 验证闭环）。"""

    def __init__(
        self,
        llm: Any = None,
        *,
        client: Optional[HTTPClient] = None,
        ctx: Optional[Context] = None,
    ) -> None:
        self._llm = llm
        self._client = client
        self._ctx = ctx

    def propose(
        self,
        cases: list[Case],
        *,
        spec: str = (
            "required-field-missing, enum-out-of-range, pagination, concurrency, permission"
        ),
        limit: int = 3,
    ) -> list[GeneratedCase]:
        """返回**已验证**的覆盖缺口候选；LLM 禁用或无法验证时返回 []。"""
        if self._llm is None or self._client is None:
            return []
        case_lines = [
            f"- {c.name}: {c.request.get('method','GET')} {c.request_path} "
            f"assert={c.assert_}"
            for c in cases
        ][:40]
        if not case_lines:
            return []
        interfaces = sorted({c.request_path for c in cases})
        prompt = (
            "Find UNTESTED edge/boundary cases for these API interfaces. Focus on "
            f"coverage gaps: {spec}.\n"
            f"EXISTING CASES:\n" + ("\n".join(case_lines) or "  (none)\n") +
            f"\nINTERFACES: {', '.join(interfaces)}\n"
            "Propose NEW cases that are NOT already covered.\n"
            "Return JSON: {\"cases\": [{\"name\": str, \"request\": {\"method\": str, "
            '"path": str, "params": {…} optional, "json": {…} optional}, '
            '"assert": {…} optional}]}. Keep request.method/path matching a listed '
            "interface. Respond only with the JSON object."
        )
        try:
            data = self._llm.judge("coverage", prompt, model="strong")
        except Exception as exc:
            log.warning("coverage llm call failed: %s", exc)
            return []
        raw = data.get("cases")
        if not isinstance(raw, list):
            return []
        ctx = self._ctx or Context()
        hooks = LLMHooks()  # 验证不叠加自愈
        accepted: list[GeneratedCase] = []
        for item in raw[:limit]:
            try:
                gen = GeneratedCase(
                    name=str(item.get("name") or item["request"].get("path")),
                    request=dict(item["request"]),
                    assert_=dict(item.get("assert") or {}),
                    extract=dict(item.get("extract") or {}),
                )
            except Exception as exc:
                log.warning("coverage invalid candidate: %s", exc)
                continue
            if not gen.request.get("path"):
                continue
            step = run_single(gen.to_case(), ctx, self._client, hooks=hooks)
            if step.succeeded:
                gen.validated = True
                accepted.append(gen)
            else:
                gen.validation_error = str(step.error)
        return accepted


# ───────────────────────── AgentQuality ─────────────────────────
class AgentQuality:
    """为每条用例打分（断言有效性 / 参数覆盖 / 维护成本），供报告防"用例腐化"。"""

    def __init__(self, llm: Any = None) -> None:
        self._llm = llm

    def score(
        self, cases: list[Case], *, max_cases: int = 100
    ) -> list[dict[str, Any]]:
        """每条用例评分：断言有效性 / 参数覆盖 / 维护成本。"""
        if self._llm is None:
            return []
        case_lines = [
            f"- {c.name}: {c.request.get('method','GET')} {c.request_path} "
            f"assert={c.assert_} extract={c.extract} vars={c.vars_req}"
            for c in cases
        ][:max_cases]
        if not case_lines:
            return []
        prompt = (
            "Score each API test case for reliability. Assess: "
            "assertion_effectiveness (does the assert actually catch regressions, "
            "or is it too loose/tautological), param_coverage (does it exercise "
            "boundaries/edge types), maintainability (hardcoded values vs variables, "
            "clear naming). Score each 0..1.\n"
            "CASES:\n" + "\n".join(case_lines) +
            "\nReturn JSON: {\"reviews\": [{\"case\": str, \"score\": number 0..1, "
            '"assertion_effectiveness": number, "param_coverage": number, '
            '"maintainability": number, "issues": [str], "suggestion": str}]}. '
            "Respond only with the JSON object."
        )
        try:
            data = self._llm.judge("quality", prompt, model="strong")
        except Exception as exc:
            log.warning("quality llm call failed: %s", exc)
            return []
        raw = data.get("reviews")
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("case"):
                continue
            score = item.get("score")
            out.append(
                {
                    "case": str(item["case"]),
                    "score": round(min(1.0, max(0.0, float(score))), 3) if score else 0.0,
                    "assertion_effectiveness": float(item.get("assertion_effectiveness") or 0.0),
                    "param_coverage": float(item.get("param_coverage") or 0.0),
                    "maintainability": float(item.get("maintainability") or 0.0),
                    "issues": [str(i) for i in (item.get("issues") or [])],
                    "suggestion": str(item.get("suggestion") or ""),
                }
            )
        return out
