#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15 19:28
# @Filename : llmdetail.py
# @Author   : Alan_Hsu
"""LLM call 明细持久化（input/output/tokens）。

与 :func:`ah.telemetry.record_llm_call` 的**聚合账本**（llm_usage.jsonl）并存，
本模块追加写 `llm_detail.jsonl`，每条多存 `input`/`output`（明文提示词/响应），
让本地看板能展示 LLM 调用细节（Langfuse generation 视角）。

铁律：
- 不修改 `telemetry.record_llm_call` 的签名与 `llm_usage.jsonl` 契约。
- input/output 可能含敏感信息 → 仅在显式 `reports_dir` 的该明细文件里留档，
  不随聚合账本/报告输出四处扩散。
- 失败（目录不可写）只 log 告警，绝不抛错影响主运行。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ah.utils.logging import get_logger

log = get_logger("observe.llmdetail")

#: 明细文件名（相对 report_dir）。与聚合账本 llm_usage.jsonl 区分。
DETAIL_FILENAME = "llm_detail.jsonl"


def default_detail_path(report_dir: str | Path | None = None) -> Path:
    """返回明细文件路径。缺省 report_dir 时复用 telemetry 的账本路径逻辑。"""
    if report_dir is not None:
        return Path(report_dir) / DETAIL_FILENAME
    from ah.telemetry import default_ledger_path

    return default_ledger_path(None).with_name(DETAIL_FILENAME)


def record_llm_detail(
    *,
    model: str,
    task: str = "unknown",
    status: str = "success",  # "success" | "error"
    cost: float = 0.0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: Optional[float] = None,
    input_text: Optional[str] = None,
    output_text: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    report_dir: str | Path | None = None,
) -> Path:
    """向本地明细文件追加一条 LLM 调用（含 input/output）。

    与 ``telemetry.record_llm_call`` 相同的字段，外加 ``input``/``output``/``metadata``。
    失败只告警不抛出。返回明细文件路径。
    """
    path = default_detail_path(report_dir)
    record: dict[str, Any] = {
        "model": str(model),
        "task": str(task),
        "status": status,
        "cost": float(cost),
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "latency_ms": latency_ms,
        "input": input_text,
        "output": output_text,
        "metadata": metadata or {},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("record_llm_detail: 无法写入明细 %s: %s", path, exc)
    return path


def load_detail(path: str | Path) -> list[dict[str, Any]]:
    """读取明细全部记录；文件缺失返回 []，坏行跳过（不崩溃）。"""
    records: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("load_detail: 跳过坏行: %.80s", line)
    except (OSError, IOError):
        return []
    return records
