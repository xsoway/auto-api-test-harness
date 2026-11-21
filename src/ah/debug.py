#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/09 18:00
# @Filename : debug.py
# @Author   : Alan_Hsu
"""调试工具子命令：``ah debug`` 与 ``ah openapi``。

- ``ah debug <project> <case>``：单用例独立执行并 dump 请求/响应/断言/判定，
  便于快速定位单个用例的问题（区别于完整调度跑批）。
- ``ah openapi <project> <file.yaml>``：把 OpenAPI 3.x 的 paths 导入为最小可用
  用例（每个 path×method 一条 Case，断言 200），写入项目 data/ 目录。

诚实边界：OpenAPI 导入只做「paths → cases」最小可用（不含鉴权/安全方案、参数
schema 语义生成、全特性覆盖）。LLMOFF 下零网络；debug 默认走 settings.base_url
实连，``--stub`` 走 hermetic 桩。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from ah.core.config import settings
from ah.core.orchestrator import HTTPClient, run_single
from ah.harness.loader import Case, discover_case_files, load_cases
from ah.scenario.catalog import load_catalog, normalize_catalog
from ah.utils.context import Context
from ah.utils.logging import get_logger

log = get_logger("ah.debug")


def _find_case(project: str, case_name: str) -> Case:
    """在项目数据目录中按名字查找用例（跨文件、含场景步骤扁平化）。"""
    direct = Path(project)
    data_dir = direct / "data" if (direct / "data").is_dir() else direct
    if not data_dir.is_dir():
        candidate = Path("projects") / project / "data"
        if candidate.is_dir():
            data_dir = candidate
        else:
            raise FileNotFoundError(
                f"未找到项目目录（需含 data/）：{project}（也尝试过 {candidate}）"
            )
    files = discover_case_files(data_dir)
    if not files:
        raise FileNotFoundError(f"{data_dir} 下没有用例 YAML")
    for f in files:
        for case in load_cases(f):
            if case.name == case_name:
                return case
    raise FileNotFoundError(
        f"项目 {project!r} 中找不到名为 {case_name!r} 的用例（数据目录：{data_dir}）"
    )


def _base_url_for(case: Case) -> str:
    """取请求目标 base_url：用例 request.host 优先，其次 settings.base_url。"""
    host = getattr(case, "host", None) or getattr(case.request, "host", None)
    if host:
        return str(host)
    return settings.base_url


def _dump(step) -> None:
    """把一次执行结果以可读形式打印到 stdout（供调试）。"""
    req = step.request_detail
    print(f"[debug] 用例: {step.name}")
    print(f"[debug] 请求: {req.get('method')} {req.get('path')}"
          + (f" params={req.get('params')}" if req.get("params") else "")
          + (f" json={json.dumps(req.get('json'), ensure_ascii=False, default=str)}"
             if req.get("json") else ""))
    print(f"[debug] 状态: {step.status_code}  判定: {'PASS' if step.succeeded else 'FAIL'}"
          f"  尝试: {step.attempts}")
    if step.extracted:
        print(f"[debug] 提取: {json.dumps(step.extracted, ensure_ascii=False, default=str)}")
    for idx, a in enumerate(step.assertions, 1):
        mark = "ok" if a.get("passed") else "XX"
        print(f"[debug]   断言#{idx}[{mark}] {a.get('type')} "
              f"期望={a.get('expected')!r} 实际={a.get('actual')!r}"
              + (" 说明=" + str(a.get("note", "")) if a.get("note") else ""))
    err = step.error
    if err:
        print(f"[debug] 失败: {err}")
    if step.llm_notes:
        notes = {k: v for k, v in step.llm_notes.items() if k in ("healed", "retried", "L1_retry")}
        if notes:
            print(f"[debug] 自愈/重试: {json.dumps(notes, ensure_ascii=False, default=str)}")
    if step.response is not None:
        body = step.response.text
        if body:
            print(f"[debug] 响应体({len(body)}B): {body[:500]}"
                  + ("…" if len(body) > 500 else ""))


def _run_debug(args: argparse.Namespace) -> int:
    """执行单个用例并 dump。0=执行完成（含判定失败）；2=环境/参数错误。"""
    try:
        case = _find_case(args.project, args.case_name)
    except FileNotFoundError as exc:
        print(f"[debug] {exc}", file=sys.stderr)
        return 2

    url = _base_url_for(case)
    if getattr(args, "stub", False):
        import httpx
        raw = httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={})
        ))
        client = HTTPClient(url, client=raw, timeout=5.0)
    else:
        client = HTTPClient(url)
    try:
        step = run_single(case, Context(), client)
        _dump(step)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - 关闭客户端失败不影响结果
            pass
    return 0


# ───────────────────────── OpenAPI 导入（最小可用） ─────────────────────────


def _openapi_paths(spec: dict) -> list[tuple[str, dict, str]]:
    """复用目录解析，提取全部标准 HTTP 操作；不生成参数/鉴权语义。"""
    return [
        (op["path"], op, op["method"])
        for op in normalize_catalog(spec)["operations"]
    ]


def _case_name(path: str, method: str) -> str:
    """生成稳定的用例名：``<path>_<method>`` 保留路径片段、安全化。"""
    seg = path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    seg = re.sub(r"[^A-Za-z0-9_]+", "_", seg).strip("_").lower()
    return f"{seg or 'root'}_{method.lower()}"


def _openapi_to_cases(spec: dict) -> list[Case]:
    """把 OpenAPI 3.x 规范转成最小可用用例列表（断言 200）。"""
    cases: list[Case] = []
    for path, op, method in _openapi_paths(spec):
        cases.append(Case(
            name=_case_name(path, method),
            request={"method": method, "path": path},
            assert_={"status_code": 200},
            suite="regression",
        ))
    return cases


def _run_openapi(args: argparse.Namespace) -> int:
    """导入 OpenAPI paths → 最小用例，写入项目 data/。0=成功；2=参数/解析错误。"""
    src = Path(args.file)
    if not src.is_file():
        print(f"[openapi] 文件不存在：{args.file}", file=sys.stderr)
        return 2
    try:
        catalog = load_catalog(src)
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        print(f"[openapi] 解析 {src} 失败：{exc}", file=sys.stderr)
        return 2
    for issue in catalog["issues"]:
        print(f"[openapi] {issue['code']}: {issue['message']}", file=sys.stderr)

    cases = [
        Case(name=_case_name(op["path"], op["method"]),
             request={"method": op["method"], "path": op["path"]},
             assert_={"status_code": 200}, suite="regression")
        for op in catalog["operations"]
    ]
    if not cases:
        print("[openapi] 未解析出任何 paths 用例（检查路径/方法/operationId）",
              file=sys.stderr)
        return 2

    # 目标项目数据目录：显式路径或 projects/<project>/data。
    direct = Path(args.project)
    if (direct / "data").is_dir():
        data_dir = direct / "data"
    else:
        data_dir = Path("projects") / args.project / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    name = (args.name or src.stem or "openapi").strip().lower()
    dst = data_dir / f"{name}.yaml"

    # 使用单个 cases 文档，保证 load_cases 能重新加载全部操作。
    document = {"cases": [
        {"name": case.name, "request": case.request, "assert": case.assert_,
         "suite": case.suite}
        for case in cases
    ]}
    header = (
        f"# 由 `ah openapi {args.file}` 导入生成（最小可用：paths → 用例，断言 200）。\n"
        f"# 未覆盖：鉴权/安全方案、参数 schema 语义、全特性。请在导入后人工补充完善。\n"
    )
    try:
        # 显式独占发布，避免再次导入覆盖人工完善后的用例。
        with dst.open("x", encoding="utf-8") as output:
            output.write(header + yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
    except FileExistsError:
        print(f"[openapi] 目标已存在，拒绝覆盖：{dst}", file=sys.stderr)
        return 2
    print(f"[openapi] 导入 {len(cases)} 个用例 -> {dst}")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """把 ``debug`` / ``openapi`` 子命令挂到 argparse subparsers。"""
    p_db = subparsers.add_parser(
        "debug",
        help="单个用例独立执行并 dump 请求/响应/断言（调试定位）",
        description="对指定项目的一个用例单次执行，打印完整请求/响应/断言/判定。",
    )
    p_db.add_argument("project", help="项目名（projects/<project>）或数据目录路径")
    p_db.add_argument("case_name", help="目标用例名（按 name 匹配）")
    p_db.add_argument("--stub", action="store_true",
                      help="用桩传输 hermetic 执行（不连真实服务）")
    p_db.set_defaults(func=_run_debug)

    p_oa = subparsers.add_parser(
        "openapi",
        help="把 OpenAPI 3.x paths 导入为最小可用用例（写入项目 data/）",
        description="从 OpenAPI 规范解析 paths → 每条 path×method 一个用例（断言 200），"
                    "追加写入项目 data/。只做最小可用，不生成鉴权/schema 语义。",
    )
    p_oa.add_argument("project", help="项目名（projects/<project>）或数据目录路径")
    p_oa.add_argument("file", help="OpenAPI 3.x YAML 文件路径")
    p_oa.add_argument("--name", default=None,
                      help="生成的用例文件主干名（默认取源文件名）")
    p_oa.set_defaults(func=_run_openapi)


def run(args: argparse.Namespace) -> int:
    """兼容两种子命令的统一执行入口（其实由 args.func 分发，此处兜底）。"""
    return int(args.func(args) or 0)
