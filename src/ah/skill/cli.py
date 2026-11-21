#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/17 10:10
# @Filename : cli.py
# @Author   : Alan_Hsu
"""ah skill list/validate：Skill 目录与静态校验。

- ``ah skill list <project>``：列出项目内全部 skill（名/服务/入参/出参/副作用/调用）。
- ``ah skill validate <project>``：静态校验 schema + 互调引用 + 变量一致性 + 环检测；
  校验失败（有错误）返回退出码 1，可接 CI。

两者都**纯静态、零网络**：不执行任何请求，``LLMOFF=1`` 安全。执行入口与 scenario/
coverage 一致：子模块暴露 ``register(subparsers)`` 挂到 Common ``ah`` CLI。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ah.skill.model import build_registry
from ah.skill.validate import summary, validate_registry


def register(subparsers: argparse._SubParsersAction) -> None:
    """注册 skill 列表与校验子命令。"""
    parser = subparsers.add_parser(
        "skill", help="列出、校验可复用 API Skill（静态、零网络）"
    )
    actions = parser.add_subparsers(dest="skill_action", required=True)

    listing = actions.add_parser("list", help="列出项目内全部 skill 目录摘要")
    listing.add_argument("project", help="业务项目名（projects/<project>）或含 skills/ 的目录路径")
    listing.add_argument("--json", action="store_true", help="以 JSON 输出摘要")
    listing.set_defaults(func=_list)

    validation = actions.add_parser("validate", help="静态校验 skill schema 与互调引用；失败退出 1")
    validation.add_argument(
        "project", help="业务项目名（projects/<project>）或含 skills/ 的目录路径"
    )
    validation.add_argument(
        "--loose-vars",
        action="store_true",
        help="宽松模式：with/assign 变量不一致仅告警不判错（默认严格）",
    )
    validation.set_defaults(func=_validate)
    graphing = actions.add_parser("graph", help="输出 Skill 互调关系图（Mermaid）")
    graphing.add_argument(
        "project", help="业务项目名（projects/<project>）或含 skills/ 的目录路径"
    )
    graphing.add_argument(
        "--json", action="store_true", help="以 JSON 输出节点与调用边（供外部绘图）"
    )
    graphing.set_defaults(func=_graph)


def _resolve_project(project: str) -> Path:
    """把项目名/目录路径解析为实际项目目录（含 skills/ 的那个）。

    三种输入都能命中：``projects/<project>``（项目名）、继承项目根的 ``<project>``
    目录路径、直接给定含 ``skills/`` 的绝对/相对目录。
    """
    p = Path(project)
    if p.is_dir():
        return p
    # 尝试 projects/<project>
    candidate = Path("projects") / project
    if candidate.is_dir():
        return candidate
    raise SystemExit(f"找不到项目目录 {project!r}（期望 projects/{project} 或含 skills/ 的目录）")


def _load(project: str):
    base = _resolve_project(project)
    try:
        registry = build_registry(base)
    except ValueError as exc:
        print(json.dumps({"valid": False, "errors": [{"code": "load_error", "message": str(exc)}]},
                         ensure_ascii=False))
        raise SystemExit(1) from exc
    return registry, base


def _list(args: argparse.Namespace) -> int:
    registry, _ = _load(args.project)
    data = summary(registry)
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    if not data["skills"]:
        print("未发现 skill（期望 <project>/skills/ 或 <project>/data/skills/ 下 *.yaml）")
        return 0
    print(f"共 {data['count']} 个 skill：")
    for s in data["skills"]:
        calls = "、" .join(s["calls"]) if s["calls"] else "-"
        effects = ",".join(s["side_effects"]) if s["side_effects"] else "read"
        print(f"  {s['name']:<24} [{s['works_on']}] 入参={s['inputs'] or '-'} "
              f"出参={s['outputs'] or '-'} 副作用={effects} 调用: {calls}")
    return 0


def _validate(args: argparse.Namespace) -> int:
    registry, _ = _load(args.project)
    result = validate_registry(registry, strict_vars=not args.loose_vars)
    print(json.dumps(result, ensure_ascii=False))
    if not result["valid"]:
        return 1
    return 0


def _graph(args: argparse.Namespace) -> int:
    """输出 skill 互调关系图：默认 stdout Mermaid，``--json`` 给节点/边清单。"""
    registry, base = _load(args.project)
    nodes = sorted(registry.names())
    edges = []
    for name in nodes:
        skill = registry.get(name)
        for target in sorted(getattr(skill, "call_targets", lambda: [])()):
            if target in registry.names() and target != name:
                edges.append((name, target))
    if args.json:
        print(
            json.dumps(
                {"project": str(base), "nodes": nodes, "edges": edges},
                ensure_ascii=False,
            )
        )
        return 0
    lines = ["graph TD"]
    lines.extend(f'    {_mq(n)}["{n}"]' for n in nodes)
    if edges:
        lines.extend(f"    {_mq(src)} --> {_mq(dst)}" for src, dst in edges)
    else:
        lines.append("    %% 无 skill 互调关系")
    print("\n".join(lines))
    return 0


def _mq(name: str) -> str:
    """Mermaid 节点 id 消毒：只留字母/数字/下划线，避免 `-`/`.` 破坏语法。"""
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name) or "node"
