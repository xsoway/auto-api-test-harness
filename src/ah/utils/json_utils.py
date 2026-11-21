"""共享 JSON 辅助工具。

:func:`robust_json_parse` 移植自 Eval-Anything 框架的
``utils/json_utils.py``：它在放弃之前尝试多种策略，因此即使被代码围栏或
自然语言包裹的 LLM 生成文本也能被解析。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


def robust_json_parse(text: str) -> Optional[Dict[str, Any]]:
    """从 ``text`` 解析 JSON，容忍常见的 LLM 输出怪癖。

    按序尝试的策略：
      1. 直接 ``json.loads``
      2. 提取 `` ```json ... ``` `` 代码块
      3. 提取第一个配平的 ``{ ... }`` 块
      4. 修复尾部逗号与单引号，然后解析

    返回解析出的 dict；当所有策略都失败时返回 ``None``（对畸形输入
    绝不抛异常）。
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    # 策略 1：直接解析。
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 策略 2：带围栏的 ```json 代码块。
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 策略 3：第一个配平的 { ... } 块（嵌套安全）。
    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if match:
        candidate = match.group(0)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # 策略 4：修复尾部逗号 / 单引号，然后解析。
    fixed = text
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    fixed = fixed.replace("'", '"')
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    return None


def safe_json(value: Any) -> Any:
    """尽力而为的 JSON 序列化；失败时返回 ``None``。"""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return None
