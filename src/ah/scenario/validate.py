#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/28 15:35
# @Filename : validate.py
# @Author   : Alan_Hsu
"""中间模型的确定性门禁；没有确认、闭包或可执行语义就拒绝编译。"""

from __future__ import annotations

import json
import re
from typing import Any

import jmespath
import jsonschema

from ah.scenario.capture import SENSITIVE, digest, value_type

TYPES = {"string", "integer", "number", "boolean", "null", "object", "array"}
REF = re.compile(r"\$\{([^{}]+)\}")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SCOPE = {
    "type": "object",
    "required": ["session", "tenant", "auth"],
    "properties": {
        key: {"type": "string", "minLength": 1} for key in ("session", "tenant", "auth")
    },
}
ENDPOINT = {
    "type": "object",
    "required": ["step", "location", "pointer", "value_type"],
    "properties": {
        key: {"type": "string"} for key in ("step", "location", "pointer", "value_type", "selector")
    },
}
MODEL_SCHEMA = {
    "type": "object",
    "required": ["model_version", "name", "catalog", "inputs", "steps", "edges", "issues"],
    "properties": {
        "model_version": {"const": 1},
        "name": {"type": "string", "minLength": 1},
        "catalog": {
            "type": "object",
            "required": ["spec_hash", "operations", "issues"],
            "properties": {
                "spec_hash": {"type": "string", "minLength": 1},
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["key", "method", "path"],
                        "properties": {
                            "key": {"type": "string"},
                            "method": {"type": "string"},
                            "path": {"type": "string"},
                        },
                    },
                },
                "issues": {"type": "array", "items": {"type": "object"}},
            },
        },
        "inputs": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["type", "scope"],
                "properties": {
                    "type": {"enum": sorted(TYPES)},
                    "scope": SCOPE,
                    "sensitive": {"type": "boolean"},
                },
            },
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "operation", "scope", "request", "assertions"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "operation": {"type": "string"},
                    "scope": SCOPE,
                    "assertions": {"type": "object"},
                    "response_fields": {"type": "array", "items": {"type": "object"}},
                    "request": {
                        "type": "object",
                        "required": ["path", "query", "header", "cookie", "body"],
                        "properties": {
                            key: {"type": "object"} for key in ("path", "query", "header", "cookie")
                        },
                        "additionalProperties": True,
                    },
                },
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "source", "target", "status", "transform", "evidence"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "source": ENDPOINT,
                    "target": ENDPOINT,
                    "status": {"enum": ["proposed", "confirmed", "verified", "rejected"]},
                    "transform": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["kind", "spec_hash"],
                            "properties": {
                                "kind": {"enum": ["declared", "observed", "inferred"]},
                                "spec_hash": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "issues": {"type": "array", "items": {"type": "object"}},
    },
}


def pointer_parts(pointer: str) -> list[str]:
    """解析严格 JSON Pointer；不接受隐式路径转换。"""
    if pointer == "":
        return []
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError("非法 JSON Pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _array_index(part: str) -> int:
    """数组下标必须为规范十进制，禁止 0/00 指向同一绑定。"""
    if re.fullmatch(r"0|[1-9][0-9]*", part) is None:
        raise ValueError("数组 JSON Pointer 下标不规范")
    return int(part)


def at_pointer(value: Any, pointer: str):
    """读取存在的请求位置；缺失直接报错。"""
    for part in pointer_parts(pointer):
        value = value[_array_index(part)] if isinstance(value, list) else value[part]
    return value


def _schema_at(schema: dict | None, pointer: str) -> dict:
    try:
        parts = pointer_parts(pointer)
    except ValueError:
        return {}
    for part in parts:
        if not isinstance(schema, dict):
            return {}
        if schema.get("type") == "array":
            try:
                _array_index(part)
            except ValueError:
                return {}
            schema = schema.get("items", {})
        else:
            schema = schema.get("properties", {}).get(part, {})
    return schema if isinstance(schema, dict) else {}


def variable_name(edge: dict) -> str:
    """相同生产字段复用同一唯一变量，可由人工显式命名。"""
    source = edge["source"]
    return (
        edge.get("variable")
        or "dep_" + digest([source["step"], source["location"], source.get("selector")])[:16]
    )


def _walk(value: Any, pointer: str = ""):
    yield pointer, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, pointer + "/" + str(key).replace("~", "~0").replace("/", "~1"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, pointer + f"/{index}")


def _assertions(assertions: dict, error, step_id: str) -> None:
    known = {"status_code", "jsonpath", "schema", "contains"}
    if not assertions or set(assertions) - known:
        error("assertions", "断言为空或含不支持的断言", step_id)
    if "status_code" in assertions:
        codes = assertions["status_code"]
        codes = codes if isinstance(codes, list) else [codes]
        if not codes or any(type(code) is not int or not 100 <= code <= 599 for code in codes):
            error("assertions", "状态码必须为明确 HTTP 整数或非空列表", step_id)
    if "jsonpath" in assertions:
        spec = assertions["jsonpath"]
        if not isinstance(spec, dict) or not spec:
            error("assertions", "jsonpath 断言须为非空映射", step_id)
        else:
            for selector, ops in spec.items():
                try:
                    jmespath.compile(selector)
                except (ValueError, TypeError, jmespath.exceptions.JMESPathError):
                    error("assertions", "断言 JMESPath 非法", step_id)
                if isinstance(ops, dict) and (
                    not ops or set(ops) - {"eq", "contains", "type", "lt", "gt"}
                ):
                    error("assertions", "断言操作符未支持", step_id)
    if "schema" in assertions:
        try:
            jsonschema.Draft202012Validator.check_schema(assertions["schema"])
        except (jsonschema.SchemaError, TypeError):
            error("assertions", "响应断言 schema 非法", step_id)
    if "contains" in assertions and not assertions["contains"]:
        error("assertions", "contains 不得为空", step_id)


def _has_selector_node(node: dict, kinds: set[str]) -> bool:
    """检查 JMESPath AST，避免字符串匹配误放行祖先索引/切片。"""
    return node.get("type") in kinds or any(
        _has_selector_node(child, kinds) for child in node.get("children", [])
    )


def _selector_fields(node: dict) -> list[str]:
    """只接受可绑定到响应字段路径的选择，不用函数结果冒充原字段类型。"""
    kind = node.get("type")
    if kind == "field":
        return [node["value"]]
    if kind == "identity":
        return []
    if kind not in {"subexpression", "projection", "filter_projection", "flatten", "pipe"}:
        raise ValueError("业务键选择中存在无法绑定的字段变换")
    children = node.get("children", [])
    if kind == "filter_projection":
        children = children[:2]  # 条件字段不是提取字段的一部分。
    return [field for child in children for field in _selector_fields(child)]


def _request_types(step: dict, op: dict, reference_types: dict, error) -> None:
    request = step["request"]
    for location in set(request) - {"path", "query", "header", "cookie", "body"}:
        error("unsupported", "请求位置未支持", step["id"] + "/" + location)
    if request["cookie"]:
        error("unsupported", "cookie 参数未支持；鉴权 Cookie 请声明外部 header 输入", step["id"])
    expected_path = set(re.findall(r"\{([^{}]+)\}", op["path"]))
    if set(request["path"]) != expected_path:
        error("path", "path 参数必须与目录模板完全一致", step["id"])
    parameters = op.get("parameters", [])
    for parameter in parameters:
        location = parameter.get("in")
        name = parameter.get("name")
        if location == "header" and isinstance(name, str):
            name = name.lower()
        values = request.get(location, {})
        if parameter.get("required") and isinstance(values, dict) and name not in values:
            error("input_closure", "缺少目录必填参数", step["id"])
    schemas = {"body": op.get("request_schema")}
    for location in ("path", "query", "header", "cookie"):
        schemas[location] = {
            "type": "object",
            "properties": {
                (p["name"].lower() if location == "header" else p["name"]): p.get("schema", {})
                for p in parameters
                if p.get("in") == location
            },
        }
    for location, value in request.items():
        if location not in schemas:
            continue
        schema = schemas[location]
        for pointer, item in _walk(value):
            if pointer == "" and location == "body" and not schema and (item is None or item == {}):
                continue
            refs = REF.findall(item) if isinstance(item, str) else []
            field_schema = _schema_at(schema, pointer)
            expected = field_schema.get("type")
            if isinstance(item, dict) and set(field_schema.get("required", [])) - item.keys():
                error("input_closure", "缺少请求体嵌套必填字段", step["id"])
            if expected is None and location in {"body", "path", "query", "cookie"}:
                error(
                    "unsupported_type",
                    "请求字段在目录中没有可确定类型",
                    step["id"] + "/" + location + pointer,
                )
                continue
            actual = value_type(item)
            if refs:
                if len(refs) == 1 and item == "${" + refs[0] + "}":
                    actual = reference_types.get(refs[0])
                    if actual is None:
                        continue  # 未声明变量由闭包校验拒绝。
                else:
                    actual = "string"  # 任意字符串插值的结果始终是文本。
            if (
                expected
                and actual != expected
                and not (expected == "number" and actual == "integer")
            ):
                error("type", "请求值与目录类型不兼容", step["id"] + "/" + location + pointer)
            if any(SENSITIVE.search(part) for part in pointer_parts(pointer)) and not refs:
                error("sensitive", "敏感输入必须使用外部变量，不得嵌入原值", step["id"])
    if op.get("security") and not (
        step.get("auth") or any(SENSITIVE.search(key) for key in request["header"])
    ):
        error("auth", "操作要求鉴权但未声明外部鉴权输入", step["id"])


def validate_model(model: Any) -> dict:
    """返回可序列化的 valid/errors/warnings，不执行网络或表达式代码。"""
    errors, warnings = [], []

    def error(code, message, location=""):
        errors.append({"code": code, "message": message, "location": str(location)})

    structural = list(jsonschema.Draft202012Validator(MODEL_SCHEMA).iter_errors(model))
    if structural:
        for problem in structural:
            error(
                "model_schema",
                "中间模型结构不符合 model_version:1",
                "/".join(map(str, problem.absolute_path)),
            )
        return {"valid": False, "errors": errors, "warnings": warnings}
    if model["catalog"].get("redacted_constraints"):
        warnings.append(
            {
                "code": "redacted_constraints",
                "message": "隐私工件省略 enum/const；仅校验类型、位置与闭包，"
                "不作完整 schema 验证。",
            }
        )
    warnings.extend(model.get("limitations", []))
    for issue in model["issues"] + model["catalog"]["issues"]:
        code = issue.get("code", "unsupported")
        if code in {"missing_operation_id", "missing_operationId"}:
            warnings.append({"code": code, "message": "目录缺 operationId，使用稳定 operation key"})
        else:
            error(code, "目录或捕获存在未解决问题", issue.get("location", ""))
    ops = {op["key"]: op for op in model["catalog"]["operations"]}
    if len(ops) != len(model["catalog"]["operations"]):
        error("duplicate_operation", "目录操作 key 不唯一")
    steps = {step["id"]: step for step in model["steps"]}
    if len(steps) != len(model["steps"]):
        error("duplicate_step", "步骤 id 必须唯一")
    order = {step["id"]: index for index, step in enumerate(model["steps"])}
    edge_ids = [edge["id"] for edge in model["edges"]]
    if len(set(edge_ids)) != len(edge_ids):
        error("duplicate_edge", "关系 id 必须唯一")
    inputs = model["inputs"]
    for name in inputs:
        if not NAME.fullmatch(name):
            error("input_name", "外部变量名非法")
        if "value" in inputs[name] or "default" in inputs[name]:
            error("sensitive", "外部输入声明不能携带原值或默认值")
    producers, targets, graph = {}, {}, {name: set() for name in steps}
    producer_types = {}
    active = [edge for edge in model["edges"] if edge["status"] != "rejected"]
    for edge in active:
        source, target = edge["source"], edge["target"]
        source_step, target_step = steps.get(source["step"]), steps.get(target["step"])
        if edge["status"] not in {"confirmed", "verified"}:
            error("unconfirmed", "候选关系必须经人工确认或拒绝", edge["id"])
        if edge["status"] == "verified" and not edge.get("verification_refs"):
            error("verification", "verified 需要独立运行证据引用", edge["id"])
        if edge["transform"] != "identity":
            error("unsupported", "只支持 identity 转换；不得生成任意表达式", edge["id"])
        if source["location"] != "response.body" or target["location"] not in {
            "request.body",
            "request.path",
            "request.query",
            "request.header",
        }:
            error("unsupported", "响应头/cookie 提取或目标位置未支持", edge["id"])
        if source["value_type"] not in TYPES or target["value_type"] not in TYPES:
            error("unsupported_type", "关系类型未知", edge["id"])
        if source["value_type"] != target["value_type"] and not (
            source["value_type"] == "integer" and target["value_type"] == "number"
        ):
            error("type", "生产者与消费者类型不兼容", edge["id"])
        if not source_step or not target_step:
            error("unknown_step", "关系引用不存在的步骤", edge["id"])
            continue
        if source_step["scope"] != target_step["scope"]:
            error("scope", "关系跨会话、租户或鉴权作用域", edge["id"])
        if order[source["step"]] >= order[target["step"]]:
            error("forward_reference", "生产者必须先于消费者", edge["id"])
        completed, started = source_step.get("completed"), target_step.get("started")
        if (
            isinstance(completed, (int, float))
            and isinstance(started, (int, float))
            and completed > started
        ):
            error("time", "上游响应在目标请求开始时尚不可用", edge["id"])
        graph[target["step"]].add(source["step"])
        selector = source.get("selector")
        selector_ast = None
        try:
            selector_ast = jmespath.compile(selector).parsed
        except (ValueError, TypeError, jmespath.exceptions.JMESPathError):
            error("selector", "生产者须声明合法 JMESPath selector", edge["id"])
        stable_selection = bool(
            edge.get("selection_confirmed") is True
            and selector_ast
            and selector_ast.get("type") == "pipe"
            and re.search(r"\|\s*\[0\]\s*$", selector or "")
            and _has_selector_node(selector_ast["children"][0], {"filter_projection"})
            and not _has_selector_node(selector_ast["children"][0], {"index", "slice"})
        )
        if selector_ast and _has_selector_node(selector_ast, {"index", "slice"}):
            if not stable_selection:
                error(
                    "unstable_selection",
                    "列表位置不是稳定身份，需无位置祖先的业务键筛选",
                    edge["id"],
                )
        try:
            parts = pointer_parts(source["pointer"])
            if stable_selection:
                selected = _selector_fields(selector_ast["children"][0])
                if selected != [part for part in parts if not part.isdigit()]:
                    error("selector", "业务键选择必须保留已声明的源字段路径", edge["id"])
                projection = selector.rsplit("|", 1)[0].strip()
                uniqueness = "length(" + projection + ")"
                assertions = source_step["assertions"].get("jsonpath", {})
                if isinstance(assertions, dict) and uniqueness in assertions:
                    existing = assertions[uniqueness]
                    if not (
                        isinstance(existing, dict)
                        and set(existing) == {"eq"}
                        and type(existing["eq"]) is int
                        and existing["eq"] == 1
                    ):
                        error("assertion_conflict", "唯一选择断言与人工 oracle 冲突", edge["id"])
            canonical = ""
            for part in parts:
                canonical += (
                    f"[{part}]" if part.isdigit() else ("." if canonical else "") + json.dumps(part)
                )
            if (
                not stable_selection
                and jmespath.compile(selector).parsed != jmespath.compile(canonical or "@").parsed
            ):
                error("selector", "selector 与已声明源字段不一致", edge["id"])
            location = target["location"].removeprefix("request.")
            target_value = at_pointer(target_step["request"][location], target["pointer"])
            actual = value_type(target_value)
            if (
                not (isinstance(target_value, str) and REF.search(target_value))
                and actual != target["value_type"]
            ):
                error("type", "目标声明类型与请求值不符", edge["id"])
            op = ops.get(target_step["operation"], {})
            if location == "body":
                schema = _schema_at(op.get("request_schema"), target["pointer"])
            else:
                parts = pointer_parts(target["pointer"])
                schema = next(
                    (
                        p.get("schema", {})
                        for p in op.get("parameters", [])
                        if p.get("in") == location and parts == [p.get("name")]
                    ),
                    {},
                )
            if schema.get("type") and schema["type"] != target["value_type"]:
                error("type", "关系目标与目录字段类型不符", edge["id"])
        except (KeyError, IndexError, ValueError, TypeError, jmespath.exceptions.JMESPathError):
            error("target", "目标 JSON Pointer 不存在或非法", edge["id"])
        recorded = [
            field
            for field in source_step.get("response_fields", [])
            if field.get("pointer") == source["pointer"]
            and field.get("location", "response.body") == source["location"]
        ]
        response_types = [field.get("value_type") for field in recorded]
        op = ops.get(source_step["operation"], {})
        declared_types = [
            _schema_at(resp.get("schema"), source["pointer"]).get("type")
            for resp in op.get("responses", {}).values()
            if isinstance(resp, dict)
        ]
        if source["value_type"] not in response_types + declared_types:
            error("source_type", "生产者字段不在捕获或目录响应类型中", edge["id"])
        for evidence in edge["evidence"]:
            if evidence["spec_hash"] != model["catalog"]["spec_hash"]:
                error("evidence", "证据与目录摘要不一致", edge["id"])
            if evidence["kind"] == "observed" and (
                not evidence.get("capture_hash") or not evidence.get("scope")
            ):
                error("evidence", "observed 必须保留捕获摘要和作用域", edge["id"])
            if evidence["kind"] == "observed" and evidence.get("capture_hash") not in model.get(
                "capture_hashes", []
            ):
                error("evidence", "捕获证据摘要未登记在模型中", edge["id"])
        name = variable_name(edge)
        origin = (source["step"], source["location"], selector)
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in inputs:
            error("variable", "提取变量名非法或与外部输入重名", edge["id"])
        if name in producers and producers[name] != origin:
            error("multiple_producers", "变量存在多个生产者", edge["id"])
        producers[name] = origin
        producer_types[name] = source["value_type"]
        key = (target["step"], target["location"], target["pointer"])
        if key in targets:
            error("ambiguous", "同一目标有多个来源；必须明确拒绝其他候选", edge["id"])
        targets[key] = edge
    for step in model["steps"]:
        op = ops.get(step["operation"])
        if op is None:
            error("unknown_operation", "步骤操作不在接口目录", step["id"])
        else:
            reference_types = {name: declaration["type"] for name, declaration in inputs.items()}
            reference_types.update(producer_types)
            _request_types(step, op, reference_types, error)
        _assertions(step["assertions"], error, step["id"])
        if set(step["assertions"]) == {"status_code"}:
            warnings.append(
                {
                    "code": "transport_only",
                    "location": step["id"],
                    "message": "只有 HTTP 状态码断言；业务终态未验证",
                }
            )
        for _, item in _walk({"request": step["request"], "assertions": step["assertions"]}):
            for name in REF.findall(item) if isinstance(item, str) else []:
                if name in inputs:
                    if inputs[name]["scope"] != step["scope"]:
                        error("scope", "外部输入跨作用域使用", step["id"])
                elif name in producers:
                    producer = producers[name][0]
                    declared = graph[step["id"]] | set(step.get("depends_on", []))
                    if order[producer] >= order[step["id"]] or producer not in declared:
                        error("forward_reference", "变量引用须有先序显式依赖", step["id"])
                else:
                    error("orphan", "变量无已声明输入或唯一生产者", step["id"])
        for dependency in step.get("depends_on", []):
            if dependency not in steps or order.get(dependency, len(steps)) >= order[step["id"]]:
                error("forward_reference", "控制依赖必须存在且在先", step["id"])
            else:
                graph[step["id"]].add(dependency)
                if steps[dependency]["scope"] != step["scope"]:
                    error("scope", "控制依赖跨作用域", step["id"])
    visiting, visited = set(), set()

    def visit(name):
        if name in visiting:
            error("cycle", "依赖图含环", name)
            return
        if name in visited:
            return
        visiting.add(name)
        for dependency in graph[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in graph:
        visit(name)
    return {"valid": not errors, "errors": errors, "warnings": warnings}
