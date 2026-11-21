#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/08 18:20
# @Filename : cli.py
# @Author   : Alan_Hsu
"""统一 CLI 入口：把各功能子模块的命令挂到 argparse 并分发执行。

主代理（integration owner）在子模块落地后统一编写本文件。每个子模块暴露
``register(subparsers)``（把子命令挂到 subparsers）与执行入口（子命令通过
``set_defaults(func=...)`` 或 ``set_defaults(handler=...)`` 绑定）。本文件只负责：

1. 构造顶层 argument parser 与共享的 ``--debug``/``--llm-off`` 开关。
2. import 各子模块并调用其 ``register(subparsers)`` 挂载子命令。
3. 解析后按 ``args.func``（多数模块）或 ``args.handler``（evolution）分发到执行函数。
4. 统一返回非零退出码（判闸失败 / 用法错误），便于 CI 判闸。

可用方式：``python -m ah.cli <subcommand> ...``（或安装后 ``ah <subcommand>``）。
所有子命令在 ``LLMOFF=1`` 下都必须可运行（LLM 能力走 canned / 跳过，绝不网络依赖）。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any, Callable, Optional

from ah.utils.logging import configure_logging, get_logger

log = get_logger("ah.cli")

# 挂载顺序（体现 CLI 分组）；子模块都在此 import，缺一即报 ImportError 提示。
_MODULES: list[tuple[str, Callable[[Any], None]]] = [
    ("ah.evolution", lambda sp: _import("ah.evolution").register(sp)),
    ("ah.scaffold", lambda sp: _import("ah.scaffold").register(sp)),
    ("ah.scheduler", lambda sp: _import("ah.scheduler").register(sp)),
    ("ah.telemetry", lambda sp: _import("ah.telemetry").register(sp)),
    ("ah.property", lambda sp: _import("ah.property").register(sp)),
    ("ah.debug", lambda sp: _import("ah.debug").register(sp)),
    ("ah.scenario.cli", lambda sp: _import("ah.scenario.cli").register(sp)),
    ("ah.quality.cli", lambda sp: _import("ah.quality.cli").register(sp)),
    ("ah.coverage.cli", lambda sp: _import("ah.coverage.cli").register(sp)),
    ("ah.skill.cli", lambda sp: _import("ah.skill.cli").register(sp)),
    ("ah.observe.cli", lambda sp: _import("ah.observe.cli").register(sp)),
]


def _import(name: str) -> Any:
    """惰性 import：失败时给出清晰错误（依赖缺失 / 模块未就绪）。"""
    import importlib

    try:
        return importlib.import_module(name)
    except ImportError as exc:  # 缺可选依赖（如 hypothesis）时友好提示而非崩栈。
        raise SystemExit(f"无法加载模块 {name}: {exc}") from exc


def _dispatch(args: argparse.Namespace) -> int:
    """按子命令绑定的执行入口分发。

    兼容两种绑定约定：多数模块用 ``args.func``，evolution 用 ``args.handler``。
    两者都缺（异常 parser 状态）时返回 2（用法错误）。
    """
    runner: Optional[Callable[[argparse.Namespace], Any]] = getattr(
        args, "func", None
    ) or getattr(args, "handler", None)
    if runner is None:
        print(
            f"未识别的子命令/缺执行入口: {args!r}。请检查模块 register 绑定。",
            file=sys.stderr,
        )
        return 2
    return int(runner(args) or 0)


def build_parser() -> argparse.ArgumentParser:
    """构造顶层 parser 并挂载全部子模块子命令。"""
    parser = argparse.ArgumentParser(
        prog="ah",
        description="auto-api-test-harness 统一 CLI：自主进化 / 脚手架 / 调度 / 遥测 / 属性探索。",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="显示 DEBUG 日志（默认 INFO）。",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="显示框架版本。",
    )
    # 可选：统一关闭 LLM 的便捷开关（等价 LLMOFF=1）。
    parser.add_argument(
        "--llm-off",
        action="store_true",
        help="等价 LLMOFF=1：全部 LLM 能力走 canned/skip，零网络。",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<subcommand>")
    for _name, register_fn in _MODULES:
        register_fn(subparsers)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 主入口。返回进程退出码（0=成功，非0=失败/判闸/用法）。"""
    if argv is None:
        argv = sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)

    # --version：打印框架版本并退出。
    if getattr(args, "version", False):
        import ah

        print(f"auto-api-test-harness {ah.__version__}")
        return 0

    # --llm-off 便捷开关：设置环境变量后立即生效（子模块在解析时才读 llm_enabled()）。
    if getattr(args, "llm_off", False):
        os.environ["LLMOFF"] = "1"

    configure_logging()
    if getattr(args, "debug", False):
        logging.getLogger().setLevel(logging.DEBUG)

    # LayaGate 前置决策层（P0 激活）：AH_LAYA 开启且 laya-mlx venv 可用时，
    # 构造进程级 gate 并注入共享 IntentRouter（intent / nl2case 埋点自动生效）。
    # AH_LAYA=off（默认）或 venv 缺失 → gate=None → 原流程字节等价、零网络。
    from ah.core.dispatch import get_router
    from ah.evidence.laya_gate import get_gate

    get_router(gate=get_gate())

    if getattr(args, "command", None) is None:
        parser.print_help()
        return 2  # 无子命令 → 用法错误（非零，避免 CI 静默空跑）。

    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        print("\n用户中断。", file=sys.stderr)
        return 130
    except SystemExit as exc:  # argparse 的 --help / 参数错误
        return int(exc.code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
