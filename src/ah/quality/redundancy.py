#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/15 15:20
# @Filename : redundancy.py
# @Author   : Alan_Hsu
"""用例等价重复识别 + 低价值僵尸用例筛排（后周期支柱 B：低价值用例精洗）。

对应文章痛点：接口自动化重复并非文本一致，更多是**业务语义等价、逻辑覆盖重叠、
场景无差异**的功能重复，文本比对难以发现。

本模块提供三层能力：

1. **确定性等价指纹**（Hermetic，P1）：对每条用例提取
   ``method + 规范化path + 归一化请求参数结构 + 断言键集合`` 生成稳定指纹，
   同指纹聚类 → 直接给出「文本不同但覆盖同一接口同一场景」的重复候选。
2. **低价值/僵尸用例筛排**（P2）：关联执行历史（连续成功无缺陷 + 覆盖接口已废弃）
   → 输出低价值用例清单与下线建议。
3. **语义等价识别**（P2，LLM 分支）：识别「参数不同但等价」「步骤顺序不同但结果等价」
   的深层冗余；LLM 关闭时回退确定性指纹，保证 Hermetic。

一致性铁律：指纹计算纯本地、确定性，绝不触碰网络。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence


def _norm_path(path: str) -> str:
    """规范化路径：模板参数替换为占位符，消除 {id} vs 123 的差异。"""
    import re
    p = str(path or "")
    p = re.sub(r"\{[^}]+\}", "{param}", p)
    p = re.sub(r"/\d+", "/{num}", p)
    return p.rstrip("/") or "/"


def _norm_params(req: dict[str, Any]) -> tuple[Any, Any]:
    """归一化请求参数：仅保留存在性（键集合），不比较具体值。

    返回 ``(params_keys, body_keys)``，把「不同输入值但覆盖同一参数空间」视为等价。
    """
    params = req.get("params") or {}
    body = req.get("json") or req.get("data") or {}
    if isinstance(params, dict):
        params_keys = tuple(sorted(str(k) for k in params))
    else:
        params_keys = ()
    if isinstance(body, dict):
        body_keys = tuple(sorted(str(k) for k in body))
    else:
        body_keys = ()
    return params_keys, body_keys


def _assert_keys(assert_: dict[str, Any]) -> tuple[str, ...]:
    """提取断言键集合（用于等价判定：断言类型一致视为覆盖同业务规则）。"""
    return tuple(sorted(str(k) for k in (assert_ or {})))


def case_fingerprint(case: Any) -> str:
    """计算单条用例的等价指纹（method + 规范化path + 参数存在性 + 断言键集合）。

    同指纹 ⇒ 覆盖同一接口同一场景，为「文本不同但业务等价」的重复候选。
    使用 SHA256 摘要保证稳定、可排序、无碰撞歧义。
    """
    req = getattr(case, "request", {}) or {}
    method = str((req or {}).get("method", "GET")).upper()
    path = _norm_path(req.get("path", ""))
    params_keys, body_keys = _norm_params(req)
    akeys = _assert_keys(getattr(case, "assert_", None) or {})
    payload = json.dumps(
        {"m": method, "p": path, "params": list(params_keys), "body": list(body_keys),
         "a": list(akeys)},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class DuplicateGroup:
    """一组等价重复用例。"""

    fingerprint: str
    members: list[Any] = field(default_factory=list)
    primary: Optional[Any] = None      # 建议保留的"最优版本"（优先级最高/质量分最高）
    is_exact_duplicate: bool = False   # 文本是否完全一致（含 request+assert 全等）

    @property
    def redundant(self) -> list[Any]:
        """除主版本外其余成员均为冗余候选。"""
        if not self.primary:
            return []
        return [m for m in self.members if m is not self.primary]


def _exact_duplicate(a: Any, b: Any) -> bool:
    def _sig(x: Any) -> str:
        req = getattr(x, "request", {}) or {}
        return json.dumps(
            {"name": str(getattr(x, "name", "")), "req": req,
             "assert": getattr(x, "assert_", {}) or {}},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
    return _sig(a) == _sig(b)


def _primary_score(case: Any, scorer: Optional[Callable[[Any], float]] = None) -> float:
    """主版本选择评分：优先质量分，其次优先级，默认 0。"""
    if scorer is not None:
        try:
            return float(scorer(case))
        except (TypeError, ValueError):
            pass
    priority = str(getattr(case, "priority", "") or "P2")
    rank = {"P0": 3.0, "P1": 2.0, "P2": 1.0}.get(priority.upper(), 1.0)
    return rank


def group_duplicates(
    cases: Sequence[Any],
    *,
    scorer: Optional[Callable[[Any], float]] = None,
) -> list[DuplicateGroup]:
    """对用例库按等价指纹聚类重复候选。

    - 完全一致的文本重复：``is_exact_duplicate=True``。
    - 同指纹（参数值不同但参数空间+断言集合相同）：业务等价重复。
    - 每组选一个主版本：优先质量分（scorer 注入），其次优先级。
    返回仅含「成员 ≥ 2」的重复组；无重复返回空列表。
    """
    buckets: dict[str, list[Any]] = {}
    for c in cases:
        buckets.setdefault(case_fingerprint(c), []).append(c)
    groups: list[DuplicateGroup] = []
    for fp, members in buckets.items():
        if len(members) < 2:
            continue
        primary = max(members, key=lambda c: _primary_score(c, scorer))
        is_exact = any(
            _exact_duplicate(members[i], members[j])
            for i in range(len(members)) for j in range(i + 1, len(members))
        )
        groups.append(
            DuplicateGroup(
                fingerprint=fp,
                members=members,
                primary=primary,
                is_exact_duplicate=is_exact,
            )
        )
    groups.sort(key=lambda g: -len(g.members))
    return groups


def semantic_similar_groups(
    cases: Sequence[Any],
    *,
    embed: Optional[Callable[[str], list[float]]] = None,
    similarity_fn: Optional[Callable[[list[float], list[float]], float]] = None,
    threshold: float = 0.85,
) -> list[DuplicateGroup]:
    """**语义等价识别**（P2，LLM/Embedding 分支）。

    识别「参数不同但覆盖同一场景」「步骤顺序不同但结果等价」的深层冗余：

    - ``embed(text) -> vector``：业务语义向量化（可注入 Embedding 模型）。
    - ``similarity_fn(a, b) -> float``：余弦相似度等。
    - 相似度 ≥ ``threshold`` 的两条用例并入同一组；确定性指纹组视为天然相似。

    **Hermetic 回退**：未注入 ``embed``/``similarity_fn`` 时仅返回确定性指纹重复组
    （等价于 :func:`group_duplicates`），绝不触碰网络。
    """
    if embed is None or similarity_fn is None:
        return group_duplicates(cases)

    names = [str(getattr(c, "name", "")) for c in cases]
    vecs = [embed(f"{name} {n}") for name, n in zip(names, cases)]
    used = [False] * len(cases)
    groups: list[DuplicateGroup] = []
    for i, c in enumerate(cases):
        if used[i]:
            continue
        members = [c]
        for j in range(i + 1, len(cases)):
            if used[j]:
                continue
            if similarity_fn(vecs[i], vecs[j]) >= threshold:
                members.append(cases[j])
                used[j] = True
        used[i] = True
        if len(members) >= 2:
            primary = max(members, key=lambda x: _primary_score(x))
            groups.append(
                DuplicateGroup(
                    fingerprint="semantic",
                    members=members,
                    primary=primary,
                    is_exact_duplicate=False,
                )
            )
    return groups


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（供语义识别默认相似度函数复用）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ───────────────────────── 低价值 / 僵尸用例筛排 ─────────────────────────


@dataclass
class LowValueCase:
    """一条低价值/僵尸用例的判定。"""

    case: Any
    reason: str
    evidence: str = ""


@dataclass
class LowValueReport:
    """低价值用例筛排聚合结果。"""

    items: list[LowValueCase] = field(default_factory=list)

    def to_dict(self) -> list[dict[str, Any]]:
        return [
            {
                "name": getattr(i.case, "name", ""),
                "reason": i.reason,
                "evidence": i.evidence,
            }
            for i in self.items
        ]


def _retired_interface(case: Any, retired_paths: set[tuple[str, str]]) -> bool:
    req = getattr(case, "request", {}) or {}
    method = str((req or {}).get("method", "GET")).upper()
    path = str(req.get("path", ""))
    return (method, path) in retired_paths


def screen_low_value(
    cases: Sequence[Any],
    *,
    retired_paths: Optional[set[tuple[str, str]]] = None,
    recent_results: Optional[dict[str, list[str]]] = None,
    no_defect_consecutive: int = 3,
    min_success_count: int = 3,
    dead_fields: Optional[set[str]] = None,
) -> LowValueReport:
    """筛排低价值/僵尸用例（文章规则判定）。

    规则（任一命中 → 低价值）：
    1. ``retired_interface``：覆盖接口已下线/废弃。
    2. ``always_pass_no_defect``：连续 ``no_defect_consecutive`` 次执行成功且未发现
       任何缺陷（``recent_results`` 为该用例的最近结果序列，全 PASS 且无 defect 标记）。
    3. ``no_effective_assert``：无有效断言（弱/空断言，无法拦截缺陷）。

    ``recent_results`` 结构：``{case_name: [result_str, ...]}``（如 PASS/FAIL/DEFECT）。
    未提供的维度不参与判定，保证 Hermetic 可全缺省。
    """
    report = LowValueReport()
    for c in cases:
        name = str(getattr(c, "name", "") or "")
        if retired_paths and _retired_interface(c, retired_paths):
            report.items.append(LowValueCase(c, "retired_interface", f"接口已下线 {name}"))
            continue
        if recent_results and name in recent_results:
            seq = recent_results[name][-max(no_defect_consecutive, 1):]
            all_pass = all(str(s).upper() in {"PASS"} for s in seq)
            no_defect = "DEFECT" not in {str(s).upper() for s in seq}
            if all_pass and no_defect and len(seq) >= min_success_count:
                report.items.append(
                    LowValueCase(
                        c, "always_pass_no_defect",
                        f"连续 {len(seq)} 次成功且无缺陷",
                    )
                )
                continue
        assert_ = getattr(c, "assert_", None) or {}
        has_body = any(k in assert_ for k in ("jsonpath", "schema", "equals", "contains", "body"))
        if not assert_ or not has_body:
            report.items.append(
                LowValueCase(c, "no_effective_assert", "无有效断言（弱/空）")
            )
    return report
