"""从响应中提取变量到 :class:`Context`。

``extract_vars(response_text, spec, ctx)`` 应用每条规格条目
``{var_name: {"path": <query>, "extract_type": "jsonpath"|"regex"}}``，将
匹配到的值存入 ``ctx``。

- ``jsonpath``：对解析后的 JSON 主体执行 :mod:`jmespath` 查询。无匹配时抛出
  断言风格的 :class:`ExtractionError`。
- ``regex``：取第一个匹配组；若模式没有组则取整个匹配。

提取值覆盖下游变量，符合后写覆盖（last-write-wins）的流程语义。
"""

from __future__ import annotations

import json
import re
from typing import Any

import jmespath

from ah.utils.context import Context


class ExtractionError(AssertionError):
    """当提取规格未产出任何值时抛出（断言风格失败）。"""


def _to_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ExtractionError(f"response body is not valid JSON: {exc}") from exc


def extract_vars(
    response_text: str, spec: dict[str, dict[str, Any]], ctx: Context,
    *, allow_null: bool = False,
) -> dict[str, Any]:
    """应用提取规格并将结果存入 ``ctx``。

    返回新提取变量名到值的映射。
    """
    results: dict[str, Any] = {}
    for var_name, config in (spec or {}).items():
        extract_type = str(config.get("extract_type", "jsonpath")).lower()
        path = config.get("path")
        if path is None:
            raise ExtractionError(f"extract[{var_name}] missing 'path'")
        if extract_type == "jsonpath":
            value = _extract_jsonpath(response_text, str(path), var_name, allow_null=allow_null)
        elif extract_type == "regex":
            value = _extract_regex(response_text, str(path), var_name)
        else:
            raise ExtractionError(
                f"extract[{var_name}] unknown extract_type {extract_type!r} "
                "(use 'jsonpath' or 'regex')"
            )
        ctx.set(var_name, value)
        results[var_name] = value
    return results


def _extract_jsonpath(
    text: str, path: str, var_name: str, *, allow_null: bool = False
) -> Any:
    data = _to_json(text)
    try:
        value = jmespath.search(path, data)
    except Exception as exc:  # jmespath 在搜索时抛出解析错误
        raise ExtractionError(f"extract[{var_name}]: bad jsonpath {path!r}: {exc}") from exc
    if value is None:
        # v2 区分显式 null 与缺失，旧用例仍保留 null 失败行为。
        if not allow_null or not _has_null_match(jmespath.compile(path).parsed, data):
            raise ExtractionError(f"extract[{var_name}]: no match for jsonpath {path!r}")
    return value


def _has_null_match(node: dict[str, Any], data: Any) -> bool:
    """区分字段/索引链的显式 null 与缺失；复杂查询仍按无匹配处理。"""
    missing = object()

    def lookup(ast: dict[str, Any], value: Any) -> Any:
        kind = ast["type"]
        if kind == "identity":
            return value
        if kind == "field":
            return value.get(ast["value"], missing) if isinstance(value, dict) else missing
        if kind == "index":
            index = ast["value"]
            if isinstance(value, list) and -len(value) <= index < len(value):
                return value[index]
            return missing
        if kind in {"subexpression", "index_expression"}:
            for child in ast["children"]:
                value = lookup(child, value)
                if value is missing:
                    break
            return value
        return missing

    return lookup(node, data) is None


def _extract_regex(text: str, pattern: str, var_name: str) -> str:
    m = re.search(pattern, text)
    if not m:
        raise ExtractionError(f"extract[{var_name}]: no regex match for {pattern!r}")
    if m.lastindex:  # 一个或多个组：使用第一个组
        return m.group(1)
    return m.group(0)
