#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/15 15:42
# @Filename : gap.py
# @Author   : Alan_Hsu
"""接口契约覆盖差集（后周期支柱 C：接口契约覆盖的中长期确定性落地）。

把 OpenAPI 契约中的**全量接口/参数/字段**与用例库做差集计算，输出：

1. **未覆盖接口**：契约定义了、但用例库没有任何 case 覆盖的 ``method path``。
2. **未覆盖参数**：契约操作声明的参数，用例库中对应接口未覆盖的参数键。
3. **未覆盖断言字段**：契约响应/请求字段，用例库中未被断言触及的字段（可选）。

覆盖判定规则（Hermetic、确定性）：

- 接口覆盖：用例 ``method+path`` 与契约操作匹配（path 模板参数视为可匹配通配）。
- 参数覆盖：用例 request 的 ``params/json/data`` 键集合 ⊆ 契约声明参数。
- 输出按操作优先级（GET/核心方法靠前）与数量排序，指导补充优先级。

仅依赖 :mod:`ah.scenario.catalog`（离线操作目录）与用例库，零网络。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ah.scenario.catalog import load_catalog


def _method_priority(method: str) -> int:
    """操作优先级排序键：核心读/写方法靠前（GET 0, POST 1, PUT 2, PATCH 3, DELETE 4）。"""
    order = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4}
    return order.get(str(method).upper(), 5)


def _norm_catalog_path(path: str) -> str:
    """把具体路径归一化为模板形态用于匹配。

    归一化（覆盖框架自身变量语法与 OpenAPI 模板参数与具体值）：
    - ``{name}`` / ``${name}`` / ``${name|default}`` → ``{param}``。
    - 具体数字段 ``/123`` → ``{param}``。
    使 ``/users/123``、``/users/${user_id}``、``/users/{id}`` 三者视为等价
    （对契约覆盖而言，具体值仍落在模板参数位）。
    """
    p = re.sub(r"\$\{[^}]*\}", "{param}", path)
    p = re.sub(r"\{\w+\}", "{param}", p)
    return re.sub(r"/\d+", "/{param}", p)


def _template_match(case_path: str, catalog_path: str) -> bool:
    """路径模板匹配：模板参数（{x} / ${x}）通配；数字段通配；否则需完全一致。"""
    p = str(case_path or "").rstrip("/")
    c = str(catalog_path or "").rstrip("/")
    return _norm_catalog_path(p) == _norm_catalog_path(c)


@dataclass
class CatalogOperation:
    """从契约目录提取的一条操作。"""

    method: str
    path: str
    params: set[str] = field(default_factory=set)


def load_operations(catalog: dict) -> list[CatalogOperation]:
    """从 :func:`ah.scenario.catalog.normalize_catalog` 的目录提取操作集合。"""
    ops: list[CatalogOperation] = []
    for op in catalog.get("operations") or []:
        method = str(op.get("method", "GET")).upper()
        path = str(op.get("path", ""))
        params: set[str] = set()
        # 目录操作 parameters 为 [{"in": ..., "name": ...}, ...]。
        for it in op.get("parameters") or []:
            if isinstance(it, dict) and it.get("name"):
                params.add(str(it["name"]))
        # request_schema 的必需字段也算"声明参数"（覆盖请求体字段）。
        rq = op.get("request_schema")
        if isinstance(rq, dict):
            req_props = rq.get("properties") or {}
            if isinstance(req_props, dict):
                params.update(str(k) for k in req_props)
        ops.append(CatalogOperation(method=method, path=path, params=params))
    ops.sort(key=lambda o: _method_priority(o.method))
    return ops


def _case_request_keys(case: Any) -> set[str]:
    """用例覆盖的请求参数键集合（params/json/data 的键）。"""
    req = getattr(case, "request", {}) or {}
    keys: set[str] = set()
    for sec in ("params", "json", "data"):
        val = req.get(sec)
        if isinstance(val, dict):
            keys.update(str(k) for k in val)
    return keys


@dataclass
class CoverageGap:
    """一个覆盖缺口（接口或参数级）。"""

    method: str
    path: str
    kind: str                  # "interface" | "param" | "assert_field"
    detail: str = ""


@dataclass
class CoverageReport:
    """契约覆盖差集聚合结果。"""

    total_operations: int = 0
    covered_interfaces: int = 0
    gaps: list[CoverageGap] = field(default_factory=list)

    @property
    def coverage_ratio(self) -> float:
        """接口覆盖率（covered/total）。"""
        if self.total_operations == 0:
            return 0.0
        return round(100.0 * self.covered_interfaces / self.total_operations, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_operations": self.total_operations,
            "covered_interfaces": self.covered_interfaces,
            "coverage_ratio": self.coverage_ratio,
            "gaps": [
                {"method": g.method, "path": g.path, "kind": g.kind, "detail": g.detail}
                for g in self.gaps
            ],
        }


def compute_gaps(
    catalog: dict,
    cases: Sequence[Any],
    *,
    include_params: bool = True,
) -> CoverageReport:
    """计算契约 vs 用例库的覆盖差集。

    - ``interface`` 缺口：契约有、用例库无任何覆盖。
    - ``param`` 缺口：契约声明参数、用例库对应接口未覆盖（``include_params=True``）。
    返回 :class:`CoverageReport`。
    """
    ops = load_operations(catalog)
    report = CoverageReport(total_operations=len(ops))
    covered_paths: set[tuple[str, str]] = set()
    covered_params: dict[tuple[str, str], set[str]] = {}

    # 建立用例覆盖索引：method+path → 合并的参数键集合。
    case_index: dict[tuple[str, str], set[str]] = {}
    for case in cases:
        req = getattr(case, "request", {}) or {}
        method = str((req or {}).get("method", "GET")).upper()
        cpath = str(req.get("path", ""))
        key = (method, cpath)
        case_index.setdefault(key, set()).update(_case_request_keys(case))

    for op in ops:
        key = (op.method, op.path)
        matched = False
        for (cmethod, cpath), ckeys in case_index.items():
            if cmethod == op.method and _template_match(cpath, op.path):
                matched = True
                covered_paths.add(key)
                covered_params.setdefault(key, set()).update(ckeys)
                break
        if not matched:
            report.gaps.append(
                CoverageGap(op.method, op.path, "interface", "契约声明但用例库未覆盖")
            )
            continue
        report.covered_interfaces += 1
        if include_params and op.params:
            missing = op.params - covered_params.get(key, set())
            if missing:
                report.gaps.append(
                    CoverageGap(
                        op.method,
                        op.path,
                        "param",
                        f"未覆盖参数: {', '.join(sorted(missing))}",
                    )
                )
    return report


def load_catalog_operations(
    catalog_path: str | Path, service: str = "default"
) -> list[CatalogOperation]:
    """从 OpenAPI 文件加载并提取操作（薄封装，供 CLI 直接使用）。"""
    catalog = load_catalog(catalog_path, service=service)
    return load_operations(catalog)
