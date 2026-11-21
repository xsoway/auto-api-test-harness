#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/15 15:50
# @Filename : cli.py
# @Author   : Alan_Hsu
"""``ah quality`` — 后周期质量治理 CLI（健康度评定 & 复盘清洗 & 冗余识别）。

子命令（全部 Hermetic，LLMOFF=1 可运行）：

- ``ah quality health <project>`` — 全用例健康度评分 + 断言密度 + 一票否决 + 无效断言清单。
- ``ah quality audit <project>``  — 已入库用例复盘清洗（LLM 关闭走确定性低分清单）。
- ``ah quality duplicates <project>`` — 等价重复用例指纹识别。
- ``ah quality lowvalue <project>`` — 低价值/僵尸用例筛排。

用例加载复用 :func:`ah.evolution.load_project_cases`，上下文可用 `--retired` /
`--recent <history.jsonl>` 增强一票否决/低价值判定。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from ah.evolution import (
    ensure_data_dir,
    load_project_cases,
    resolve_project_dir,
)
from ah.quality.audit import run_audit
from ah.quality.health import (
    HealthContext,
    aggregate_health,
    assess_suite,
)
from ah.quality.redundancy import (
    group_duplicates,
    screen_low_value,
)


def _load_project(args: argparse.Namespace) -> tuple[Path, list[Any]]:
    """定位项目 data 目录并加载全量用例。"""
    project_dir = resolve_project_dir(args.project)
    data_dir = ensure_data_dir(project_dir)
    return data_dir, load_project_cases(data_dir)


def _ctx_from_args(args: argparse.Namespace) -> HealthContext:
    """从 CLI 参数构造健康度上下文（未提供的维度缺省 None，保证 Hermetic）。"""
    ctx = HealthContext()
    if getattr(args, "retired", None):
        retired: set[tuple[str, str]] = set()
        for item in str(args.retired).split(","):
            item = item.strip()
            if not item:
                continue
            parts = item.split()
            method = parts[0].upper() if parts else "GET"
            path = parts[1] if len(parts) > 1 else item
            retired.add((method, path))
        ctx.retired_paths = retired
    if getattr(args, "stale", None):
        ctx.stale_names = {s.strip() for s in str(args.stale).split(",") if s.strip()}
    if getattr(args, "broken", None):
        ctx.broken_names = {s.strip() for s in str(args.broken).split(",") if s.strip()}
    return ctx


def _cmd_health(args: argparse.Namespace) -> int:
    """输出全用例健康度报告（评分/断言密度/一票否决/无效断言清单）。"""
    data_dir, cases = _load_project(args)
    if not cases:
        print(f"quality health {args.project}: 无用例", file=sys.stderr)
        return 1
    ctx = _ctx_from_args(args)
    items = assess_suite(cases, ctx)
    report = aggregate_health(items)
    print(
        f"quality health {args.project}: 共 {report.total} 条，通过 {report.approved}，"
        f"一票否决 {report.vetoed}，平均分 {report.avg_score}，"
        f"平均断言密度 {report.avg_assert_density}"
    )
    if report.by_veto:
        print("一票否决分布:", json.dumps(report.by_veto, ensure_ascii=False))
    warn = [i for i in items if not i.approved and not i.veto]
    if warn:
        print("未通过门禁（非一票否决）:", file=sys.stderr)
        for i in warn:
            print(
                f"  - {getattr(i.case, 'name', '')} score={i.score} "
                f"assert={i.assert_count} reasons={i.reasons}",
                file=sys.stderr,
            )
    if getattr(args, "json_path", None):
        Path(args.json_path).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    """已入库用例复盘清洗（LLM 可选，关闭走确定性建议）。"""
    data_dir, cases = _load_project(args)
    if not cases:
        print(f"quality audit {args.project}: 无用例", file=sys.stderr)
        return 1
    ctx = _ctx_from_args(args)
    llm = getattr(args, "llm", None)
    report = run_audit(cases, ctx, llm=llm, min_quality=float(getattr(args, "min_quality", 70.0)))
    print(
        f"quality audit {args.project}: 共 {report.total} 条，需优化 {report.to_optimize}，"
        f"需清洗 {report.to_clean}，建议下线 {report.to_delete}，健康 {report.healthy}"
    )
    for it in report.items:
        if it.action != "ok":
            print(f"  - [{it.action}] {it.name}: {it.suggestion}")
    if getattr(args, "json_path", None):
        Path(args.json_path).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


def _cmd_duplicates(args: argparse.Namespace) -> int:
    """等价重复用例指纹识别。"""
    data_dir, cases = _load_project(args)
    if not cases:
        print(f"quality duplicates {args.project}: 无用例", file=sys.stderr)
        return 1
    groups = group_duplicates(cases)
    redundant = sum(len(g.redundant) for g in groups)
    print(
        f"quality duplicates {args.project}: 共 {len(cases)} 条，"
        f"等价重复组 {len(groups)} 组，冗余候选 {redundant} 条"
    )
    for g in groups:
        primary = getattr(g.primary, "name", "unknown")
        exact = "（文本重复）" if g.is_exact_duplicate else ""
        print(
            f"  - 组 {len(g.members)} 条{exact}: 主版 {primary}，"
            f"冗余 {[getattr(m, 'name', '') for m in g.redundant]}"
        )
    if getattr(args, "json_path", None):
        Path(args.json_path).write_text(
            json.dumps(
                [
                    {
                        "fingerprint": g.fingerprint,
                        "members": [getattr(m, "name", "") for m in g.members],
                        "primary": getattr(g.primary, "name", ""),
                        "is_exact_duplicate": g.is_exact_duplicate,
                    }
                    for g in groups
                ],
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    return 0


def _cmd_lowvalue(args: argparse.Namespace) -> int:
    """低价值/僵尸用例筛排。"""
    data_dir, cases = _load_project(args)
    if not cases:
        print(f"quality lowvalue {args.project}: 无用例", file=sys.stderr)
        return 1
    ctx = _ctx_from_args(args)
    recent: Optional[dict[str, list[str]]] = None
    if getattr(args, "recent", None):
        recent = _load_recent_results(args.recent)
    report = screen_low_value(
        cases,
        retired_paths=ctx.retired_paths,
        recent_results=recent,
        no_defect_consecutive=int(getattr(args, "consecutive", 3)),
        min_success_count=int(getattr(args, "min_success", 3)),
    )
    print(f"quality lowvalue {args.project}: 低价值/僵尸 {len(report.items)} 条")
    for it in report.items:
        print(f"  - {getattr(it.case, 'name', '')}: {it.reason} ({it.evidence})")
    if getattr(args, "json_path", None):
        Path(args.json_path).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


def _load_recent_results(path: str) -> dict[str, list[str]]:
    """从 history.jsonl / 简单 JSONL 加载 ``{case_name: [result, ...]}``。"""
    out: dict[str, list[str]] = {}
    p = Path(path)
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(entry.get("name") or entry.get("interface") or "")
        if not name:
            continue
        out.setdefault(name, []).append(str(entry.get("result", "")))
    return out


def register(subparsers: argparse._SubParsersAction) -> None:
    """把 ``quality`` 子命令挂到 argparse subparsers。"""
    p = subparsers.add_parser(
        "quality",
        help="后周期质量治理：健康度/复盘清洗/冗余识别/低价值筛排",
        description="确定性优先的用例质量治理（LLMOFF=1 可运行）。",
    )
    p.add_argument("project", nargs="?", default="demo", help="项目名或含 data/ 的目录")
    p.add_argument(
        "--min-quality", type=float, default=70.0,
        help="健康度门禁阈值（默认 70，文章 70 分门禁）",
    )
    p.add_argument("--retired", default=None, help="已下线接口（'METHOD path,GET /x'）")
    p.add_argument("--stale", default=None, help="连续失败无人维护的用例名（逗号分隔）")
    p.add_argument("--broken", default=None, help="严重逻辑错误的用例名（逗号分隔）")
    p.add_argument("--recent", default=None, help="历史结果 JSONL（低价值筛排用）")
    p.add_argument("--consecutive", type=int, default=3, help="低价值：连续成功次数阈值")
    p.add_argument("--min-success", type=int, default=3, help="低价值：最少成功样本数")
    p.add_argument("--json-path", default=None, help="把结构化结果写到该 JSON 文件")

    action = p.add_subparsers(dest="action", required=True)
    a_health = action.add_parser("health", help="健康度评分 + 断言密度 + 一票否决")
    a_health.set_defaults(func=_cmd_health)
    a_audit = action.add_parser("audit", help="已入库用例复盘清洗")
    a_audit.set_defaults(func=_cmd_audit)
    a_dup = action.add_parser("duplicates", help="等价重复用例指纹识别")
    a_dup.set_defaults(func=_cmd_duplicates)
    a_low = action.add_parser("lowvalue", help="低价值/僵尸用例筛排")
    a_low.set_defaults(func=_cmd_lowvalue)


def run(args: argparse.Namespace) -> int:
    """quality 模块执行入口（按 func 分发）。"""
    func = getattr(args, "func", None)
    if func is None:
        print("quality 子命令：health / audit / duplicates / lowvalue", file=sys.stderr)
        return 2
    return int(func(args) or 0)
