"""LLM 抽象 — BaseLLM 接口与共享数据类。

所有具体 LLM 后端均继承 :class:`BaseLLM` 并实现本 harness 使用的两种
同步 helper 形态：

- ``generate(task, prompt, *, model) -> str``       — 自由形式补全
- ``judge(task, prompt, *, model) -> dict``         — 结构化的 JSON 对象

``model`` 是路由键（``"cheap"`` / ``"strong"`` / 具体模型 id）。harness 严格将
LLM 视为 *helper*：后端被禁用时，agents 中回退为 ``None``（隔离模式），或
在测试中回退为 :class:`MockLLM`。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class LLMConfig:
    """LLM 后端的静态配置。"""

    model_name: str
    endpoint_url: str = ""
    api_key: str = "EMPTY"
    temperature: float = 0.0
    max_tokens: int = 600
    top_p: float = 1.0
    timeout_seconds: int = 30
    extra_params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """单次 LLM 调用的统一结果。"""

    content: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    finish_reason: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


class BaseLLM(ABC):
    """实现 harness helper 接口的抽象 LLM 后端。"""

    def __init__(self, config: Optional[LLMConfig] = None) -> None:
        self.config = config

    @abstractmethod
    def generate(self, task: str, prompt: str, *, model: str = "cheap") -> str:
        """自由形式补全；返回助手文本。"""

    @abstractmethod
    def judge(self, task: str, prompt: str, *, model: str = "strong") -> Dict[str, Any]:
        """结构化补全，返回单个 JSON 对象。"""

    def close(self) -> None:
        """释放资源（子类在需要时覆写）。"""

    def __repr__(self) -> str:
        name = self.config.model_name if self.config is not None else "<?>"
        return f"<{self.__class__.__name__} model={name!r}>"
