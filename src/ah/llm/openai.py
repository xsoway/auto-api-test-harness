"""兼容 OpenAI 的 LLM 驱动（OpenRouter 端点）。

:class:`LLMClient` 封装官方 ``openai`` SDK 并实现
:class:`ah.llm.base.BaseLLM` helper 接口。它是 *helper*，绝非测试驱动。两种形态：

- :meth:`generate` — 自由形式补全 -> ``str``
- :meth:`judge` — 结构化补全 -> 单个合法 JSON 对象

构造函数守卫：当设置了 ``LLMOFF`` 或未配置 API key 时抛出
:class:`RuntimeError`，以便 agents 检测到独立性并回退到隔离 fixtures。
模型路由为数据生成任务选择 ``model_cheap``，为三分类/自愈/报告分析选择
``model_strong``。用量与延迟会写入已配置的 tracer。
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from ah.core.config import settings
from ah.core.trace import get_tracer
from ah.utils.json_utils import robust_json_parse
from ah.utils.logging import get_logger

from .base import BaseLLM, LLMConfig

log = get_logger("llm")

_SYSTEM_JSON = (
    "You are a precise JSON-producing assistant. "
    "Respond only with a single valid JSON object and nothing else."
)


def llm_enabled() -> bool:
    """当允许发起真实 LLM 调用时返回 True（未设置 LLMOFF 且存在 key 时）。"""
    off = os.getenv("LLMOFF", "").strip() in {"1", "true", "yes", "on"}
    return not (off or not settings.llm.api_key)


class LLMDisabledError(RuntimeError):
    """在禁用状态下尝试使用 LLM 时抛出（隔离模式）。"""


class LLMClient(BaseLLM):
    """实现 BaseLLM helper 接口的兼容 OpenAI 的客户端。"""

    def __init__(
        self,
        *,
        model_cheap: Optional[str] = None,
        model_strong: Optional[str] = None,
        extra_client: Any = None,
        tracer: Any = None,
        config: Optional[LLMConfig] = None,
    ) -> None:
        if not llm_enabled():
            raise LLMDisabledError("LLM disabled (LLMOFF set or no API key)")
        super().__init__(config)
        self.model_cheap = model_cheap or settings.llm.model_cheap
        self.model_strong = model_strong or settings.llm.model_strong
        self._tracer = tracer or get_tracer()
        try:
            from openai import OpenAI  # type: ignore

            self._client = extra_client or OpenAI(
                base_url=settings.llm.provider_base_url,
                api_key=settings.llm.api_key,
                timeout=settings.llm.timeout,
            )
        except ImportError as exc:
            raise LLMDisabledError(f"openai SDK not installed: {exc}") from exc

    # ---- 模型路由 ----
    def _model(self, model: str) -> str:
        if model == "cheap":
            return self.model_cheap
        if model == "strong":
            return self.model_strong
        return model

    # ---- generate ----
    def generate(self, task: str, prompt: str, *, model: str = "cheap") -> str:
        """自由形式补全；返回助手文本。"""
        start = time.perf_counter()
        span = self._tracer.start_llm(task, self._model(model))
        try:
            resp = self._client.chat.completions.create(
                model=self._model(model),
                messages=[
                    {"role": "system", "content": "You are a helpful API-testing assistant."},
                    {"role": "user", "content": prompt},
                ],
            )
            text = resp.choices[0].message.content or ""
        except Exception as exc:
            raise RuntimeError(f"LLM generate failed ({task}): {exc}") from exc
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            usage = None
            if "resp" in locals():
                u = resp.usage  # type: ignore[union-attr]
                if u is not None:
                    usage = {
                        "prompt_tokens": u.prompt_tokens,
                        "completion_tokens": u.completion_tokens,
                        "total_tokens": u.total_tokens,
                    }
            span.finish(
                prompt=prompt, response=text, usage=usage, latency_ms=latency_ms
            )
            log.info("LLM %s -> %d chars (%dms)", self._model(model), len(text), latency_ms)
        # 遥测落账：每次真实 LLM 调用写 llm_usage.jsonl（成本聚合/报告「LLM 成本」段
        # 的数据源）。失败仅告警绝不中断调用；report_dir 传 None 以优先运行期
        # AH_REPORT_DIR（业务项目报告目录），未设则退回 settings.report_dir。
        try:
            from ah.telemetry import record_llm_call

            record_llm_call(
                model=self._model(model),
                task=task,
                status="success" if "resp" in locals() else "error",
                cost=0.0,  # 无价格表，成本按 0 记账；调用计数/成功率仍真实。
                prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                completion_tokens=(usage or {}).get("completion_tokens", 0),
                latency_ms=latency_ms,
                report_dir=None,  # None → 运行时 AH_REPORT_DIR / settings.report_dir。
            )
        except Exception as exc:  # noqa: BLE001 - 遥测落账失败不影响主调用。
            log.debug("LLM telemetry record skipped: %s", exc, exc_info=True)
        return text

    # ---- judge（仅 JSON）----
    def judge(self, task: str, prompt: str, *, model: str = "strong") -> dict[str, Any]:
        """结构化补全，返回单个 JSON 对象。"""
        start = time.perf_counter()
        span = self._tracer.start_llm(f"{task}:json", self._model(model))
        try:
            resp = self._client.chat.completions.create(
                model=self._model(model),
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM_JSON},
                    {"role": "user", "content": prompt},
                ],
            )
            text = resp.choices[0].message.content or ""
            parsed = robust_json_parse(text)
            if parsed is None:
                raise RuntimeError(f"LLM judge {task}: response not valid JSON: {text[:200]}")
        except Exception as exc:
            raise RuntimeError(f"LLM judge failed ({task}): {exc}") from exc
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            span.finish(prompt=prompt, response=text, latency_ms=latency_ms)
            log.info("LLM judge %s (%dms)", self._model(model), latency_ms)
        # 遥测落账：judge 也写 llm_usage.jsonl（成本/成功率口径与 generate 一致）。
        try:
            from ah.telemetry import record_llm_call

            record_llm_call(
                model=self._model(model),
                task=f"{task}:json",
                status="success",
                cost=0.0,
                latency_ms=latency_ms,
                report_dir=None,  # None → 运行时 AH_REPORT_DIR / settings.report_dir。
            )
        except Exception as exc:  # noqa: BLE001 - 遥测落账失败不影响主调用。
            log.debug("LLM telemetry record skipped: %s", exc, exc_info=True)
        return parsed
