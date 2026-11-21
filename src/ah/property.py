#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2026-10-08 21:00
# @Filename: property.py
# @Author  : Alan_Hsu
"""Hypothesis 属性测试（三方接入 · 功能 17）。

对指定用例的 ``request.json`` / ``params`` 做参数空间探索，复用现有
:func:`ah.metrics.assertions.assert_response` 判定每个样本是否满足断言。
Hypothesis 为 *optional dependency*：未安装时 ``ah property`` 报清晰错误，
已安装则零配置使用（绝不静默降级为跳过）。

命令行入口：
    ah property <project> <case-name> [--examples N] [--seed S] [--json]

hermetic 语义：本模块自身不依赖 LLM / 网络；HTTP 请求经调用方注入的
``HTTPClient`` 发出，测试用 ``httpx.MockTransport`` 注入即零网络。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from ah.harness.http import HTTPClient
from ah.harness.loader import Case, discover_case_files, load_cases
from ah.metrics.assertions import assert_response
from ah.utils.context import Context
from ah.utils.logging import get_logger

log = get_logger("property")

#: 默认每个参数组合的探索样本数。
DEFAULT_EXAMPLES = 100
#: 服务端响应安全上限，超出认为是「响应不可信」而非属性违反。
_MAX_RESPONSE_BYTES = 1 << 20


class HypothesisUnavailable(RuntimeError):
    """hypothesis 未安装时抛出；run() 捕获后给出安装指引。"""


def require_hypothesis() -> Any:
    """按需导入 hypothesis，未安装时报 :class:`HypothesisUnavailable`。

    测试可用 monkeypatch 替换本函数的导入路径来模拟「未安装」分支。
    """
    try:
        import hypothesis
        from hypothesis import given, settings
        from hypothesis import strategies as st
    except ImportError as exc:  # pragma: no cover - 仅未安装时命中
        raise HypothesisUnavailable(
            "Hypothesis 未安装。请先安装可选依赖：\n"
            "    uv pip install hypothesis\n"
            "（或 uv sync --extra all / --extra <含 hypothesis 的 extra>）\n"
            "装好后重试 `ah property`；不装则本命令不可用。"
        ) from exc
    # 返回一个小命名空间，避免在模块顶层依赖 hypothesis。
    return ArgNamespace(given=given, settings=settings, strategies=st, hypothesis=hypothesis)


class ArgNamespace:
    """轻量命名空间：让 require_hypothesis() 返回的符号可被点号访问。"""

    __slots__ = ("given", "settings", "strategies", "hypothesis", "seed")

    def __init__(
        self,
        *,
        given: Any,
        settings: Any,
        strategies: Any,
        hypothesis: Any,
    ) -> None:
        self.given = given
        self.settings = settings
        self.strategies = strategies
        self.hypothesis = hypothesis
        self.seed = hypothesis.seed




def _leaf_strategy(st: Any, value: Any) -> Any:
    """依据现有标量值推断 Hypothesis 策略（探索其邻域而非仅原值）。

    字符串/数字/布尔各自用带边界的通用策略；None 用 none()。若无法推断
    （如已模板化 ``${var}`` 或结构过特殊），退回 ``just(value)`` 保底，
    保证任何样本仍可构造出合法请求。
    """
    if value is None:
        return st.none()
    if isinstance(value, bool):
        return st.booleans()
    if isinstance(value, int):
        return st.integers(min_value=-1000000, max_value=1000000)
    if isinstance(value, float):
        return st.floats(allow_nan=False, allow_infinity=False)
    if isinstance(value, str):
        # 模板占位（${...}）不宜被随机改写，保留原样即可；其余字符串给宽泛域。
        if "${" in value and "}" in value and "${".count("${") == value.count("}"):
            return st.just(value)
        return st.one_of(st.text(min_size=0, max_size=64), st.just(value))
    return st.just(value)


def _list_element_strategy(st: Any, items: Any) -> Any:
    """为定长 list 的每个位置构建元素策略：并集各 index 候选的邻域策略。"""
    cand = [
        _leaf_strategy(st, item) if not isinstance(item, (dict, list)) else st.just(item)
        for item in items
    ]
    if not cand:
        return st.nothing()
    return st.one_of(cand)


def _mapping_strategy(st: Any, node: Any) -> Any:
    """为 json/params 的 :class:`dict` 构建逐键策略。

    标量键用 :func:`_leaf_strategy`；嵌套 dict 递归；list 保留长度并对
    每项生成候选（用 just 保底，避免任意长度引发的结构与断言错位）。
    """
    assert isinstance(node, dict)
    strategies: dict[str, Any] = {}
    for key, value in node.items():
        if isinstance(value, dict):
            strategies[key] = _mapping_strategy(st, value)
        elif isinstance(value, list):
            strategies[key] = st.lists(
                _list_element_strategy(st, value),
                min_size=len(value),
                max_size=len(value),
            )
        else:
            strategies[key] = _leaf_strategy(st, value)
    return st.fixed_dictionaries(strategies)


def build_payload_strategies(request: dict[str, Any]) -> tuple[Any, Any]:
    """为用例 request 构建 ``(json_strat, params_strat)`` 两个 Hypothesis 策略。

    只探索显式给出的 ``json`` / ``params`` 键；无参数空间时用
    ``just(None)`` 占位（表示「该部分不变化」）。
    """
    st = require_hypothesis().strategies
    jsn = request.get("json")
    params = request.get("params")

    json_strat: Any = st.just(None)
    if isinstance(jsn, dict) and jsn:
        json_strat = _mapping_strategy(st, jsn)
    elif jsn is not None and not isinstance(jsn, dict):
        json_strat = _leaf_strategy(st, jsn)

    params_strat: Any = st.just(None)
    if isinstance(params, dict) and params:
        params_strat = _mapping_strategy(st, params)
    elif params is not None and not isinstance(params, dict):
        params_strat = _leaf_strategy(st, params)

    return json_strat, params_strat


@dataclass
class PropertySample:
    """一次参数探索样本的结果。"""

    params: Any = None
    json: Any = None
    passed: bool = False
    status: Optional[int] = None
    error: Optional[str] = None
    duration_ms: float = 0.0


@dataclass
class PropertyReport:
    """属性测试汇总报告。"""

    case_name: str
    examples_requested: int
    examples_run: int = 0
    passed: int = 0
    failed: int = 0
    samples: list[PropertySample] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def success_rate(self) -> float:
        if self.examples_run == 0:
            return 0.0
        return round(self.passed / self.examples_run, 4)

    @property
    def all_passed(self) -> bool:
        return self.examples_run > 0 and self.failed == 0


def _execute_sample(
    case: Case,
    client: HTTPClient,
    params: Any,
    jsn: Any,
) -> PropertySample:
    """用给定的 params/json 构造请求，发出并复用 assert_response 判定。"""
    sample = PropertySample(params=params, json=jsn)
    req = case.request
    request = dict(req)
    # 探索参数覆盖原始 json/params（未给 None 才覆盖，保持请求其余部分原样）。
    if params is not None:
        request["params"] = params
    if jsn is not None:
        request["json"] = jsn
    probe = Case(
        name=case.name,
        request=request,
        extract=case.extract,
        assert_=case.assert_,
        auth=case.auth,
        on_failure="continue",
        source_path=case.source_path,
        env=case.env,
        priority=case.priority,
        suite=case.suite,
        tags=case.tags,
    )
    start = time.perf_counter()
    try:
        resp = client.request(
            str(probe.request.get("method", "GET")).upper(),
            str(probe.request.get("path", "")),
            params=probe.request.get("params"),
            json=probe.request.get("json"),
            data=probe.request.get("data"),
            headers=probe.request.get("headers"),
            auth=probe.auth,
        )
        sample.status = resp.status_code
        mut_ctx = Context()
        assert_response(probe.assert_, resp, mut_ctx)
        sample.passed = True
    except Exception as exc:  # 断言失败/网络异常均为「样本未通过」，不阻断探索
        sample.error = str(exc)[:800]
        sample.passed = False
    finally:
        sample.duration_ms = (time.perf_counter() - start) * 1000.0
    return sample


def explore_properties(
    case: Case,
    *,
    examples: int = DEFAULT_EXAMPLES,
    client: Optional[HTTPClient] = None,
    seed: Optional[int] = None,
) -> PropertyReport:
    """对 ``case`` 做 ``examples`` 次参数空间探索，返回汇总报告。

    全部样本都会执行（断言失败被记录而不中断 Hypothesis 探索），因此返回
    的 ``all_passed`` 只有在 0 失败时才为真。``client`` 缺省用空 base 构建，
    供纯聚合/单元测试在不触碰网络时调用（真实调用方注入带真实传输的 client）。
    """
    ns = require_hypothesis()
    effective_client = client or HTTPClient("")
    report = PropertyReport(case_name=case.name, examples_requested=examples)

    json_strat, params_strat = build_payload_strategies(case.request)

    # 固定种子可复现；不固定则 Hypothesis 默认随机。seed 走 @seed 装饰器，
    # @settings 不支持 seed 参数。
    settings_kw: dict[str, Any] = {
        "max_examples": max(1, examples),
        "deadline": None,
        # 关闭耗时/慢数据健康检查，允许小样本 + 慢 mock 端跑满 N 次。
        "suppress_health_check": list(ns.hypothesis.HealthCheck),
    }

    @ns.given(params=params_strat, jsn=json_strat)
    @ns.settings(**settings_kw)
    def _probe(params: Any, jsn: Any) -> None:
        report.examples_run += 1
        report.samples.append(_execute_sample(case, effective_client, params, jsn))

    probe = _probe
    if seed is not None:
        probe = ns.seed(int(seed))(probe)
    start = time.perf_counter()
    probe()
    report.duration_ms = (time.perf_counter() - start) * 1000.0
    report.passed = sum(1 for s in report.samples if s.passed)
    report.failed = report.examples_run - report.passed
    return report



# ---------------------------------------------------------------------------
# CLI 集成（register / run）
# ---------------------------------------------------------------------------

def _resolve_project_data(project: str) -> Path:
    """把 ``<project>`` 解析为数据目录：``projects/<project>/data`` 或直接路径。"""
    p = Path(project)
    if p.is_dir():
        return p
    root = Path.cwd()
    candidate = root / "projects" / project / "data"
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError(
        f"找不到项目 {project!r} 的数据目录（试过 {candidate} 与 {p}）"
    )


def _find_case(project: str, case_name: str) -> Case:
    """在项目数据目录中按名字查找用例（跨文件、含场景步骤扁平化）。"""
    data_dir = _resolve_project_data(project)
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


def _print_summary(report: PropertyReport, examples: int) -> None:
    print(f"[property] 用例: {report.case_name}")
    print(f"[property] 请求样本: {report.examples_requested}，实际执行: {report.examples_run}")
    print(f"[property] 通过: {report.passed}  失败: {report.failed}  "
          f"成功率: {report.success_rate:.2%}  耗时: {report.duration_ms:.1f}ms")


def register(subparsers: argparse._SubParsersAction) -> None:
    """把 ``property`` 子命令挂到 argparse subparsers。"""
    p = subparsers.add_parser(
        "property",
        help="用 Hypothesis 对用例 request.json/params 做参数空间探索",
        description="对指定用例的参数空间运行 Hypothesis 属性测试，复用现有断言判定。",
    )
    p.add_argument("project", help="项目名（projects/<project>）或数据目录路径")
    p.add_argument("case_name", help="目标用例名（按 name 匹配）")
    p.add_argument("--examples", type=int, default=DEFAULT_EXAMPLES,
                   help=f"探索样本数（默认 {DEFAULT_EXAMPLES}）")
    p.add_argument("--seed", type=int, default=None,
                   help="Hypothesis 随机种子（确定性复现，缺省随机）")
    p.add_argument("--json", dest="write_json", action="store_true",
                   help="把报告另写一份 property_report.json")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """执行属性测试；0=全部样本通过，1=存在失败，2=环境/参数错误。"""
    try:
        require_hypothesis()
    except HypothesisUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.examples < 1:
        print(f"[property] --examples 必须 >=1（收到 {args.examples}）", file=sys.stderr)
        return 2

    try:
        case = _find_case(args.project, args.case_name)
    except FileNotFoundError as exc:
        print(f"[property] {exc}", file=sys.stderr)
        return 2

    client = HTTPClient(_base_url_for(case) or "")  # 真实运行：从用例/配置展开
    try:
        report = explore_properties(
            case, examples=args.examples, client=client, seed=args.seed
        )
    except HypothesisUnavailable as exc:  # pragma: no cover - 与上面一致但容错
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        try:
            client.close()
        except Exception:
            pass

    _print_summary(report, args.examples)
    if getattr(args, "write_json", False) or report.failed > 0:
        out = Path.cwd() / "property_report.json"
        out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        log.info("property report written: %s", out)

    return 0 if report.all_passed else 1


def _base_url_for(case: Case) -> str:
    """从用例 request.host 或设置取 base_url；空则交给 HTTPClient 默认。"""
    from ah.core.config import settings
    host = case.host
    if host:
        return host
    return settings.base_url
