#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/17 10:00
# @Filename : model.py
# @Author   : Alan_Hsu
"""Skill 数据模型：可复用步骤单元 + 声明式互调（call/with/assign）。

一个 Skill 是「一段可复用的步骤包」：有名字、描述、所属服务、声明入参/出参、默认数据、
前置条件与副作用标注。skill 步骤里可用 ``call`` 声明式调用另一个 skill，``with`` 传入
参数覆盖其 defaults，``assign`` 把被调 skill 产出的变量提升到调用方作用域。

本模块只承载纯数据模型与 YAML 反序列化，不执行任何请求（``LLMOFF=1`` 安全）。
运行时「展开扁平步骤链」见 :mod:`ah.skill.registry`（阶段 B 使用）。

设计原则（对齐方案 docs/designs/api-skill-composition.md）：
- 兼容旧用例：Skill 概念是可选增强，旧 ``scenario.yaml`` 不依赖任何新字段。
- 显式优于隐式：``call``/``with``/``assign`` 是声明式互调契约，不是隐式 ``${var}`` 碰运气。
- 静态可校验：``inputs``/``outputs``/``defaults`` 供 :mod:`ah.skill.validate` 做引用完整性
  与变量名校验，坏引用在加载期就报出明确错误，而不是运行期才失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

#: 副作用取值（read=读操作无副作用；write=写操作，需显式授权）。
SIDE_READ = "read"
SIDE_WRITE = "write"
SIDE_EFFECTS: set[str] = {SIDE_READ, SIDE_WRITE}

#: 嵌套互调展开的最大深度；超过即视为循环或过深（防御环引用）。
MAX_CALL_DEPTH = 8


@dataclass
class CallStep:
    """一次声明式互调：调用另一 skill 并传参/接收产出。

    对应 YAML：

    .. code-block:: yaml

        - call: create-order
          with: { item_id: "${order_item_id}" }
          assign: { order_id: "${order_id}" }
          assert: { custom: ... }   # 对被调结果的额外校验（可选，阶段 B 生效）
    """

    target: str  # 被调 skill 名
    with_vars: Dict[str, Any] = field(default_factory=dict)  # 覆盖被调 skill 的 defaults
    assign: Dict[str, str] = field(default_factory=dict)  # 被调 outputs -> 调用方作用域变量
    assert_extra: Dict[str, Any] = field(default_factory=dict)  # 对被调结果的额外校验

    @property
    def is_call(self) -> bool:
        return True


@dataclass
class Step:
    """skill 的一个执行节点：要么是内联 HTTP 步骤，要么是一次 skill 互调。

    内联步骤直接承载与现有 :class:`~ah.harness.loader.Case` 一致的字段（request/
    extract/assert/variables…），互调步骤承载 :class:`CallStep`。两者二选一。

    YAML 判定：出现 ``call:`` 键即为互调步骤，否则为内联请求步骤。
    """

    name: str
    request: Optional[Dict[str, Any]] = None  # 内联请求（互调步骤为 None）
    extract: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    assert_: Dict[str, Any] = field(default_factory=dict)
    call: Optional[CallStep] = None  # 互调（内联步骤为 None）

    @property
    def is_call(self) -> bool:
        return self.call is not None


@dataclass
class Skill:
    """一个可复用步骤包（skill 定义）。

    字段与 YAML 对齐：

    .. code-block:: yaml

        name: create-order
        description: 创建订单并返回订单号
        works_on: order-service
        inputs: [item_id]
        outputs: [order_id]
        defaults: { item_id: 7 }
        side_effects: [write]
        steps:
          - name: create
            request: { method: POST, path: /orders, ... }
    """

    name: str
    steps: List[Step] = field(default_factory=list)
    description: str = ""
    version: str = "1.0"  # skill 语义版本（注册表/变更影响分析用，默认 1.0）
    works_on: str = "default"  # 所属服务/域（供 finder 按服务检索）
    inputs: List[str] = field(default_factory=list)  # 声明消费的变量（校验/文档用）
    outputs: List[str] = field(default_factory=list)  # 声明产出的变量（校验/文档用）
    defaults: Dict[str, Any] = field(default_factory=dict)  # 未传参时的默认变量
    requires: List[str] = field(default_factory=list)  # 前置条件名（阶段 C 联动 check 用）
    side_effects: List[str] = field(default_factory=list)  # read / write
    source_path: str = ""  # 定义文件路径（定位与报告用）

    def call_targets(self) -> List[str]:
        """本 skill 直接调用的其他 skill 名（去重保序）。"""
        seen: List[str] = []
        for step in self.steps:
            if step.is_call and step.call is not None and step.call.target not in seen:
                seen.append(step.call.target)
        return seen


@dataclass
class SkillRegistry:
    """已加载 skill 的内存注册表；按名字解析嵌套互调。"""

    items: Dict[str, Skill] = field(default_factory=dict)
    base_dir: str = ""  # skill 目录根（相对引用定位用，可选）

    def get(self, name: str) -> Optional[Skill]:
        return self.items.get(name)

    def names(self) -> List[str]:
        return sorted(self.items.keys())

    def by_works_on(self, service: str) -> List[Skill]:
        return [s for s in self.items.values() if s.works_on == service]

    def references_to(self, name: str) -> List[str]:
        """返回**直接引用**指定 skill 的 skill 名列表（变更影响分析用）。

        改动 ``name`` 时，列表内 skill 声明了 ``call: name``，可能受其产出/行为变化
        影响。只查直接引用；间接（A→B→target）需递归展开才可见。
        """
        return sorted(s.name for s in self.items.values() if name in s.call_targets())


def _parse_step(data: Dict[str, Any]) -> Step:
    """把单步 YAML 映射解析为 :class:`Step`（内联请求或互调之一）。"""
    if "call" in data:
        return Step(
            name=str(data.get("name", f"call-{data['call']}")),
            call=CallStep(
                target=str(data["call"]),
                with_vars=dict(data.get("with") or {}),
                assign=dict(data.get("assign") or {}),
                assert_extra=dict(data.get("assert") or {}),
            ),
        )
    return Step(
        name=str(data.get("name", "step")),
        request=dict(data.get("request") or {}),
        extract=dict(data.get("extract") or {}),
        assert_=dict(data.get("assert") or {}),
    )


def load_skill_yaml(data: Dict[str, Any], *, source: str = "") -> Skill:
    """从 YAML 顶层映射构造 :class:`Skill`（校验由 :mod:`ah.skill.validate` 负责）。"""
    name = str(data.get("name", ""))
    if not name:
        raise ValueError(f"skill {source}: name 必填")
    side = data.get("side_effects") or []
    if isinstance(side, str):
        side = [side]
    return Skill(
        name=name,
        steps=[_parse_step(step) for step in data.get("steps") or []],
        description=str(data.get("description", "") or ""),
        version=str(data.get("version", "1.0") or "1.0"),
        works_on=str(data.get("works_on", "default") or "default"),
        inputs=[str(i) for i in (data.get("inputs") or [])],
        outputs=[str(o) for o in (data.get("outputs") or [])],
        defaults=dict(data.get("defaults") or {}),
        requires=[str(r) for r in (data.get("requires") or [])],
        side_effects=[s for s in side if s in SIDE_EFFECTS],
        source_path=source,
    )


def load_skill_file(path: str | Path) -> Skill:
    """从技能 YAML 文件加载单个 skill。"""
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"skill 文件 {p}: 顶层必须是映射")
    return load_skill_yaml(data, source=str(p))


def discover_skill_files(base_dir: str | Path) -> List[str]:
    """递归发现 ``skills/``（或 ``data/skills/``）目录下的所有 ``.yaml`` skill 文件。

    解析策略：只收集 ``base_dir/skills/`` 与 ``base_dir/data/skills/`` 两个约定位置的
    ``*.yaml``，避免把 ``data/`` 下的用例文件误判为 skill。若目录不存在返回空列表。
    """
    base = Path(base_dir)
    roots = [base / "skills", base / "data" / "skills"]
    out: List[str] = []
    for root in roots:
        if root.is_dir():
            out.extend(str(p) for p in root.rglob("*.yaml") if p.is_file())
    return sorted(set(out))


def build_registry(base_dir: str | Path) -> SkillRegistry:
    """扫描项目目录发现并加载全部 skill，返回 :class:`SkillRegistry`。"""
    registry = SkillRegistry(base_dir=str(Path(base_dir)))
    for path in discover_skill_files(base_dir):
        try:
            skill = load_skill_file(path)
        except ValueError as exc:
            raise ValueError(f"skill 文件 {path} 加载失败: {exc}") from exc
        if skill.name in registry.items:
            raise ValueError(
                f"skill 重名: {skill.name!r} 出现在 {registry.items[skill.name].source_path} "
                f"与 {path}（skill 名必须全局唯一）"
            )
        registry.items[skill.name] = skill
    return registry
