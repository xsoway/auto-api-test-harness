#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/17 10:20
# @Filename : test_skill.py
# @Author   : Alan_Hsu
"""Skill 定义/加载/校验单元测试（纯静态、零网络，LLMOFF 安全）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ah.skill.model import SIDE_WRITE, build_registry, load_skill_file, load_skill_yaml
from ah.skill.validate import (
    CODE_CALL_NO_TARGET,
    CODE_CYCLE,
    CODE_NO_STEPS,
    CODE_UNKNOWN_ASSIGN,
    CODE_UNKNOWN_TARGET,
    CODE_UNKNOWN_WITH,
    validate_skill,
)


def _skill_yaml(**over) -> dict:
    """构造一个最小合法 skill 顶层映射（无 call）。"""
    base = {
        "name": "create-order",
        "description": "创建订单",
        "works_on": "order-service",
        "inputs": ["item_id"],
        "outputs": ["order_id"],
        "defaults": {"item_id": 7},
        "side_effects": ["write"],
        "steps": [
            {
                "name": "create",
                "request": {"method": "POST", "path": "/orders", "json": {"item_id": "${item_id}"}},
                "assert": {"status_code": 201},
                "extract": {"order_id": {"path": "order.id", "extract_type": "jsonpath"}},
            }
        ],
    }
    base.update(over)
    return base


def test_load_skill_yaml_inline() -> None:
    skill = load_skill_yaml(_skill_yaml(), source="skills/create-order.yaml")
    assert skill.name == "create-order"
    assert skill.works_on == "order-service"
    assert skill.inputs == ["item_id"]
    assert skill.side_effects == [SIDE_WRITE]
    assert len(skill.steps) == 1
    step = skill.steps[0]
    assert not step.is_call
    assert step.request["path"] == "/orders"
    assert step.extract == {"order_id": {"path": "order.id", "extract_type": "jsonpath"}}


def test_load_skill_yaml_missing_name_raises() -> None:
    with pytest.raises(ValueError):
        load_skill_yaml({"steps": []}, source="x.yaml")


def test_load_skill_yaml_call_step() -> None:
    skill = load_skill_yaml(
        {
            "name": "checkout",
            "steps": [
                {
                    "call": "create-order",
                    "with": {"item_id": "${order_item_id}"},
                    "assign": {"order_id": "${order_id}"},
                }
            ],
        },
        source="skills/checkout.yaml",
    )
    step = skill.steps[0]
    assert step.is_call
    assert step.call is not None
    assert step.call.target == "create-order"
    assert step.call.with_vars == {"item_id": "${order_item_id}"}
    assert step.call.assign == {"order_id": "${order_id}"}


def test_build_registry_discovers_and_dedups(tmp_path: Path) -> None:
    # 同时验证 skills/ 与 data/skills/ 都被发现，且 data/ 下普通用例不被误收。
    (tmp_path / "skills").mkdir(parents=True)
    (tmp_path / "data" / "skills").mkdir(parents=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "skills" / "a.yaml").write_text("name: a\nsteps: []\n", encoding="utf-8")
    (tmp_path / "data" / "skills" / "b.yaml").write_text("name: b\nsteps: []\n", encoding="utf-8")
    # 普通用例文件（无 name 之外的 skill 形态）不应被当 skill 收集，但这里是合法用例文件：
    (tmp_path / "data" / "order_flow.yaml").write_text(
        "name: order-flow\nscenario: []\n", encoding="utf-8"
    )
    reg = build_registry(tmp_path)
    assert reg.names() == ["a", "b"]
    # data/ 下的用例文件没有被 discovery 收集（不在 skills 约定目录）。
    assert "order-flow" not in reg.names()


def test_build_registry_duplicate_name_raises(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "a.yaml").write_text("name: dup\nsteps: []\n", encoding="utf-8")
    (tmp_path / "skills" / "b.yaml").write_text("name: dup\nsteps: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重名"):
        build_registry(tmp_path)


def _reg(skills: dict[str, dict]) -> object:
    """从 dict 构造临时注册表（用于引用/环校验测试）。"""
    from ah.skill.model import Skill, SkillRegistry, Step

    items: dict[str, Skill] = {}
    for name, data in skills.items():
        steps = []
        for s in data.get("steps", []):
            steps.append(Step(name=str(s.get("name")), call=None, request=s.get("request")))
        items[name] = Skill(
            name=name,
            steps=steps,
            inputs=data.get("inputs", []),
            outputs=data.get("outputs", []),
            defaults=data.get("defaults", {}),
        )
    return SkillRegistry(items=items)


def _call_step_with(target: str, with_vars: dict | None = None, assign: dict | None = None) -> dict:
    from ah.skill.model import CallStep, Step

    step = Step(
        name=f"call-{target}",
        call=CallStep(target=target, with_vars=with_vars or {}, assign=assign or {}),
    )
    return step


def test_validate_unknown_target() -> None:
    registry = _reg({"a": {"steps": []}})
    from ah.skill.model import Skill

    skill = Skill(name="b", steps=[_call_step_with("nope")])
    errors = validate_skill(skill, registry)
    assert any(e["code"] == CODE_UNKNOWN_TARGET for e in errors)


def test_validate_cycle_detected() -> None:
    from ah.skill.model import Skill

    # a -> b -> a 环。
    registry = _reg(
        {
            "a": {"steps": []},
            "b": {"steps": []},
        }
    )
    # 直接构造 a 调用 b、b 调用 a。
    skill_a = Skill(name="a", steps=[_call_step_with("b")])
    skill_b = Skill(name="b", steps=[_call_step_with("a")])
    registry.items["a"] = skill_a
    registry.items["b"] = skill_b
    errors = validate_skill(skill_a, registry)
    assert any(e["code"] == CODE_CYCLE for e in errors)


def test_validate_unknown_with_and_assign() -> None:
    from ah.skill.model import Skill

    registry = _reg({"target": {"inputs": ["item_id"], "outputs": ["order_id"], "defaults": {}}})
    skill = Skill(
        name="caller",
        steps=[
            _call_step_with("target", with_vars={"nonexistent_in": 1}, assign={"nope_out": "x"})
        ],
    )
    errors = validate_skill(skill, registry)
    assert any(e["code"] == CODE_UNKNOWN_WITH for e in errors)
    assert any(e["code"] == CODE_UNKNOWN_ASSIGN for e in errors)


def test_validate_loose_vars_allows_undeclared() -> None:
    from ah.skill.model import Skill

    registry = _reg({"target": {"inputs": ["item_id"], "outputs": ["order_id"], "defaults": {}}})
    skill = Skill(
        name="caller",
        steps=[_call_step_with("target", with_vars={"free": 1}, assign={"other": "x"})],
    )
    # 严格模式报错；
    errors_strict = validate_skill(skill, registry, strict_vars=True)
    assert any(e["code"] == CODE_UNKNOWN_WITH for e in errors_strict)
    # 宽松模式不报变量错误（但目标存在即通过）。
    errors_loose = validate_skill(skill, registry, strict_vars=False)
    assert not any(e["code"] in (CODE_UNKNOWN_WITH, CODE_UNKNOWN_ASSIGN) for e in errors_loose)


def test_validate_no_steps_and_bad_side_effect() -> None:
    from ah.skill.model import Skill

    skill = Skill(name="empty", steps=[], side_effects=["mutate"])
    errors = validate_skill(skill, None)
    assert any(e["code"] == CODE_NO_STEPS for e in errors)
    # 无 registry 时只做 schema 校验，不查引用。


def test_validate_call_no_target() -> None:
    from ah.skill.model import CallStep, Skill, Step

    registry = _reg({})
    skill = Skill(name="x", steps=[Step(name="c", call=CallStep(target=""))])
    errors = validate_skill(skill, registry)
    assert any(e["code"] == CODE_CALL_NO_TARGET for e in errors)


def test_load_skill_file(tmp_path: Path) -> None:
    p = tmp_path / "create-order.yaml"
    p.write_text("# comment\n" + str(_skill_yaml()), encoding="utf-8")
    skill = load_skill_file(p)
    assert skill.name == "create-order"


def test_discover_skill_files(tmp_path: Path) -> None:
    from ah.skill.model import discover_skill_files

    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "a.yaml").write_text("name: a\nsteps: []\n", encoding="utf-8")
    (tmp_path / "data" / "skills").mkdir(parents=True)
    (tmp_path / "data" / "skills" / "b.yaml").write_text("name: b\nsteps: []\n", encoding="utf-8")
    files = discover_skill_files(tmp_path)
    assert len(files) == 2
    # 非约定目录不被收集。
    (tmp_path / "skills" / "sub").mkdir()
    (tmp_path / "skills" / "sub" / "c.yaml").write_text("name: c\nsteps: []\n", encoding="utf-8")
    assert len(discover_skill_files(tmp_path)) == 3
