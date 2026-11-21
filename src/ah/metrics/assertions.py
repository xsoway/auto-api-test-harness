"""响应断言。

``assert_response(case_assert, resp, ctx)`` 针对 :class:`ah.client.APIResponse`
评估一份声明式断言规格：

- ``status_code``：精确 int，或可接受状态码的列表/元组。
- ``schema``：依据响应 JSON 校验的 JSON Schema。
- ``jsonpath``：``{path: {eq|contains|type|lt|gt}}`` — 逐个检查操作符。
- ``contains``：单个子串或子串列表，均须存在于原始响应文本中。

失败抛出 :class:`AssertionError`，消息为规范化格式
``{[path]} expected {X}, got {Y}`` —— 供 LLM 错误三分类/自愈（第 8 步）
消费的稳定格式。
"""

from __future__ import annotations

from typing import Any, Optional

import jmespath
import jsonschema

from ah.harness.http import APIResponse
from ah.utils.context import Context, render_value


def _fmt(value: Any) -> str:
    if isinstance(value, str):
        return value
    return repr(value)


def _fail(path: str, expected: Any, got: Any, *, soft: bool = False) -> None:
    """断言失败判定；soft 模式只记录不抛出（供软断言使用）。"""
    if soft:
        return
    raise AssertionError(f"{{{path}}} expected {_fmt(expected)}, got {_fmt(got)}")


def _record(
    collected: Optional[list[dict[str, Any]]],
    *,
    type_: str,
    logic: str,
    expected: Any,
    actual: Any,
    passed: bool,
    detail: str = "",
) -> None:
    """把单条断言评估结果追加进记录（供报告中「断言详情」渲染）。

    ``collected`` 为 None 时跳过（向后兼容：旧调用方不采集断言元数据）。
    """
    if collected is None:
        return
    collected.append(
        {
            "type": type_,
            "logic": logic,
            "expected": _fmt(expected),
            "actual": _fmt(actual),
            "passed": passed,
            "detail": detail,
        }
    )


def assert_response(
    case_assert: dict[str, Any],
    resp: APIResponse,
    ctx: Context,
    *,
    collected: Optional[list[dict[str, Any]]] = None,
    soft: bool = False,
    reference_mode: str = "legacy",
) -> None:
    """针对响应评估所有断言键（失败时默认抛出 AssertionError）。

    ``soft=True`` 时断言失败只记录（passed=False），不抛出，供软断言场景使用；
    硬断言（默认）行为保持不变。``collected`` 存在时，会把每条断言的结果
    （类型/逻辑/期望值/实际值/是否通过）追加进去，使报告能展示「断言详情」。
    """
    if not case_assert:
        return
    # 预先基于 ctx 解析任何 ${var} 模板，使每个下游检查
    # 都针对具体值进行比较（例如 jsonpath.eq、contains）。
    if reference_mode == "typed":
        case_assert = render_value(case_assert, ctx, typed=True)
    else:
        _resolve_templates(case_assert, ctx)
    if "status_code" in case_assert:
        expected = case_assert["status_code"]
        codes = expected if isinstance(expected, (list, tuple, set)) else [expected]
        ok = resp.status_code in codes
        _record(
            collected, type_="status_code", logic="∈", expected=expected,
            actual=resp.status_code, passed=ok,
        )
        if not ok:
            _fail("status_code", expected, resp.status_code, soft=soft)
    if "schema" in case_assert:
        data = resp.json()
        try:
            jsonschema.validate(instance=data, schema=case_assert["schema"])
            _record(
                collected, type_="schema", logic="validate",
                expected="JSON Schema", actual="valid", passed=True,
            )
        except jsonschema.ValidationError as exc:
            # 规范化消息会指明失败的具体属性。
            path = ".".join(str(p) for p in exc.absolute_path) or "<root>"
            _record(
                collected, type_="schema", logic=f"validate.{path}",
                expected=exc.message, actual="invalid", passed=False,
                detail=exc.message,
            )
            _fail(f"schema.{path}", exc.message, "invalid", soft=soft)
    if "jsonpath" in case_assert:
        _assert_jsonpath(
            case_assert["jsonpath"], resp, collected=collected, soft=soft,
            typed=reference_mode == "typed",
        )
    if "contains" in case_assert:
        needles = case_assert["contains"]
        if isinstance(needles, str):
            needles = [needles]
        for needle in needles:
            ok = str(needle) in resp.text
            _record(
                collected, type_="contains", logic="substring",
                expected=needle, actual="命中" if ok else "未命中", passed=ok,
            )
            if not ok:
                _fail("contains", needle, resp.text[:256], soft=soft)

def _eq_coerce(a: Any, b: Any) -> bool:
    """严格相等，并为模板化比较提供数字字符串强制转换。

    ``${var}`` 渲染为字符串（例如 ``"42"``），而 JSONPath 查找返回原生类型
    （例如 ``42``）；将裸数字字符串视为其数值，以便 ``eq`` 能跨插值边界匹配。
    """
    if a == b:
        return True
    if isinstance(a, str) and isinstance(b, (int, float)) and not isinstance(b, bool):
        try:
            return int(a) == b if isinstance(b, int) else float(a) == b
        except ValueError:
            return False
    if isinstance(b, str) and isinstance(a, (int, float)) and not isinstance(a, bool):
        try:
            return int(b) == a if isinstance(a, int) else float(b) == a
        except ValueError:
            return False
    return False


def _typed_equal(actual: Any, expected: Any) -> bool:
    """typed 比较递归保留标量类型（包括 bool/int 和 int/float 的区别）。"""
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(
            _typed_equal(left, right) for left, right in zip(actual, expected)
        )
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(
            _typed_equal(value, expected[key]) for key, value in actual.items()
        )
    return actual == expected


def _assert_jsonpath(
    spec: dict[str, Any],
    resp: APIResponse,
    *,
    collected: Optional[list[dict[str, Any]]] = None,
    soft: bool = False,
    typed: bool = False,
) -> None:
    """对 jsonpath 子断言求值；soft 模式失败只记录不抛出。"""
    data = resp.json()
    for path, ops in spec.items():
        value = jmespath.search(path, data)
        if not isinstance(ops, dict):
            ops = {"eq": ops}
        for op, expected in ops.items():
            if op == "eq":
                ok = (
                    _typed_equal(value, expected)
                    if typed else _eq_coerce(value, expected)
                )
                _record(
                    collected, type_="jsonpath", logic=f"{path} ==",
                    expected=expected, actual=value, passed=ok,
                )
                if not ok:
                    _fail(f"jsonpath.{path}.eq", expected, value, soft=soft)
            elif op == "contains":
                ok = value is not None and expected in value
                _record(
                    collected, type_="jsonpath", logic=f"{path} in",
                    expected=expected, actual=value, passed=ok,
                )
                if not ok:
                    _fail(f"jsonpath.{path}.contains", expected, value, soft=soft)
            elif op == "type":
                ok = _type_matches(value, expected)
                _record(
                    collected, type_="jsonpath", logic=f"{path} type",
                    expected=expected, actual=type(value).__name__, passed=ok,
                )
                if not ok:
                    _fail(
                        f"jsonpath.{path}.type", expected,
                        type(value).__name__, soft=soft,
                    )
            elif op == "lt":
                ok = isinstance(value, (int, float)) and value < expected
                _record(
                    collected, type_="jsonpath", logic=f"{path} <",
                    expected=expected, actual=value, passed=ok,
                )
                if not ok:
                    _fail(f"jsonpath.{path}.lt", expected, value, soft=soft)
            elif op == "gt":
                ok = isinstance(value, (int, float)) and value > expected
                _record(
                    collected, type_="jsonpath", logic=f"{path} >",
                    expected=expected, actual=value, passed=ok,
                )
                if not ok:
                    _fail(f"jsonpath.{path}.gt", expected, value, soft=soft)
            else:
                _record(
                    collected, type_="jsonpath", logic=f"{path} {op}",
                    expected="supported-op", actual=op, passed=False,
                )
                _fail(f"jsonpath.{path}.{op}", "supported-op", op, soft=soft)


def _type_matches(value: Any, expected: Any) -> bool:
    expected_name = str(expected).lower()
    type_map = {
        "string": str,
        "int": int,
        "integer": int,
        "float": float,
        "number": (int, float),
        "bool": bool,
        "boolean": bool,
        "list": list,
        "array": list,
        "dict": dict,
        "object": dict,
        "null": type(None),
    }
    if expected_name == "null":
        return value is None
    target = type_map.get(expected_name)
    if target is None:
        return False
    return isinstance(value, target)


def _resolve_templates(case_assert: dict[str, Any], ctx: Context) -> None:
    """尽力而为：用上下文值替换 eq/contains 字符串中的 ``${var}``。

    保持宽松（缺失变量原样保留），以便纯静态断言仍能工作。
    """
    from ah.utils.context import render  # 局部导入以避免模块加载时循环引用

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if isinstance(node, str) and "${" in node:
            try:
                return render(node, ctx)
            except KeyError:
                return node
        return node

    resolved = _walk(case_assert)
    case_assert.clear()
    case_assert.update(resolved)


def assert_llm(
    requirement: str,
    response_text: str,
    llm: Any = None,
    *,
    tag: str = "semantic",
    gate: Any = None,
) -> dict[str, Any]:
    """可选**语义判定**（提案 §4.2 + §7.3），不抛异常、不进硬闸。

    ``assert_llm`` 保留 ``assert_`` 确定性断言不变，新增一条 LLM 辅助的
    自由语义校验（如"列表应包含全部在售商品且按价格升序"）。返回
    ``{"verdict", "pass", "reason"}``：

    - ``llm`` 为 None（``LLMOFF``）：跳过，``verdict="SKIP"``，reason 注明。
    - LLM 输出须是确定性 JSON（``robust_json_parse``）；解析失败或调用抛错
      → 降级 ``verdict="UNVERIFIED"``（非 test 失败，报告提示人工复核）。
    - LLM 判定 ``pass=true`` → ``verdict="PASS"``。
    - LLM 判定 ``pass=false`` → **仍降级**为 ``UNVERIFIED``（§7.3：语义失败
      不等于 test 失败，默认人工复核，不做硬闸），reason 携带模型原话。

    Hermetic：LLM 禁用/失败绝不抛异常，主链路完全不受拖累。
    """
    # LayaGate 前置：语义断言最先过 Laya（assert 环节，默认关）。
    # AUTO 高置信 → 直接裁决；否则落回 LLM 判定（gate=None/低置信 → 原流程）。
    if gate is not None:
        try:
            dec = gate.decide(
                "assert",
                response_text,
                {
                    "satisfies": {
                        "type": "noul",
                        "instructions": "响应是否满足断言要求？要求：" + requirement,
                    }
                },
            )
            if dec.action.value == "auto" and isinstance(dec.value, dict):
                p_true = float(dec.value.get("satisfies") or 0.0)
                if p_true >= 0.5:
                    return {
                        "verdict": "PASS",
                        "pass": True,
                        "reason": f"laya auto (p={p_true:.2f})",
                    }
                return {
                    "verdict": "UNVERIFIED",
                    "pass": False,
                    "reason": f"laya auto fail (p={p_true:.2f})",
                    "flagged": True,
                }
        except Exception:  # Laya 故障绝不破坏原流程
            pass
    if llm is None:
        return {"verdict": "SKIP", "pass": None, "reason": "llm disabled (LLMOFF)"}
    prompt = (
        "You are a semantic oracle for API test responses. Judge whether the "
        f"response satisfies the requirement.\nREQUIREMENT: {requirement}\n"
        f"RESPONSE: {response_text[:2000]!r}\n"
        'Return JSON: {"pass": true|false, "reason": "short justification"}.'
    )
    try:
        content = llm.judge(tag, prompt, model="strong")
    except Exception as exc:  # LLM 调用失败 → 降级，绝不中断主链路
        return {"verdict": "UNVERIFIED", "pass": False, "reason": f"llm error: {exc}"}
    from ah.utils.json_utils import robust_json_parse

    if isinstance(content, str):
        content = robust_json_parse(content)
    if not isinstance(content, dict):
        return {
            "verdict": "UNVERIFIED",
            "pass": False,
            "reason": "non-JSON semantic verdict",
        }
    if content.get("pass") is True:
        return {"verdict": "PASS", "pass": True, "reason": str(content.get("reason", ""))}
    # 语义判定失败 → 降级为 UNVERIFIED，交人工复核（不进硬闸）。
    return {
        "verdict": "UNVERIFIED",
        "pass": False,
        "reason": str(content.get("reason", "") or "semantic mismatch (human review)"),
        "flagged": True,
    }
