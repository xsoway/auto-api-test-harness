#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/28 15:35
# @Filename : modeler.py
# @Author   : Alan_Hsu
"""可选语义助手只选择已有操作；不接收抓包值，不创造业务 oracle。"""

from __future__ import annotations

import json
import os

from ah.llm.base import BaseLLM


def suggest_operations(catalog: dict, *, llm: BaseLLM | None = None) -> dict:
    """LLMOFF 明确不可用；注入 BaseLLM 时也只允许目录 key 候选。"""
    operations = [
        {"key": op["key"], "method": op["method"], "path": op["path"]}
        for op in catalog["operations"]
    ]
    unavailable = {
        "available": False,
        "reason": "LLM 未启用；仅保留确定性目录候选",
        "candidates": [{"operation": op["key"], "status": "proposed"} for op in operations],
    }
    if os.getenv("LLMOFF", "").strip().lower() in {"1", "true", "yes", "on"} or llm is None:
        return unavailable
    # 故意不传 HAR、输入值、响应样本、headers、security、scope 或任意用户自由文本。
    response = llm.judge(
        "scenario_candidate_operations",
        json.dumps(
            {
                "operations": operations,
                "instruction": "只返回目录内 operation 候选；禁止新接口、断言、凭据。",
            },
            ensure_ascii=False,
        ),
    )
    allowed = {op["key"] for op in operations}
    result, seen = [], set()
    if not isinstance(response, dict) or not isinstance(response.get("candidates"), list):
        return {"available": False, "reason": "模型输出不符合候选结构", "candidates": []}
    for candidate in response["candidates"]:
        if (
            not isinstance(candidate, dict)
            or not isinstance(candidate.get("operation"), str)
            or candidate["operation"] not in allowed
        ):
            return {
                "available": False,
                "reason": "模型输出包含目录外操作，已拒绝整批建议",
                "candidates": [],
            }
        key = candidate["operation"]
        if key not in seen:
            result.append(
                {
                    "operation": key,
                    "status": "proposed",
                    "evidence": {"kind": "inferred", "spec_hash": catalog["spec_hash"]},
                }
            )
            seen.add(key)
    return {
        "available": True,
        "reason": "语义建议尚待人工审核，不代表业务正确",
        "candidates": result,
    }
