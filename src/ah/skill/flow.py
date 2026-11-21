#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/17 12:00
# @Filename : flow.py
# @Author   : Alan_Hsu
"""业务流程（flow）加载：把 ``flow:`` 声明式组装文件解析/校验为可执行条目。

flow 是「skill 互调 + 内联步骤」的顶层编排：一个业务流 = 一串条目，每个条目要么是
``call: <skill>``（带 ``with``/``assign``），要么是内联 ``name``+``request``+``extract``。

约定：flow 文件放项目 ``flows/`` 目录（与 ``data/`` 平级），文件名 ``<name>.yaml``，
顶层键为 ``flow``。**不放 ``data/``**——因为 :func:`ah.harness.loader.discover_case_files`
会收集 ``data/`` 下所有 ``*.yaml`` 并交给 ``load_cases``，而 flow 文件没有
``request/cases/scenario`` 键会被判为 ``unrecognized case file format`` 误伤。

本模块纯静态、零网络，``LLMOFF=1`` 安全；执行逻辑见 :mod:`ah.skill.executor`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from ah.skill.model import SkillRegistry, build_registry
from ah.utils.logging import get_logger

log = get_logger("skill.flow")

#: flow 文件默认存放目录名（与项目 data/ 平级）。
FLOWS_DIR = "flows"


class FlowLoadError(Exception):
    """flow 文件缺失 / schema 不合法时抛出。"""


def _default_flows_dir(project_dir: str | Path) -> Path:
    """返回项目 ``flows/`` 目录（不存在则建成）。"""
    d = Path(project_dir) / FLOWS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def flow_path(project_dir: str | Path, name: str) -> Path:
    """定位 ``<project>/flows/<name>.yaml``。"""
    p = _default_flows_dir(project_dir) / f"{name}.yaml"
    if not p.is_file():
        raise FlowLoadError(f"找不到 flow {name!r}：期望 {p}")
    return p


def load_flow(path: str | Path) -> Tuple[str, List[Dict[str, Any]]]:
    """加载 flow 文件，返回 ``(name, entries)``。

    校验顶层 ``flow`` 键：缺失 / 非列表即报 :class:`FlowLoadError`。条目内的
    ``call/with/assign`` 与内联字段合法性留给 :mod:`ah.skill.model` 与 executor
    在展开时校验（引用缺失会在执行期转成该步骤失败，不抛穿）。
    """
    p = Path(path)
    if not p.is_file():
        raise FlowLoadError(f"flow 文件不存在: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or "flow" not in raw:
        raise FlowLoadError(f"{p}: 缺少顶层 flow 键（应为 flow: 列表）")
    entries = raw["flow"]
    if not isinstance(entries, list):
        raise FlowLoadError(f"{p}: flow 键应为步骤条目列表")
    name = str(raw.get("name") or p.stem)
    return name, list(entries)

def flow_meta(path: str | Path) -> Dict[str, Any]:
    """读取 flow 顶层编排元数据（``dataset``/``resources``），不校验、不展开。

    与 :func:`load_flow` 分离：load_flow 返回可执行条目（``flow:`` 列表），本函数
    返回驱动多轮数据驱动（§7.3 ``dataset``）与资源清理（§10.2 ``resources``）所需的
    顶层字段。executor 在执行后消费；校验由 :func:`ah.skill.validate.validate_flow_extras`。
    """
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    meta: Dict[str, Any] = {}
    if isinstance(raw, dict):
        if "dataset" in raw:
            meta["dataset"] = raw["dataset"]
        if "resources" in raw:
            meta["resources"] = raw["resources"]
    return meta


def build_project_flow(
    project_dir: str | Path, name: str
) -> Tuple[str, SkillRegistry, List[Dict[str, Any]]]:
    """一键装配：解析项目 + 加载 flow + 构建 skill 注册表。

    返回 ``(flow_name, registry, entries)`` 供 :func:`ah.skill.executor.execute_flow`
    直接消费。skill 目录用 :func:`ah.skill.model.build_registry`（扫描 ``skills/`` 或
    ``data/skills/``）。
    """
    flow_name, entries = load_flow(flow_path(project_dir, name))
    registry: SkillRegistry = build_registry(Path(project_dir))
    return flow_name, registry, entries
