#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/28 15:35
# @Filename : cli.py
# @Author   : Alan_Hsu
"""ah scenario discover/validate/compile：只读本地材料，排他创建人工审核工件。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ah.scenario.catalog import load_catalog
from ah.scenario.compile import modeling_notes, write_json, write_scenario
from ah.scenario.dependencies import discover
from ah.scenario.modeler import suggest_operations
from ah.scenario.validate import validate_model


def register(subparsers: argparse._SubParsersAction) -> None:
    """注册本地场景生成、JSON 校验和确认后编译命令。"""
    parser = subparsers.add_parser("scenario", help="离线发现、审核和编译接口依赖链")
    actions = parser.add_subparsers(dest="scenario_action", required=True)
    discovery = actions.add_parser("discover", help="OpenAPI/HAR 生成待审核 relationships.json")
    discovery.add_argument("--openapi", required=True)
    discovery.add_argument(
        "--har", action="append", default=[], help="首个会话建链；可重复指定同构会话补证"
    )
    discovery.add_argument("--service", default="default")
    discovery.add_argument("--name", default="generated-chain")
    discovery.add_argument("--output-dir", required=True, help="必须为不存在的目录，且不在 data 下")
    discovery.set_defaults(func=_discover)
    validation = actions.add_parser("validate", help="审核中间模型；stdout 输出 JSON；失败退出 1")
    validation.add_argument("model")
    validation.add_argument("--output", help="排他写入 JSON 校验结果")
    validation.set_defaults(func=_validate)
    compilation = actions.add_parser("compile", help="只将严格校验通过的模型编译为 v2 YAML")
    compilation.add_argument("model")
    compilation.add_argument("--output", required=True)
    compilation.add_argument("--notes", help="排他写入 Markdown 建模说明")
    compilation.set_defaults(func=_compile)


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _failure(exc: Exception) -> int:
    # 解析异常可能携带原文或 URL 凭据；公共错误只暴露类别。
    print(
        json.dumps(
            {
                "valid": False,
                "errors": [
                    {
                        "code": type(exc).__name__,
                        "message": "输入无法处理或输出已存在；检查本地文件及命令参数",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    return 1


def _discover(args: argparse.Namespace) -> int:
    try:
        output = Path(args.output_dir).resolve()
        if "data" in output.parts:
            raise ValueError("草稿不得放入用例自动发现的 data 目录")
        catalog = load_catalog(args.openapi, service=args.service)
        model = discover(catalog, [_load(path) for path in args.har], name=args.name)
        model["modeling"] = suggest_operations(model["catalog"])
        validation = validate_model(model)
        notes = modeling_notes(model, validation)
        # 所有输入解析完成后才创建输出目录；目录已存在即拒绝。
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / "relationships.json", model)
        write_json(output / "validation.json", validation)
        with (output / "modeling_notes.md").open("x", encoding="utf-8") as stream:
            stream.write(notes)
        print(
            json.dumps(
                {
                    "output_dir": str(output),
                    "valid": validation["valid"],
                    "steps": len(model["steps"]),
                    "candidates": len(model["edges"]),
                    "business_verified": False,
                },
                ensure_ascii=False,
            )
        )
        # 发现未确认关系属于正常结果，不与 validate 的拒绝编译退出码混同。
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _failure(exc)


def _validate(args: argparse.Namespace) -> int:
    try:
        result = validate_model(_load(args.model))
        if args.output:
            write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["valid"] else 1
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _failure(exc)


def _compile(args: argparse.Namespace) -> int:
    try:
        model = _load(args.model)
        if args.notes and Path(args.notes).exists():
            raise FileExistsError(args.notes)
        if args.notes and Path(args.notes).resolve() == Path(args.output).resolve():
            raise ValueError("YAML 与说明不得使用同一输出文件")
        notes = modeling_notes(model)
        output = write_scenario(model, args.output)
        if args.notes:
            with Path(args.notes).open("x", encoding="utf-8") as stream:
                stream.write(notes)
        print(
            json.dumps(
                {"output": str(output), "schema_version": 2, "business_verified": False},
                ensure_ascii=False,
            )
        )
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _failure(exc)
