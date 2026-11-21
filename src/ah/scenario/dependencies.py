#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/28 15:35
# @Filename : dependencies.py
# @Author   : Alan_Hsu
"""按类型指纹、时间和作用域发现候选；相等从不意味着确认。"""

from __future__ import annotations

import copy
from typing import Any

from ah.scenario.capture import digest, escape, leaves, normalize_har


def _safe_catalog(value: Any):
    """仅发布支持的 schema 结构；properties 的键是业务字段，不是注解名。"""
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    if "$ref" in value:
        ref = value["$ref"]
        return {"$ref": ref if str(ref).startswith("#/") else "<unsupported-ref>"}
    output = {
        key: copy.deepcopy(value[key]) for key in ("type", "required", "nullable") if key in value
    }
    if isinstance(value.get("properties"), dict):
        output["properties"] = {
            name: _safe_catalog(schema) for name, schema in value["properties"].items()
        }
    for key in ("items", "additionalProperties"):
        if key in value:
            output[key] = _safe_catalog(value[key])
    return output


def _has_constraints(value: Any) -> bool:
    if isinstance(value, dict):
        return bool({"enum", "const"} & value.keys()) or any(
            _has_constraints(item) for item in value.values()
        )
    return isinstance(value, list) and any(_has_constraints(item) for item in value)


def _catalog_snapshot(catalog: dict) -> dict:
    """只保留结构性目录字段；响应 headers、示例和 link 字面值不进入工件。"""
    operations = []
    for operation in catalog["operations"]:
        op = {
            key: operation.get(key) for key in ("key", "service", "method", "path", "operation_id")
        }
        op["parameters"] = [
            {
                key: _safe_catalog(param[key])
                for key in ("name", "in", "required", "schema")
                if key in param
            }
            for param in operation.get("parameters", [])
        ]
        op["request_schema"] = _safe_catalog(operation.get("request_schema"))
        op["responses"] = {
            status: {"schema": _safe_catalog(response.get("schema"))}
            for status, response in operation.get("responses", {}).items()
        }
        op["security"] = [
            {name: [] for name in requirement} for requirement in operation.get("security", [])
        ]
        op["links"] = {}
        for status, links in operation.get("links", {}).items():
            op["links"][status] = {}
            for name, link in links.items():
                safe = {key: link[key] for key in ("operationId", "operationRef") if key in link}
                if "operationRef" in safe and not str(safe["operationRef"]).startswith("#/"):
                    safe["operationRef"] = "<unsupported-ref>"
                safe["parameters"] = {
                    key: value
                    if isinstance(value, str) and value.startswith("$response.body#/")
                    else "<redacted>"
                    for key, value in link.get("parameters", {}).items()
                }
                if "requestBody" in link:
                    safe["requestBody"] = "<redacted>"
                op["links"][status][name] = safe
        operations.append(op)
    return {
        "service": catalog.get("service", "default"),
        "spec_hash": catalog["spec_hash"],
        "redacted_constraints": _has_constraints(catalog),
        "operations": operations,
        "issues": [
            {
                "code": issue.get("code", "unsupported"),
                "message": "目录结构需人工审核",
                "location": issue.get("location", ""),
            }
            for issue in catalog.get("issues", [])
        ],
    }


def _observed(steps: list[dict], spec_hash: str) -> list[dict]:
    index: dict[str, list[tuple[dict, dict]]] = {}
    edges = []
    for target in steps:
        for location, body in target["request"].items():
            for field in leaves(body):
                if field["pointer"] == "" and body is None:
                    continue
                for source, output in index.get(field["value_hash"], []):
                    if source["scope"] != target["scope"]:
                        continue
                    if (
                        source["completed"] is None
                        or target["started"] is None
                        or source["completed"] > target["started"]
                    ):
                        continue
                    edge = {
                        "id": f"edge-{len(edges) + 1}",
                        "source": {
                            "step": source["id"],
                            "location": output.get("location", "response.body"),
                            **{key: output[key] for key in ("pointer", "selector", "value_type")},
                        },
                        "target": {
                            "step": target["id"],
                            "location": "request." + location,
                            "pointer": field["pointer"],
                            "value_type": field["value_type"],
                        },
                        "transform": "identity",
                        "status": "proposed",
                        "constant_like": output.get("constant_like", False),
                        "evidence": [
                            {
                                "kind": "observed",
                                "capture_hash": source["capture_hash"],
                                "spec_hash": spec_hash,
                                "value_hash": field["value_hash"],
                                "scope": source["scope"],
                            }
                        ],
                    }
                    edges.append(edge)
        for output in target["response_fields"]:
            index.setdefault(output["value_hash"], []).append((target, output))
    return edges


def _declared(model: dict) -> None:
    operations = {op["key"]: op for op in model["catalog"]["operations"]}
    by_id = {op.get("operation_id"): op for op in operations.values() if op.get("operation_id")}
    for source in model["steps"]:
        op = operations[source["operation"]]
        for links in op.get("links", {}).values():
            for link in links.values():
                target_op = by_id.get(link.get("operationId"))
                reference = link.get("operationRef", "")
                if (
                    not target_op
                    and isinstance(reference, str)
                    and reference.startswith("#/paths/")
                ):
                    template, _, method = reference[len("#/paths/") :].rpartition("/")
                    template = template.replace("~1", "/").replace("~0", "~")
                    target_op = next(
                        (
                            candidate
                            for candidate in operations.values()
                            if candidate["path"] == template
                            and candidate["method"].lower() == method.lower()
                        ),
                        None,
                    )
                if not target_op:
                    model["issues"].append(
                        {"code": "unsupported_link", "message": "Link 目标无法解析为本地目录操作"}
                    )
                    continue
                targets = [step for step in model["steps"] if step["operation"] == target_op["key"]]
                for target in targets:
                    for name, expression in link.get("parameters", {}).items():
                        params = [
                            p for p in target_op.get("parameters", []) if p.get("name") == name
                        ]
                        if (
                            len(params) != 1
                            or not isinstance(expression, str)
                            or not expression.startswith("$response.body#/")
                        ):
                            model["issues"].append(
                                {
                                    "code": "unsupported_link",
                                    "message": "Link 参数位置或运行时表达式未支持",
                                }
                            )
                            continue
                        pointer = expression[len("$response.body#") :]
                        selector = ".".join(
                            '"' + p.replace("~1", "/").replace("~0", "~") + '"'
                            for p in pointer.lstrip("/").split("/")
                        )
                        existing = next(
                            (
                                e
                                for e in model["edges"]
                                if e["source"]["step"] == source["id"]
                                and e["source"]["pointer"] == pointer
                                and e["target"]["step"] == target["id"]
                                and e["target"]["location"] == "request." + params[0]["in"]
                                and e["target"]["pointer"] == "/" + escape(name)
                            ),
                            None,
                        )
                        evidence = {"kind": "declared", "spec_hash": model["catalog"]["spec_hash"]}
                        if existing:
                            existing["evidence"].append(evidence)
                            continue
                        kind = params[0].get("schema", {}).get("type", "unknown")
                        model["edges"].append(
                            {
                                "id": f"edge-{len(model['edges']) + 1}",
                                "source": {
                                    "step": source["id"],
                                    "location": "response.body",
                                    "pointer": pointer,
                                    "selector": selector,
                                    "value_type": kind,
                                },
                                "target": {
                                    "step": target["id"],
                                    "location": "request." + params[0]["in"],
                                    "pointer": "/" + escape(name),
                                    "value_type": kind,
                                },
                                "transform": "identity",
                                "status": "proposed",
                                "evidence": [evidence],
                            }
                        )
                if link.get("requestBody") is not None:
                    model["issues"].append(
                        {"code": "unsupported_link", "message": "Link requestBody 表达式未支持"}
                    )


def _catalog_steps(catalog: dict, inputs: dict) -> list[dict]:
    steps = []
    scope = {"session": "manual", "tenant": "manual", "auth": "manual"}
    for index, op in enumerate(catalog["operations"]):
        step_id = f"step-{index + 1}"

        def placeholder(pointer: str, schema: dict):
            name = "input_" + digest([step_id, pointer])[:12]
            inputs[name] = {
                "type": schema.get("type", "unknown"),
                "scope": scope,
                "sensitive": False,
            }
            return "${" + name + "}"

        request = {"path": {}, "query": {}, "header": {}, "cookie": {}, "body": None}
        for param in op.get("parameters", []):
            if param.get("required") and param.get("in") in {"path", "query", "header", "cookie"}:
                request[param["in"]][param["name"]] = placeholder(
                    param["in"] + "/" + param["name"], param.get("schema", {})
                )
        if op.get("request_schema"):
            request["body"] = placeholder("body", op["request_schema"])
        codes = [int(code) for code in op.get("responses", {}) if str(code).isdigit()]
        steps.append(
            {
                "id": step_id,
                "operation": op["key"],
                "scope": scope,
                "request": request,
                "assertions": {"status_code": codes[0]} if codes else {},
                "response_fields": [],
                "started": None,
                "completed": None,
            }
        )
    return steps


def discover(
    catalog: dict, captures: list[dict] | None = None, *, name: str = "generated-chain"
) -> dict:
    """生成纯 JSON 草稿；第一会话建链，其余会话仅补充同构证据。"""
    model = {
        "model_version": 1,
        "name": name,
        "catalog": _catalog_snapshot(catalog),
        "capture_hashes": [],
        "inputs": {},
        "steps": [],
        "edges": [],
        "issues": [],
        "limitations": [],
    }
    for op in catalog["operations"]:
        if any(param.get("in") == "formData" for param in op.get("parameters", [])):
            model["issues"].append(
                {
                    "code": "unsupported",
                    "location": op["key"],
                    "message": "Swagger formData 参数暂不支持",
                }
            )
        if any(param.get("in") == "body" for param in op.get("parameters", [])) and not op.get(
            "request_schema"
        ):
            model["issues"].append(
                {
                    "code": "unsupported_type",
                    "location": op["key"],
                    "message": "Swagger body 参数缺少可确定请求 schema",
                }
            )
    captures = captures or []
    if not captures:
        model["steps"] = _catalog_steps(catalog, model["inputs"])
    for capture_index, document in enumerate(captures):
        capture = normalize_har(document, catalog, capture_id=str(capture_index))
        model["capture_hashes"].append(capture["capture_hash"])
        model["issues"].extend(capture["issues"])
        model["limitations"].extend(capture["limitations"])
        sessions = {step["scope"]["session"] for step in capture["steps"]}
        if len(sessions) > 1:
            model["issues"].append(
                {
                    "code": "multiple_sessions",
                    "message": "一个 HAR 包含多会话；请先拆分单条业务会话",
                }
            )
        edges = _observed(capture["steps"], catalog["spec_hash"])
        if capture_index == 0:
            model["steps"] = capture["steps"]
            model["inputs"] = capture["inputs"]
            model["edges"] = edges
        elif [s["operation"] for s in capture["steps"]] != [s["operation"] for s in model["steps"]]:
            model["issues"].append(
                {"code": "capture_alignment", "message": "补充会话操作顺序不同，无法自动对齐"}
            )
        else:
            for edge in edges:
                identity = (edge["source"], edge["target"])
                matched = next(
                    (e for e in model["edges"] if (e["source"], e["target"]) == identity), None
                )
                if matched:
                    matched["evidence"].extend(edge["evidence"])
    _declared(model)
    # 单独保留歧义组，不按最近或最后出现选择生产者。
    groups = {}
    for edge in model["edges"]:
        target = edge["target"]
        key = (target["step"], target["location"], target["pointer"])
        groups.setdefault(key, []).append(edge)
    for alternatives in groups.values():
        if len(alternatives) > 1:
            for edge in alternatives:
                edge["alternatives"] = [other["id"] for other in alternatives if other is not edge]
    return copy.deepcopy(model)
