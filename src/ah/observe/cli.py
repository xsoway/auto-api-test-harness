#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15 20:10
# @Filename : cli.py
# @Author   : Alan_Hsu
"""``ah observe`` 子命令：本地类 Langfuse 可观测平台。

子命令：
- ``ah observe serve  [--db PATH] [--port N] [--host H] [--web DIR]``  启动 HTTP 服务
- ``ah observe ingest [--db PATH] <report_dir> [--name PREFIX]``       把报告目录摄入 SQLite
- ``ah observe seed   [--db PATH] [--runs N]``                         灌入合成演示数据
- ``ah observe llm-detail ...``  写一条 LLM 明细（演示/测试用）

挂载到 :mod:`ah.cli` 的 ``_MODULES``，子命令用 ``set_defaults(func=...)``。
Hermetic 安全：零网络、LLMOFF 可测。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ah.observe.ingest import ingest_report_dir, seed_demo
from ah.observe.llmdetail import record_llm_detail
from ah.observe.store import open_store
from ah.utils.logging import get_logger

log = get_logger("observe.cli")

#: 默认数据库路径（相对 cwd；可用 --db 覆盖）。
DEFAULT_DB = "local-obs.sqlite3"


def register(subparsers: argparse._SubParsersAction) -> None:
    """把 ``observe`` 子命令挂到顶层 argparse。"""
    p = subparsers.add_parser(
        "observe",
        help="本地类 Langfuse 可观测平台（服务+DB+前端）。",
        description="启动本地可观测 HTTP 服务 / 摄入报告数据 / 灌入演示数据。",
    )
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 路径（默认 {DEFAULT_DB}）")
    sub = p.add_subparsers(dest="observe_cmd", metavar="<action>")

    s_serve = sub.add_parser("serve", help="启动本地 HTTP 服务。")
    s_serve.add_argument("--host", default="127.0.0.1")
    s_serve.add_argument("--port", type=int, default=8111)
    s_serve.add_argument("--web", default=None,
                         help="前端构建产物目录（默认 web/dist，若存在）。")
    s_serve.set_defaults(func=_serve)

    s_ingest = sub.add_parser("ingest", help="把报告目录摄入 SQLite。")
    s_ingest.add_argument("report_dir", help="report 目录（含 report_history.jsonl 等）。")
    s_ingest.add_argument("--name", default="run", help="run 名称前缀。")
    s_ingest.set_defaults(func=_ingest)

    s_seed = sub.add_parser("seed", help="灌入合成演示数据（零网络）。")
    s_seed.add_argument("--runs", type=int, default=3)
    s_seed.set_defaults(func=_seed)

    s_detail = sub.add_parser("llm-detail", help="写一条 LLM 明细（演示/联调用）。")
    s_detail.add_argument("--model", default="gpt-4o-mini")
    s_detail.add_argument("--task", default="demo")
    s_detail.add_argument("--report-dir", default=".")
    s_detail.add_argument("--input", default="示例提示词")
    s_detail.add_argument("--output", default="示例响应")
    s_detail.add_argument("--prompt-tokens", type=int, default=100)
    s_detail.add_argument("--completion-tokens", type=int, default=50)
    s_detail.add_argument("--cost", type=float, default=0.00015)
    s_detail.set_defaults(func=_llm_detail)


def _serve(args: argparse.Namespace) -> int:
    from ah.observe.server import serve

    store = open_store(args.db)
    serve(store, host=args.host, port=args.port, web_dir=args.web, block=True)
    store.close()
    return 0


def _ingest(args: argparse.Namespace) -> int:
    store = open_store(args.db)
    counts = ingest_report_dir(store, Path(args.report_dir), name_prefix=args.name)
    print(f"已摄入 {args.report_dir}:")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print(f"总计: {store.counts()}")
    store.close()
    return 0


def _seed(args: argparse.Namespace) -> int:
    store = open_store(args.db)
    counts = seed_demo(store, runs=args.runs)
    print(f"已灌入演示数据: {counts}")
    store.close()
    return 0


def _llm_detail(args: argparse.Namespace) -> int:
    path = record_llm_detail(
        model=args.model,
        task=args.task,
        cost=args.cost,
        prompt_tokens=args.prompt_tokens,
        completion_tokens=args.completion_tokens,
        input_text=args.input,
        output_text=args.output,
        report_dir=args.report_dir,
    )
    print(f"已写入 LLM 明细: {path}")
    return 0
