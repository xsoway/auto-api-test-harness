#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/28 15:35
# @Filename : capture.py
# @Author   : Alan_Hsu
"""HAR 仅本地处理；响应只保留类型指纹，凭据只保留输入占位和作用域摘要。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

SENSITIVE = re.compile(
    r"authorization|cookie|password|passwd|secret|token|api[-_]?key|credential", re.I
)


def digest(value: Any) -> str:
    """计算稳定 JSON 摘要，不经过浮点转换。"""
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def value_type(value: Any) -> str:
    """返回 JSON 类型，布尔值不归入整数。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def typed_key(value: Any) -> str:
    """按 JSON 类型和值建索引，保留大整数精度。"""
    return digest([value_type(value), value])


def escape(part: str) -> str:
    """编码 JSON Pointer 片段。"""
    return part.replace("~", "~0").replace("/", "~1")


def leaves(value: Any, pointer: str = "", selector: str = ""):
    """递归遍历叶子，同时记录 JSON Pointer 和 JMESPath。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if SENSITIVE.search(str(key)):
                continue
            quoted = json.dumps(str(key), ensure_ascii=False)
            yield from leaves(
                item,
                pointer + "/" + escape(str(key)),
                selector + ("." if selector else "") + quoted,
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from leaves(item, pointer + f"/{index}", selector + f"[{index}]")
    else:
        yield {
            "pointer": pointer,
            "selector": selector or "@",
            "value_type": value_type(value),
            "value_hash": typed_key(value),
            "constant_like": value is None or isinstance(value, bool),
        }


def _secret_strings(value: Any, sensitive: bool = False) -> set[str]:
    if isinstance(value, dict):
        return set().union(
            *(
                _secret_strings(item, sensitive or bool(SENSITIVE.search(str(key))))
                for key, item in value.items()
            )
        )
    if isinstance(value, list):
        return set().union(*(_secret_strings(item, sensitive) for item in value))
    if sensitive and isinstance(value, str) and value:
        return {value, value.removeprefix("Bearer ").removeprefix("bearer ")}
    return set()


def _match(request: dict, catalog: dict):
    path = unquote(urlsplit(request.get("url", "")).path)
    matches = []
    for op in catalog["operations"]:
        if op["method"] != request.get("method", "GET").upper():
            continue
        names = re.findall(r"\{([^{}]+)\}", op["path"])
        pattern = "^" + re.sub(r"\\\{[^{}]+\\\}", "([^/]+)", re.escape(op["path"])) + "$"
        matched = re.match(pattern, path)
        if matched:
            matches.append((op, dict(zip(names, matched.groups()))))
    # 同时命中静态路径和模板时，静态路径优先；同等特异性不猜测。
    matches.sort(key=lambda pair: len(re.findall(r"\{", pair[0]["path"])))
    if len(matches) > 1 and matches[0][0]["path"].count("{") == matches[1][0]["path"].count("{"):
        return None, {}
    return matches[0] if matches else (None, {})


def _coerce(value: str, schema: dict):
    kind = schema.get("type")
    try:
        if kind == "integer" and re.fullmatch(r"-?\d+", value):
            return int(value)
        if kind == "number":
            return json.loads(value)
        if kind == "boolean" and value in {"true", "false"}:
            return value == "true"
    except (ValueError, json.JSONDecodeError):
        pass
    return value


def _time(text: str) -> float | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def _capture_secrets(entries: list[dict]) -> set[str]:
    """先汇总整份抓包的凭据，再处理别名字段，避免跨请求泄漏已知秘密。"""
    secrets: set[str] = set()
    for entry in entries:
        for owner in (entry.get("request", {}), entry.get("response", {})):
            headers = {str(h["name"]).lower(): h.get("value", "") for h in owner.get("headers", [])}
            secrets.update(_secret_strings(headers))
            secrets.update(
                cookie.get("value", "")
                for cookie in owner.get("cookies", [])
                if cookie.get("value")
            )
            for key in ("cookie", "set-cookie"):
                try:
                    cookies = SimpleCookie()
                    cookies.load(headers.get(key, ""))
                    secrets.update(cookie.value for cookie in cookies.values() if cookie.value)
                except CookieError:
                    pass  # 无法解码的 Cookie 仍按整个敏感 header 值脱敏。
            text = owner.get("postData", owner.get("content", {})).get("text", "")
            try:
                secrets.update(_secret_strings(json.loads(text)))
            except (ValueError, TypeError):
                pass
        request = entry.get("request", {})
        query = [
            (item.get("name", ""), item.get("value", "")) for item in request.get("queryString", [])
        ]
        query.extend(parse_qsl(urlsplit(request.get("url", "")).query))
        for name, value in query:
            if SENSITIVE.search(name):
                secrets.update(_secret_strings({"secret": value}))
    return secrets


def normalize_har(document: dict, catalog: dict, *, capture_id: str = "capture") -> dict:
    """规范化一个 HAR；绝不返回原始响应或鉴权值。"""
    capture_hash = digest(document)
    result = {
        "capture_hash": capture_hash,
        "steps": [],
        "inputs": {},
        "issues": [],
        "limitations": [],
    }
    entries = document.get("log", {}).get("entries", [])
    known_secrets = _capture_secrets(entries)
    for index, entry in enumerate(entries):
        step_id = f"step-{index + 1}"
        req = entry.get("request", {})
        op, path_values = _match(req, catalog)
        if op is None:
            result["issues"].append(
                {
                    "code": "unknown_operation",
                    "location": step_id,
                    "message": "HAR 请求无法唯一对应目录操作",
                }
            )
            continue
        headers = {str(h["name"]).lower(): h.get("value", "") for h in req.get("headers", [])}
        auth_scope = [
            {name: value for name, value in headers.items() if SENSITIVE.search(name)},
            req.get("cookies", []),
            [
                (item.get("name"), item.get("value"))
                for item in req.get("queryString", [])
                if SENSITIVE.search(item.get("name", ""))
            ],
            [
                (name, value)
                for name, value in parse_qsl(urlsplit(req.get("url", "")).query)
                if SENSITIVE.search(name)
            ],
        ]
        scope = {
            "session": digest([capture_id, entry.get("pageref", "default")]),
            "tenant": digest(entry.get("_tenant", headers.get("x-tenant-id", "unknown"))),
            "auth": digest(auth_scope),
        }
        inputs = result["inputs"]
        secrets = set(known_secrets)

        def redact(value, pointer=""):
            if isinstance(value, dict):
                output = {}
                for key, item in value.items():
                    current = pointer + "/" + escape(str(key))
                    if SENSITIVE.search(str(key)):
                        name = "input_" + digest([step_id, current])[:12]
                        inputs[name] = {"type": value_type(item), "scope": scope, "sensitive": True}
                        output[key] = "${" + name + "}"
                    else:
                        output[key] = redact(item, current)
                return output
            if isinstance(value, list):
                return [redact(item, pointer + f"/{i}") for i, item in enumerate(value)]
            if isinstance(value, str) and any(
                secret == value or (len(secret) >= 4 and secret in value) for secret in secrets
            ):
                name = "input_" + digest([step_id, pointer])[:12]
                inputs[name] = {"type": "string", "scope": scope, "sensitive": True}
                return "${" + name + "}"
            return value

        query_pairs = req.get("queryString")
        if query_pairs is None:
            query_pairs = [
                {"name": k, "value": v} for k, v in parse_qsl(urlsplit(req.get("url", "")).query)
            ]
        query = {}
        for pair in query_pairs:
            if pair["name"] in query:
                result["issues"].append(
                    {
                        "code": "unsupported",
                        "location": step_id,
                        "message": "重复 query 参数需要人工编码",
                    }
                )
            query[pair["name"]] = pair.get("value", "")
        body = None
        post = req.get("postData", {})
        if post:
            if "json" in post.get("mimeType", ""):
                try:
                    body = json.loads(post.get("text", "null"))
                except (ValueError, TypeError):
                    result["issues"].append(
                        {
                            "code": "unsupported",
                            "location": step_id,
                            "message": "请求 JSON 无法解析",
                        }
                    )
            else:
                result["issues"].append(
                    {
                        "code": "unsupported",
                        "location": step_id,
                        "message": "仅支持 JSON 请求体，表单/二进制未支持",
                    }
                )
        request_headers = {
            k: v for k, v in headers.items() if k not in {"host", "content-length", "cookie"}
        }
        if req.get("cookies") or headers.get("cookie"):
            request_headers["cookie"] = "capture-cookie"
        request = {
            "path": path_values,
            "query": query,
            "header": request_headers,
            "cookie": {},
            "body": body,
        }
        secrets.update(_secret_strings(request))
        response_text = entry.get("response", {}).get("content", {}).get("text", "")
        try:
            secrets.update(_secret_strings(json.loads(response_text)))
        except (ValueError, TypeError):
            pass
        if secrets or entry.get("response", {}).get("cookies"):
            result["limitations"].append(
                {
                    "code": "external_auth",
                    "location": step_id,
                    "message": "凭据不匹配；token/Bearer/Cookie 链须用外部 Context 输入，"
                    "未支持凭据提取。",
                }
            )
        for parameter in op.get("parameters", []):
            location = {"path": "path", "query": "query", "header": "header"}.get(
                parameter.get("in")
            )
            name = parameter.get("name", "")
            if location == "header":
                name = name.lower()
            if location and name in request[location] and not SENSITIVE.search(name):
                request[location][name] = _coerce(
                    request[location][name], parameter.get("schema", {})
                )
        request = {location: redact(value, "/" + location) for location, value in request.items()}
        response = entry.get("response", {})
        content = response.get("content", {})
        fields = []
        if content.get("encoding"):
            result["issues"].append(
                {"code": "unsupported", "location": step_id, "message": "编码响应正文未支持"}
            )
        elif content.get("text"):
            try:
                fields = list(leaves(json.loads(content["text"])))
            except (ValueError, TypeError):
                result["issues"].append(
                    {"code": "unsupported", "location": step_id, "message": "响应不是可索引 JSON"}
                )
        for header in response.get("headers", []):
            if not SENSITIVE.search(header.get("name", "")):
                fields.append(
                    {
                        "location": "response.header",
                        "pointer": "/" + header["name"],
                        "selector": header["name"],
                        "value_type": "string",
                        "value_hash": typed_key(header.get("value", "")),
                    }
                )
        started = _time(entry.get("startedDateTime"))
        duration = entry.get("time")
        completed = (
            started + duration / 1000
            if started is not None and isinstance(duration, (int, float)) and duration >= 0
            else None
        )
        result["steps"].append(
            {
                "id": step_id,
                "operation": op["key"],
                "scope": scope,
                "request": request,
                "assertions": {"status_code": response.get("status")},
                "response_fields": fields,
                "started": started,
                "completed": completed,
                "capture_hash": capture_hash,
            }
        )
    return result


def load_har(path: str | Path, catalog: dict) -> dict:
    """从本地 JSON 文件读取 HAR。"""
    return normalize_har(json.loads(Path(path).read_text(encoding="utf-8")), catalog)
