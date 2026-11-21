#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/28 15:35
# @Filename : compile.py
# @Author   : Alan_Hsu
"""已确认的中间模型编译为现有 loader 可加载的 v2 场景。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml

from ah.scenario.validate import REF, pointer_parts, validate_model, variable_name


def _replace(value: Any, pointer: str, replacement: Any):
    parts = pointer_parts(pointer)
    if not parts:
        return replacement
    current = value
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    final = int(parts[-1]) if isinstance(current, list) else parts[-1]
    current[final] = replacement
    return value


def compile_model(model: dict) -> dict:
    """校验通过才生成 YAML 映射，不执行请求，不填充运行凭据。"""
    validation = validate_model(model)
    if not validation["valid"]:
        codes = sorted({item["code"] for item in validation["errors"]})
        raise ValueError("中间模型不可编译: " + ", ".join(codes))
    operations = {op["key"]: op for op in model["catalog"]["operations"]}
    steps = {step["id"]: copy.deepcopy(step) for step in model["steps"]}
    extracts = {name: {} for name in steps}
    dependencies = {name: set(step.get("depends_on", [])) for name, step in steps.items()}
    for edge in model["edges"]:
        if edge["status"] == "rejected":
            continue
        source, target = edge["source"], edge["target"]
        name = variable_name(edge)
        extracts[source["step"]][name] = {"path": source["selector"], "extract_type": "jsonpath"}
        if edge.get("selection_confirmed") and "[?" in source["selector"]:
            # 业务键筛选必须唯一；重复键不能退化为“取第一条”。
            projection = source["selector"].rsplit("|", 1)[0].strip()
            assertions = steps[source["step"]]["assertions"]
            assertions.setdefault("jsonpath", {})["length(" + projection + ")"] = {"eq": 1}
        location = target["location"].removeprefix("request.")
        request = steps[target["step"]]["request"]
        request[location] = _replace(request[location], target["pointer"], "${" + name + "}")
        dependencies[target["step"]].add(source["step"])
    compiled = []
    order = {step["id"]: index for index, step in enumerate(model["steps"])}
    for step in steps.values():
        op = operations[step["operation"]]
        raw = step["request"]
        path = op["path"]
        for name, value in raw["path"].items():
            path = path.replace("{" + name + "}", str(value))
        request = {"method": op["method"], "path": path}
        if op.get("service", "default") != "default":
            request["host"] = op["service"]
        for location, key in (("query", "params"), ("header", "headers")):
            if raw[location]:
                request[key] = raw[location]
        if raw["body"] is not None:
            request["json"] = raw["body"]
        case = {
            "name": step["id"],
            "request": request,
            "assert": step["assertions"],
            "depends_on": sorted(dependencies[step["id"]], key=order.get),
        }
        if extracts[step["id"]]:
            case["extract"] = extracts[step["id"]]
        if step.get("auth"):
            case["auth"] = step["auth"]
        required = set(REF.findall(json.dumps(case, ensure_ascii=False))) & model["inputs"].keys()
        if required:
            case["vars_req"] = sorted(required)
        compiled.append(case)
    return {"schema_version": 2, "name": model["name"], "on_failure": "stop", "scenario": compiled}


def modeling_notes(model: dict, validation: dict | None = None) -> str:
    """生成明确区分 HTTP 契约与业务 oracle 的人工建模说明。"""
    validation = validation or validate_model(model)
    notes = [
        "# 离线场景建模说明",
        "",
        "只处理本地 OpenAPI/HAR；未访问真实服务，未验证真实业务。",
        "HTTP 状态码不是业务 oracle；人工补业务断言、清理与权限前提。",
        "",
        f"目录 SHA-256：{model['catalog']['spec_hash']}",
        "捕获 SHA-256：" + ", ".join(model.get("capture_hashes", [])),
        f"静态校验：{'通过' if validation['valid'] else '未通过，不可编译'}",
        "",
        "## 人工编辑 JSON 契约",
        "",
        "model_version=1；steps 为执行顺序，id 唯一，operation 引用目录 key。",
        "request 按 body/path/query/header/cookie 分区；pointer 使用 RFC 6901，根为 ''。",
        "inputs: {name: {type, scope, sensitive}}；scope 含 session/tenant/auth，不保存凭据。",
        "steps.assertions 复用 assert 语法，禁止编造业务预期。",
        "steps.depends_on 可声明先序、同作用域的控制依赖；不支持并发/循环/条件/签名转换。",
        "edges.source: {step,location,pointer,selector,value_type}；selector 使用 JMESPath。",
        "edges.target: {step,location,pointer,value_type}；transform 仅 identity。",
        "evidence.kind=declared/observed/inferred；status 单独表示审核状态。",
        "值相等只为 proposed；确认正确来源为 confirmed，其余候选改 rejected。",
        "verified 还需 verification_refs 引用独立运行证据，不得用捕获相等冒充验证。",
        "列表需业务键 JMESPath 与 selection_confirmed:true，不能只翻转状态。",
        "鉴权凭据仅作外部输入；Cookie 参数和响应头提取显式不支持。",
        "更新输入后重新审核哈希与 scope，不允许跨租户值流。",
        "所有输出采用排他创建，不覆盖已有人工产物；discover 草稿禁止放入 data 目录。",
        "",
        "## 关系",
        "",
    ]
    for edge in model["edges"]:
        source, target = edge["source"], edge["target"]
        notes.append(
            f"- {edge['id']} [{edge['status']}]: {source['step']}:{source.get('selector', '')} → "
            f"{target['step']}:{target['location']}{target['pointer']}"
        )
    notes.extend(["", "## 外部输入（由运行 Context 注入，不含原值）", ""])
    notes.extend(f"- {name}: {item['type']}" for name, item in model["inputs"].items())
    notes.extend(["", "## 校验问题", ""])
    notes.extend(
        f"- {item['code']}: {item['message']} ({item.get('location', '')})"
        for item in validation["errors"] + validation["warnings"]
    )
    return "\n".join(notes) + "\n"


def write_scenario(model: dict, path: str | Path) -> Path:
    """排他写入已确认 YAML；文件存在时绝不覆盖。"""
    compiled = compile_model(model)
    output = Path(path)
    with output.open("x", encoding="utf-8") as stream:
        yaml.safe_dump(compiled, stream, allow_unicode=True, sort_keys=False)
    return output


def write_json(path: str | Path, value: Any) -> None:
    """排他保存 JSON 工件。"""
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
