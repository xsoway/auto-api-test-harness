#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/28 15:30
# @Filename : catalog.py
# @Author   : Alan_Hsu
"""离线 OpenAPI 目录：稳定操作标识、局部引用与明确的不支持说明。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml

_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")
_UNSUPPORTED_SCHEMA = ("allOf", "anyOf", "oneOf", "not", "if", "then", "else", "prefixItems")


class _CatalogReader:
    def __init__(self, document: dict) -> None:
        self.document = document
        self.issues: list[dict] = []

    def issue(self, code: str, message: str, location: str) -> None:
        item = {"code": code, "message": message, "location": location}
        if item not in self.issues:
            self.issues.append(item)

    def resolve(self, value: Any, location: str, refs: tuple = (), nodes: tuple = ()) -> Any:
        if not isinstance(value, (dict, list)):
            return value
        if id(value) in nodes:
            self.issue("cyclic_structure", "YAML 对象存在循环，无法展开", location)
            return {}
        nodes = (*nodes, id(value))
        if isinstance(value, list):
            return [self.resolve(v, f"{location}/{i}", refs, nodes) for i, v in enumerate(value)]
        if "$ref" in value:
            ref = value["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                self.issue(
                    "unsupported_ref", "仅支持文档内 JSON Pointer $ref；不会下载外部引用", location
                )
                return {"$ref": ref}
            if ref in refs:
                self.issue("cyclic_ref", f"循环引用未展开：{ref}", location)
                return {"$ref": ref}
            target: Any = self.document
            try:
                for part in unquote(ref[2:]).split("/"):
                    token = part.replace("~1", "/").replace("~0", "~")
                    target = target[int(token)] if isinstance(target, list) else target[token]
            except (KeyError, IndexError, TypeError, ValueError):
                self.issue("unresolved_ref", f"引用目标不存在：{ref}", location)
                return {"$ref": ref}
            if not isinstance(target, dict):
                self.issue("unsupported_ref", f"引用目标不是对象：{ref}", location)
                return {"$ref": ref}
            if len(value) > 1:
                self.issue(
                    "unsupported_ref_siblings", "$ref 同级字段不合并；仅使用引用目标", location
                )
            return self.resolve(target, location, (*refs, ref), nodes)
        return {str(k): self.resolve(v, f"{location}/{k}", refs, nodes) for k, v in value.items()}

    def schema(self, value: Any, location: str) -> dict | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            self.issue("unsupported_schema", "仅支持对象形式的 JSON Schema", location)
            return None
        for keyword in _UNSUPPORTED_SCHEMA:
            if keyword in value:
                self.issue("unsupported_schema", f"尚不支持 schema 组合/条件：{keyword}", location)
        required = value.get("required", [])
        if not isinstance(required, list) or any(not isinstance(k, str) for k in required):
            self.issue("unsupported_schema", "required 必须为字段名列表", location)
        props = value.get("properties", {})
        if not isinstance(props, dict):
            self.issue("unsupported_schema", "properties 必须为对象", location)
        else:
            for name, child in props.items():
                self.schema(child, f"{location}/properties/{name}")
        if "items" in value:
            self.schema(value["items"], f"{location}/items")
        return value

    def content_schema(self, owner: dict, location: str) -> dict | None:
        if "content" not in owner:
            return self.schema(owner.get("schema"), f"{location}/schema")
        content = owner["content"]
        if not isinstance(content, dict):
            self.issue("unsupported_content", "content 必须为对象", location)
            return None
        for media in content:
            if media != "application/json":
                self.issue("unsupported_media_type", f"不挖掘媒体类型：{media}", location)
        media = content.get("application/json")
        if media is None:
            return None
        if not isinstance(media, dict):
            self.issue("unsupported_content", "application/json 必须为对象", location)
            return None
        return self.schema(media.get("schema"), f"{location}/content/application~1json/schema")

    def parameters(self, value: Any, location: str) -> list[dict]:
        if not isinstance(value, list):
            self.issue("unsupported_parameters", "parameters 必须为列表", location)
            return []
        parameters = []
        for i, param in enumerate(value):
            loc = f"{location}/{i}"
            if not isinstance(param, dict) or not isinstance(param.get("name"), str):
                self.issue("unsupported_parameter", "参数必须具有 name/in", loc)
                continue
            if param.get("in") not in ("path", "query", "header", "cookie", "body", "formData"):
                self.issue("unsupported_parameter", "参数位置不支持", loc)
                continue
            item = dict(param)
            if "content" in item:
                item["schema"] = self.content_schema(item, loc)
            elif "schema" in item:
                item["schema"] = self.schema(item["schema"], f"{loc}/schema")
            else:
                item["schema"] = {
                    k: item[k]
                    for k in ("type", "format", "items", "enum", "default", "minimum", "maximum")
                    if k in item
                }
            item.setdefault("required", False)
            parameters.append(item)
        return parameters

    def operation(self, path: str, method: str, item: dict, op: dict, service: str) -> dict:
        loc = f"#/paths/{path}/{method}"
        params = self.parameters(item.get("parameters", []), f"{loc}/path_parameters")
        merged = {(p["in"], p["name"]): p for p in params}
        for param in self.parameters(op.get("parameters", []), f"{loc}/parameters"):
            merged[(param["in"], param["name"])] = param
        request_schema = None
        body = op.get("requestBody")
        if body is not None:
            if isinstance(body, dict):
                request_schema = self.content_schema(body, f"{loc}/requestBody")
            else:
                self.issue("unsupported_request_body", "requestBody 必须为对象", loc)
        else:
            bodies = [p for p in merged.values() if p["in"] == "body"]
            if bodies:
                request_schema = bodies[0]["schema"]
        responses: dict = {}
        raw_responses = op.get("responses", {})
        if not isinstance(raw_responses, dict) or not raw_responses:
            self.issue("unsupported_responses", "responses 必须为非空对象", loc)
        else:
            for code, response in raw_responses.items():
                if not isinstance(response, dict):
                    self.issue(
                        "unsupported_response", "响应描述必须为对象", f"{loc}/responses/{code}"
                    )
                    continue
                responses[str(code)] = dict(response)
                responses[str(code)]["schema"] = self.content_schema(
                    response, f"{loc}/responses/{code}"
                )
        operation_id = op.get("operationId")
        if not isinstance(operation_id, str) or not operation_id.strip():
            self.issue("missing_operation_id", "operationId 缺失或无效；操作由 key 标识", loc)
            operation_id = None
        security = op.get("security", self.document.get("security", []))
        if not isinstance(security, list):
            self.issue("unsupported_security", "security 必须为列表", loc)
            security = []
        links = {}
        for code, response in responses.items():
            if "links" in response:
                if isinstance(response["links"], dict):
                    links[code] = response["links"]
                else:
                    self.issue("unsupported_links", "响应 links 必须为对象", loc)
        if "callbacks" in op:
            self.issue("unsupported_callbacks", "不展开 callback 操作", loc)
        return {
            "key": f"{service} {method.upper()} {path}",
            "service": service,
            "method": method.upper(),
            "path": path,
            "operation_id": operation_id,
            "parameters": list(merged.values()),
            "request_schema": request_schema,
            "responses": responses,
            "security": security,
            "links": links,
        }


def _canonical(value: Any, seen: tuple = ()) -> Any:
    """统一 YAML 状态码键，并拒绝非 JSON 或循环结构。"""
    if isinstance(value, (dict, list)):
        if id(value) in seen:
            raise ValueError("OpenAPI 文档包含循环 YAML 对象")
        seen = (*seen, id(value))
        if isinstance(value, dict):
            return {str(k): _canonical(v, seen) for k, v in value.items()}
        return [_canonical(v, seen) for v in value]
    return value


def normalize_catalog(document: dict, service: str = "default") -> dict:
    """归一化目录，返回 service/spec_hash/operations/issues 四个字段。

    操作以 ``service METHOD /template`` 为稳定 key；operationId 仅作元数据，
    缺失/冲突均报告。响应状态码统一为字符串，schema 从 JSON content 提升；
    links 按响应状态码分组。不支持项保留证据并列入 issues，不访问网络。
    """
    if not isinstance(document, dict):
        raise ValueError("OpenAPI 文档顶层必须为对象")
    if not isinstance(service, str) or not service.strip():
        raise ValueError("service 必须为非空字符串")
    canonical = _canonical(document)
    encoded = json.dumps(
        canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    reader = _CatalogReader(canonical)
    catalog = {
        "service": service,
        "spec_hash": hashlib.sha256(encoded).hexdigest(),
        "operations": [],
        "issues": reader.issues,
    }
    version = canonical.get("openapi", canonical.get("swagger"))
    if version is not None and not str(version).startswith(("2.", "3.")):
        reader.issue("unsupported_version", f"不支持 OpenAPI 版本：{version}", "#")
        return catalog
    paths = canonical.get("paths")
    if not isinstance(paths, dict):
        reader.issue("unsupported_paths", "paths 必须为对象", "#/paths")
        return catalog
    operation_ids: dict[str, str] = {}
    for path, raw_item in paths.items():
        loc = f"#/paths/{path}"
        if path.startswith("x-"):
            continue
        if not path.startswith("/"):
            reader.issue("unsupported_path", "路径模板必须以 / 开头", loc)
            continue
        item = reader.resolve(raw_item, loc)
        if not isinstance(item, dict):
            reader.issue("unsupported_path_item", "path item 必须为对象", loc)
            continue
        for method in _METHODS:
            if method not in item:
                continue
            op = item[method]
            if not isinstance(op, dict) or "$ref" in op:
                reader.issue("unsupported_operation", "操作必须为可解析对象", f"{loc}/{method}")
                continue
            operation = reader.operation(path, method, item, op, service)
            catalog["operations"].append(operation)
            op_id = operation["operation_id"]
            if op_id is not None:
                if op_id in operation_ids:
                    reader.issue(
                        "conflicting_operation_id",
                        f"重复 operationId {op_id!r}：{operation_ids[op_id]} 与 {operation['key']}",
                        f"{loc}/{method}",
                    )
                else:
                    operation_ids[op_id] = operation["key"]
    for name in ("webhooks",):
        if name in canonical:
            reader.issue("unsupported_structure", f"不展开 {name}", f"#/{name}")
    return catalog


def load_catalog(path: str | Path, service: str = "default") -> dict:
    """从本地 JSON/YAML 读取目录；外部 $ref 仅报告，不下载。"""
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return normalize_catalog(document, service)
