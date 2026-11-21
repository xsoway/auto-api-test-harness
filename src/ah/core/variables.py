#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/30 09:10
# @Filename : variables.py
# @Author   : Alan_Hsu

"""声明式数据夹具生成（对齐《业务场景接口串联测试》的 ``variables`` 块）。

场景 YAML 在顶层用 ``variables`` 声明一组数据策略，运行时按策略生成唯一/可复现值
并写入 :class:`~ah.utils.context.Context`，供链路中的 ``${name}`` 串联消费。

支持的策略（``strategy`` 与 ``value`` 二选一）：
- ``value`` / ``strategy: fixed``：直接使用给定字面值（保留原始类型）。
- ``strategy: timestamp``：``prefix + 毫秒级 epoch + 3 位随机尾缀``，保证一次运行内唯一
  （含同毫秒）。
- ``strategy: random``：``prefix + length 位随机数字``，``seed`` 可复现。
- ``strategy: uuid``：``str(uuid.uuid4())``。

未知策略会快速失败（抛出 :class:`ValueError`），而不是悄悄降级——这是用户的
显式声明，静默忽略会掩盖配置错误。本模块零 LLM、零网络，``LLMOFF=1`` 下可安全复用。
"""

from __future__ import annotations

import random as _random
import time
import uuid
from typing import Any, Dict, List, Optional

from ah.utils.context import Context

#: timestamp 策略生成的时间戳精度转换（毫秒，保证同秒多次调用仍唯一）。
_MS = 1000


def materialize_variables(
    decls: Optional[List[Dict[str, Any]]],
    ctx: Context,
    *,
    rng: Optional[_random.Random] = None,
) -> Context:
    """按声明列表生成变量并写入 ``ctx``，返回同一 ``ctx``。

    :param decls: ``[{name, strategy|value, prefix?, length?, seed?}]`` 列表；
        为 ``None``/空时原样返回，不做任何事。
    :param ctx: 注入目标上下文（就地写入，并返回以方便链式调用）。
    :param rng: 可注入的随机源；缺省用模块级 ``random``（同 seed 才可复现）。
        ``seed`` 仅对 ``random`` 策略生效；固定实现内部务必传 ``rng`` 才能精确复现。
    :raises ValueError: 声明缺少 ``name``、``strategy`` 与 ``value`` 同时/都不出现、
        或策略名未知。

    声明的变量**总是覆盖**（场景拥有这些名字）；若不希望覆盖，调用方应在塞入前
    检查 ``ctx.has(name)``。
    """
    for item in decls or []:
        name = str(item.get("name", ""))
        if not name:
            raise ValueError("variables 声明缺少 name")
        value = _resolve_value(item, rng=rng)
        ctx.set(name, value)
    return ctx


def _resolve_value(item: Dict[str, Any], *, rng: Optional[_random.Random]) -> Any:
    """把单条声明的策略解析为生成值；策略与 value 二选一。"""
    has_value = "value" in item
    strategy = str(item.get("strategy", "")).strip().lower()
    has_strategy = bool(strategy)

    if has_value and has_strategy:
        raise ValueError(f"variables {item.get('name')!r}: strategy 与 value 只能二选一")
    if has_value:
        return item["value"]
    if not has_strategy:
        raise ValueError(f"variables {item.get('name')!r}: 需要 strategy 或 value 之一")
    if strategy == "fixed":
        raise ValueError(f"variables {item.get('name')!r}: strategy=fixed 需要 value")
    if strategy not in ("timestamp", "random", "uuid"):
        raise ValueError(
            f"variables {item.get('name')!r}: 未知策略 {strategy!r} "
            "(可选 timestamp/random/uuid/fixed 或直接用 value)"
        )

    if strategy == "timestamp":
        prefix = str(item.get("prefix", ""))
        # 毫秒时间戳 + 三位随机尾缀，避免同毫秒内多次生成碰撞，保证一次运行内唯一。
        rand = rng if rng is not None else _random
        tail = "".join(str(rand.randint(0, 9)) for _ in range(3))
        return f"{prefix}{int(time.time() * _MS)}{tail}"
    if strategy == "uuid":
        return str(uuid.uuid4())

    # strategy == "random"
    rand = rng if rng is not None else _random
    seed = item.get("seed")
    if seed is not None and rng is None:
        rand = _random.Random(seed)
    prefix = str(item.get("prefix", ""))
    length = max(1, int(item.get("length", 8)))
    digits = "".join(str(rand.randint(0, 9)) for _ in range(length))
    return f"{prefix}{digits}"




