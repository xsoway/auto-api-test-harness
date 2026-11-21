"""基于失败模式/历史的 LLM 测试数据生成器（Predictor）。

:class:`Predictor` 把"预测生成新的测试数据并自动再跑"落成可复现闭环：

1. 收集输入：现有用例集 + 历史失败记录（含 triage 根因/失败类别）。
2. 让 LLM（配置里的强模型）基于这些信息提议**新候选用例**（边界值、
   对抗输入、失败的相邻参数组合等），输出结构化 JSON。
3. 对每个候选执行 :func:`ah.core.orchestrator.run_single` **重新验证**；
   仅接受重跑通过（或按 ``accept_passes``）的候选。
4. 返回通过的候选，供上层回写用例集。

LLM 被禁用（``LLMOFF`` 或无 key）时不发网络，返回确定性空结果——hermetic
套件零依赖。LLM 只作为*提议器*，执行与验证始终由框架确定性完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ah.core.orchestrator import LLMHooks, run_single
from ah.harness.http import HTTPClient
from ah.harness.loader import Case
from ah.utils.context import Context
from ah.utils.logging import get_logger

log = get_logger("predict")


@dataclass
class GeneratedCase:
    """LLM 提议并通过验证的新增用例。"""

    name: str
    request: dict[str, Any]
    assert_: dict[str, Any] = field(default_factory=dict)
    extract: dict[str, Any] = field(default_factory=dict)
    validated: bool = False
    validation_error: Optional[str] = None

    def to_case(self) -> Case:
        """转成可直接落地的 :class:`Case`（保留 request 的 method/path）。"""
        req = dict(self.request)
        return Case(
            name=self.name,
            request=req,
            extract=dict(self.extract or {}),
            assert_=dict(self.assert_ or {}),
        )

    def to_yaml_mapping(self) -> dict[str, Any]:
        """按照 loader 接受的单个用例映射格式输出。"""
        mapping: dict[str, Any] = {"name": self.name, "request": dict(self.request)}
        if self.assert_:
            mapping["assert"] = dict(self.assert_)
        if self.extract:
            mapping["extract"] = dict(self.extract)
        return mapping


class Predictor:
    """LLM 驱动的测试数据生成器，带确定性验证闭环。

    ``llm`` 为 :class:`ah.llm.base.BaseLLM`（或 None 表示禁用）。
    ``traits`` 是可选的"新数据倾向"提示（例如默认倾向边界值/对抗输入）。
    """

    def __init__(
        self,
        llm: Any = None,
        *,
        client: Optional[HTTPClient] = None,
        ctx: Optional[Context] = None,
        traits: str = "prefer boundary values, adversarial inputs, and NULL/empty edge cases",
    ) -> None:
        self._llm = llm
        self._client = client
        self._ctx = ctx
        self.traits = traits

    def propose(
        self,
        cases: list[Case],
        history: list[dict[str, Any]],
        *,
        limit: int = 3,
        max_heal_attempts: int = 2,
    ) -> list[GeneratedCase]:
        """生成新候选用例并逐个验证，返回通过（或按 accept 策略接收）的候选。

        Hermetic：LLM 禁用时返回空列表，不调用任何网络。
        """
        if self._llm is None:
            return []
        if self._client is None:
            raise ValueError("Predictor.propose requires a client to validate candidates")

        # 1) 构造输入上下文：现有接口清单 + 失败样本。
        interface_keys = {c.request_path for c in cases}
        interface_keys |= {
            h.get("interface", "") for h in history if h.get("interface")
        }
        interfaces = sorted(interface_keys)
        failures = [
            f
            for f in history
            if f.get("result") != "PASS"
            and (f.get("failure_msg") or f.get("llm_notes"))
        ][:20]

        prompt = self._build_prompt(cases, interfaces, failures, limit)

        try:
            data = self._llm.judge("predict", prompt, model="strong")
        except Exception as exc:  # 模型失败当作空结果，绝不把 LLM 故障变成测试失败。
            log.warning("predictor llm call failed: %s", exc)
            return []

        raw_candidates = data.get("cases")
        if not isinstance(raw_candidates, list):
            return []

        ctx = self._ctx or Context()
        hooks = LLMHooks()  # 生成验证不叠加自愈，避免把"生成即失败"误判为通过。
        accepted: list[GeneratedCase] = []
        for raw in raw_candidates[:limit]:
            try:
                gen = self._to_generated(raw)
            except Exception as exc:
                log.warning("invalid candidate %r: %s", raw, exc)
                continue
            case = gen.to_case()
            result = run_single(case, ctx, self._client, hooks=hooks)
            if result.succeeded:
                gen.validated = True
                accepted.append(gen)
            else:
                gen.validation_error = str(result.error)
                log.info("candidate %r failed validation: %s", gen.name, gen.validation_error)
        return accepted

    def _build_prompt(
        self,
        cases: list[Case],
        interfaces: list[str],
        failures: list[dict[str, Any]],
        limit: int,
    ) -> str:
        case_lines = []
        for c in cases[:40]:
            case_lines.append(
                f"- {c.name}: {c.request.get('method','GET')} {c.request_path} "
                f"assert={c.assert_}"
            )
        fail_lines = []
        for f in failures:
            notes = f.get("llm_notes")
            notes = notes if isinstance(notes, dict) else {}
            triage = notes.get("triage")
            cause = triage.get("root_cause") if isinstance(triage, dict) else None
            fail_lines.append(
                f"- {f.get('interface','')} result={f.get('result')} "
                f"failure_class={f.get('failure_class')} "
                f"cause={(cause or f.get('failure_msg') or '')[:200]}"
            )
        return (
            f"You are a test-data generator for an API test harness. Based on the "
            f"existing cases and observed failures below, propose {limit} NEW test case "
            f"scenarios that would likely surface bugs ({self.traits}).\n"
            f"EXISTING INTERFACES: {', '.join(interfaces)}\n"
            f"EXISTING CASES:\n" + ("\n".join(case_lines) or "  (none)\n") +
            "\nOBSERVED FAILURES:\n" + ("\n".join(fail_lines) or "  (none)\n") +
            "\nReturn JSON: {\"cases\": [{\"name\": str, \"request\": {\"method\": str, "
            '"path": str, "params": {…} optional, "json": {…} optional}, '
            '"assert": {…} optional, "extract": {…} optional}]}. '
            "Keep request.method and request.path consistent with existing interfaces. "
            "Respond only with the JSON object."
        )

    @staticmethod
    def _to_generated(raw: Any) -> GeneratedCase:
        if not isinstance(raw, dict):
            raise ValueError("candidate must be a dict")
        req = raw.get("request")
        if not isinstance(req, dict) or not req.get("path"):
            raise ValueError("candidate request must have path")
        return GeneratedCase(
            name=str(raw.get("name") or req.get("path")),
            request=req,
            assert_=raw.get("assert") or {},
            extract=raw.get("extract") or {},
        )
