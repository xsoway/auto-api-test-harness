"""auto-api-test-harness 包——分层 API 测试框架。

分层结构（镜像 Eval-Anything 参考结构）：
- ``ah.core``       —— 配置、编排（runner）、追踪、多 agent 意图调度
- ``ah.harness``    —— HTTP 传输 + 用例加载
- ``ah.environment``—— 认证/令牌生命周期
- ``ah.llm``        —— BaseLLM 接口、OpenAI 驱动、mock、agents
- ``ah.metrics``    —— 断言 + 响应提取
- ``ah.reporting``  —— 结果记录 + 报告写入
- ``ah.utils``      —— 上下文插值、日志、JSON 辅助工具

``ah.core.plugin`` 承载 pytest 插件入口；``ah.core.dispatch`` 是多 agent
意图识别调度入口（用户意图 → 对应 helper agent / 编排器）。
"""

from ah.core.config import Settings, load_settings, settings
from ah.core.dispatch import DispatchResult, Intent, IntentRouter, get_router, route_intent
from ah.core.orchestrator import LLMHooks, StepResult, run_flow, run_single
from ah.harness.http import APIResponse, HTTPClient
from ah.utils.context import Context, ContextManager, render
from ah.utils.logging import configure_logging, get_logger

__version__ = "0.1.0"

__all__ = [
    "APIResponse",
    "HTTPClient",
    "Settings",
    "load_settings",
    "settings",
    "Context",
    "ContextManager",
    "render",
    "LLMHooks",
    "StepResult",
    "run_single",
    "run_flow",
    "configure_logging",
    "get_logger",
    "Intent",
    "DispatchResult",
    "IntentRouter",
    "get_router",
    "route_intent",
]
