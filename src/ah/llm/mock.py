"""MockLLM — 用于测试的隔离 LLM 后端，无网络依赖。

镜像 Eval-Anything 的 ``MockLLM`` 模式：预置响应从队列中取出，附带
``call_log`` 以便断言，且输出完全确定。用于在无任何真实模型的情况下
演练 ``llm`` 层与 agents。

与 harness 的 helper 接口（``generate`` / ``judge``）兼容，并额外提供
``set_response`` / ``set_json_response`` 用于脚本化。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ah.utils.json_utils import robust_json_parse

from .base import BaseLLM, LLMConfig


class MockLLM(BaseLLM):
    """确定性的 LLM 后端；通过 ``set_response`` 排队响应。"""

    def __init__(self, config: Optional[LLMConfig] = None) -> None:
        super().__init__(config)
        self._responses: List[str] = []
        self._call_log: List[Dict[str, Any]] = []

    # ── 脚本化 ──
    def set_response(self, content: str) -> None:
        """将下一段响应内容入队（FIFO）。"""
        self._responses.append(content)

    def set_json_response(self, obj: Dict[str, Any]) -> None:
        """将序列化为 JSON 文本的响应入队。"""
        self._responses.append(json.dumps(obj, ensure_ascii=False))

    @property
    def call_log(self) -> List[Dict[str, Any]]:
        """每一次调用的记录：task、prompt、模型路由键。"""
        return self._call_log

    @property
    def calls(self) -> int:
        return len(self._call_log)

    # ── BaseLLM 接口 ──
    def _next(self) -> str:
        return self._responses.pop(0) if self._responses else ""

    def generate(self, task: str, prompt: str, *, model: str = "cheap") -> str:
        self._call_log.append({"task": task, "prompt": prompt, "model": model, "kind": "generate"})
        return self._next()

    def judge(self, task: str, prompt: str, *, model: str = "strong") -> Dict[str, Any]:
        self._call_log.append({"task": task, "prompt": prompt, "model": model, "kind": "judge"})
        content = self._next()
        parsed = robust_json_parse(content)
        if parsed is None:
            raise RuntimeError(
                f"MockLLM judge {task}: queued response is not valid JSON: {content[:200]!r}"
            )
        return parsed
