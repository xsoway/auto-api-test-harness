#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2026-10-08 21:10
# @Filename: telemetry.py
# @Author  : Alan_Hsu
"""Langfuse LLM 成本看板（三方接入 · 功能 14）。

从本地 LLM 用量账本（``<report_dir>/llm_usage.jsonl``）聚合 LLM 调用的
``cost / success / error`` 计数，输出可配置摘要（CLI 打印 + 可写报告段）。

- 只做**聚合**，不改 ``src/ah/core/trace.py`` 的埋点。
- :func:`record_llm_call` 是供 LLM 调用方（如 LLMClient）追加记录的入口，
  hermetic 模式（LLMOFF=1、无 LLM 调用）下账本为空，聚合结果 0/空，
  绝不触碰网络。
- 成本聚合只做本地 JSONL 文件 IO；缺省读 :func:`default_ledger_path`。

命令行入口：
    ah telemetry <project> [--fields cost,success,error,total] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Optional

from ah.core.config import settings
from ah.utils.logging import get_logger

log = get_logger("telemetry")

#: 账本文件名（相对 report_dir）。
LEDGER_FILENAME = "llm_usage.jsonl"

#: 汇总摘要里默认展示的字段（可经 --fields 覆盖）。
DEFAULT_FIELDS = ["cost", "requests", "success", "errors", "success_rate"]


def default_ledger_path(report_dir: str | Path | None = None) -> Path:
    """返回账本文件路径：显式 report_dir，否则活动 ``AH_REPORT_DIR``，
    再退回 ``settings.report_dir``。

    优先读运行期的 ``AH_REPORT_DIR`` 环境变量（而非 import 时烘焙的
    ``settings.report_dir``）：业务项目在 ``pytest_configure`` 阶段才会
    ``os.environ.setdefault("AH_REPORT_DIR", <项目 report/>)``，此时再写入的
    账本会落到项目报告目录；报告侧的 ``_project_insights`` 用同一函数读，
    保证写读同源、``LLM 成本`` 段在项目报告中正确渲染。
    """
    base: Path
    if report_dir:
        base = Path(report_dir)
    else:
        env_dir = os.getenv("AH_REPORT_DIR")
        base = Path(env_dir) if env_dir else Path(settings.report_dir)
    return base / LEDGER_FILENAME


def record_llm_call(
    *,
    model: str,
    task: str = "unknown",
    status: str = "success",  # "success" | "error"
    cost: float = 0.0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: Optional[float] = None,
    report_dir: str | Path | None = None,
) -> Path:
    """向本地账本追加一条 LLM 调用记录（聚合的原始输入）。

    这是聚合侧的**写入面**，不修改 trace 埋点。失败（如目录不可写）会
    log 告警而不抛出，确保追踪/聚合绝不影响主运行。返回账本路径。
    """
    path = default_ledger_path(report_dir)
    record = {
        "model": str(model),
        "task": str(task),
        "status": status,
        "cost": float(cost),
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "latency_ms": latency_ms,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("record_llm_call: 无法写入账本 %s: %s", path, exc)
    return path


def _num(value: Any) -> float:
    """安全地转数值，非法输入按 0 处理（成本/计数不可因坏行崩溃）。"""
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def load_ledger(path: str | Path) -> list[dict[str, Any]]:
    """读取账本全部记录；文件缺失返回 []，坏行跳过（不崩溃）。"""
    p = Path(path)
    if not p.exists():
        return []
    records: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    records.append(obj)
            except (ValueError, TypeError):
                log.warning("telemetry: 跳过无法解析的账本行: %.120r", line)
    return records


def aggregate_llm_stats(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """把账本记录聚合为成本/成功率/用量看板字典。

    返回字段：
    - ``requests`` 总调用数；``success`` / ``errors`` 按 status 计数。
    - ``cost`` 成本合计；``success_rate`` 成功率（无调用为 0.0）。
    - ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``：各用法合并计数
      （token 由 LLM 提供方返回，缺失按 0）。
    - ``by_model`` 按 model 分组的 ``{requests, cost, success, errors,
      prompt_tokens, completion_tokens, total_tokens}``。
    - ``llm_off`` 标记 LLM 是否应关闭（供 CLI 提示「空看板」）。
    """
    total_requests = len(records)
    success = 0
    errors = 0
    cost = 0.0
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    by_model: dict[str, dict[str, float | int]] = {}

    for rec in records:
        status = str(rec.get("status", "success")).lower()
        if status == "error" or status in {"fail", "failed"}:
            errors += 1
        else:
            success += 1
        cost += _num(rec.get("cost"))

        pt = int(_num(rec.get("prompt_tokens")))
        ct = int(_num(rec.get("completion_tokens")))
        prompt_tokens += pt
        completion_tokens += ct
        total_tokens += pt + ct

        model = str(rec.get("model", "unknown")) or "unknown"
        bucket = by_model.setdefault(
            model,
            {
                "requests": 0,
                "cost": 0.0,
                "success": 0,
                "errors": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        )
        bucket["requests"] = int(bucket["requests"]) + 1
        bucket["cost"] = float(bucket["cost"]) + _num(rec.get("cost"))
        bucket["prompt_tokens"] = int(bucket["prompt_tokens"]) + pt
        bucket["completion_tokens"] = int(bucket["completion_tokens"]) + ct
        bucket["total_tokens"] = int(bucket["total_tokens"]) + pt + ct
        if status == "error" or status in {"fail", "failed"}:
            bucket["errors"] = int(bucket["errors"]) + 1
        else:
            bucket["success"] = int(bucket["success"]) + 1

    success_rate = round(success / total_requests, 4) if total_requests else 0.0
    return {
        "requests": total_requests,
        "success": success,
        "errors": errors,
        "cost": round(cost, 6),
        "success_rate": success_rate,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "by_model": by_model,
        "llm_off": not _llm_on(),
    }


def _llm_on() -> bool:
    """LLM 是否启用：复用 openai.llm_enabled 判定（LLMOFF / 无 key → False）。"""
    try:
        from ah.llm.openai import llm_enabled

        return llm_enabled()
    except Exception:  # 依赖缺失时保守判定为关
        return False


def summarize(stats: dict[str, Any], fields: list[str] | None = None) -> str:
    """把聚合结果渲染成人类可读的多行字符串（配置化字段子集）。"""
    fields = [f for f in (fields or DEFAULT_FIELDS) if f in stats]
    lines = []
    if stats.get("llm_off"):
        lines.append("[telemetry] LLM 未启用（LLMOFF/无 key）——看板为空。")
    for key in fields:
        if key == "cost":
            lines.append(f"[telemetry] 成本: ${stats['cost']:.6f}")
        elif key == "success_rate":
            lines.append(f"[telemetry] 成功率: {stats['success_rate']:.2%}")
        else:
            lines.append(f"[telemetry] {key}: {stats[key]}")
    if stats.get("by_model"):
        lines.append("[telemetry] 按模型:")
        for model, bucket in sorted(stats["by_model"].items()):
            lines.append(
                f"  - {model}: 请求={bucket['requests']} 成功={bucket['success']} "
                f"错误={bucket['errors']} 成本=${bucket['cost']:.6f}"
            )
    return "\n".join(lines)


def _validate_fields(fields: Optional[str]) -> list[str]:
    """解析 --fields 逗号列表；空/缺省回退 DEFAULT_FIELDS；非法值剔除。"""
    if not fields:
        return list(DEFAULT_FIELDS)
    known = {
        "cost",
        "requests",
        "success",
        "errors",
        "success_rate",
        "by_model",
    }
    return [f.strip() for f in fields.split(",") if f.strip() in known]


def register(subparsers: argparse._SubParsersAction) -> None:
    """把 ``telemetry`` 子命令挂到 argparse subparsers。"""
    p = subparsers.add_parser(
        "telemetry",
        help="聚合 LLM 调用成本/成功率看板（Langfuse 三方接入）",
        description="从本地 LLM 用量账本聚合 cost/success/error 计数并输出摘要。",
    )
    p.add_argument("project", help="项目名（仅用于标识，聚合读 settings.report_dir）")
    p.add_argument(
        "--fields",
        default=None,
        help="逗号分隔的展示字段子集（cost,requests,success,errors,success_rate）",
    )
    p.add_argument(
        "--report-dir",
        default=None,
        help="账本所在目录（缺省 settings.report_dir）",
    )
    p.add_argument(
        "--json",
        dest="write_json",
        action="store_true",
        help="把聚合结果写一份 llm_telemetry.json（报告段）",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """执行聚合并输出摘要；0=成功（含空看板为成功语义）。"""
    report_dir = getattr(args, "report_dir", None) or settings.report_dir
    path = default_ledger_path(report_dir)
    records = load_ledger(path)
    stats = aggregate_llm_stats(records)
    fields = _validate_fields(getattr(args, "fields", None))

    text = summarize(stats, fields)
    print(text)

    if getattr(args, "write_json", False) or stats["requests"] == 0:
        out = Path.cwd() / "llm_telemetry.json"
        out.write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("telemetry report written: %s", out)

    log.info(
        "telemetry: ledger=%s records=%d cost=%.6f success=%d errors=%d",
        path,
        stats["requests"],
        stats["cost"],
        stats["success"],
        stats["errors"],
    )
    # 聚合无「闸」语义——看板旨在观测，恒返回成功（0）。
    return 0


__all__ = [
    "aggregate_llm_stats",
    "default_ledger_path",
    "load_ledger",
    "record_llm_call",
    "register",
    "run",
    "summarize",
]
