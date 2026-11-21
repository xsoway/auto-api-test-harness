#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/15 16:00
# @Filename : cli.py
# @Author   : Alan_Hsu
"""``ah coverage-gap`` — 接口契约覆盖差集 CLI（后周期支柱 C）。

计算 OpenAPI 契约全量接口 vs 用例库的覆盖缺口，输出未覆盖接口/参数清单。

用法：``ah coverage-gap <project> --openapi <path> [--json-path <out>]``
Hermetic：仅离线解析，零网络。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ah.evolution import ensure_data_dir, load_project_cases, resolve_project_dir


def _cmd_gap(args: argparse.Namespace) -> int:
    """计算并输出覆盖差集。"""
    openapi = getattr(args, "openapi", None)
    if not openapi:
        print("coverage-gap 需要 --openapi <OpenAPI 文件路径>", file=sys.stderr)
        return 2
    project_dir = resolve_project_dir(args.project)
    ensure_data_dir(project_dir)
    cases = load_project_cases(project_dir / "data")
    report = _compute(args, openapi, cases)
    print(
        f"coverage-gap {args.project}: 契约 {report.total_operations} 个接口，"
        f"已覆盖 {report.covered_interfaces}，覆盖率 {report.coverage_ratio}%；"
        f"缺口 {len(report.gaps)} 条"
    )
    for g in report.gaps:
        print(f"  - [{g.kind}] {g.method} {g.path}: {g.detail}")
    if getattr(args, "json_path", None):
        Path(args.json_path).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


def _compute(args: argparse.Namespace, openapi: str, cases: list[Any]):
    """真实计算入口（隔离 compute_gaps 调用，便于测试替换）。"""
    from ah.coverage.gap import compute_gaps, load_catalog

    catalog = load_catalog(openapi, service=getattr(args, "service", "default"))
    return compute_gaps(catalog, cases)


def register(subparsers: argparse._SubParsersAction) -> None:
    """把 ``coverage-gap`` 子命令挂到 argparse subparsers。"""
    p = subparsers.add_parser(
        "coverage-gap",
        help="接口契约覆盖差集：OpenAPI 全接口 vs 用例库",
        description="确定性地输出未覆盖接口/参数清单，指导补充优先级。",
    )
    p.add_argument("project", nargs="?", default="demo", help="项目名或含 data/ 的目录")
    p.add_argument("--openapi", default=None, help="OpenAPI 规范文件路径（JSON/YAML）")
    p.add_argument("--service", default="default", help="服务名（标识用）")
    p.add_argument("--json-path", default=None, help="把结构化结果写到该 JSON 文件")
    p.set_defaults(func=_cmd_gap)


def run(args: argparse.Namespace) -> int:
    """coverage-gap 模块执行入口。"""
    func = getattr(args, "func", None)
    if func is None:
        print("coverage-gap：需要 --openapi", file=sys.stderr)
        return 2
    return int(func(args) or 0)
