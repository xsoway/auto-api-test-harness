#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/17 10:05
# @Filename : validate.py
# @Author   : Alan_Hsu
"""Skill 静态校验：schema 完整性 + 互调引用 + 变量名一致性 + 环检测。

校验是**纯静态**的：不执行任何请求（``LLMOFF=1`` 完全安全，可进 CI）。目标是在加载期
就报出明确的引用/变量错误，而不是让坏 skill 到运行期才失败：

- schema：``name`` 必填、``steps`` 必填非空、`副作用取值合法、`call` 步骤必须有 target。
- 引用完整性：``call`` 的目标 skill 必须存在；嵌套调用可跨层，但禁止循环引用（环）。
- 变量一致性：``with``/``assign`` 的键必须在被调 skill 的 ``defaults``/``outputs`` 范围内
  （严格模式强制，宽松模式仅告警）。``assign`` 的键必须可解析（校验阶段只做形式检查）。

结果统一为 ``{valid, errors: [{code, message, skill?, detail?}]}``，与现有 scenario/
validate 的 JSON 结构保持一致，便于 CLI 输出与测试断言。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ah.skill.model import SIDE_EFFECTS, Skill, SkillRegistry

#: 校验错误码（稳定标识，供测试与报告匹配）。
CODE_NAME_REQUIRED = "skill.name_required"
CODE_NO_STEPS = "skill.no_steps"
CODE_BAD_SIDE_EFFECT = "skill.bad_side_effect"
CODE_CALL_NO_TARGET = "skill.call_no_target"
CODE_UNKNOWN_TARGET = "skill.unknown_target"
CODE_CYCLE = "skill.cycle"
CODE_UNKNOWN_WITH = "skill.unknown_with"
CODE_UNKNOWN_ASSIGN = "skill.unknown_assign"

#: flow 编排新能力（数据驱动/条件/轮询/资源清理）校验错误码。
CODE_FLOW_DATASET_BAD = "flow.dataset_bad"      # dataset 行缺 case 或不是映射
CODE_FLOW_RESOURCE_BAD = "flow.resource_bad"    # resources 条缺 type/id/cleanup
CODE_FLOW_WHEN_BAD = "flow.when_bad"            # when 缺 field 或算子非法
CODE_FLOW_WAIT_BAD = "flow.wait_bad"            # wait 缺 field/equals 或算子非法
CODE_FLOW_VERSION_BAD = "skill.version_bad"     # skill version 非字符串


def _err(code: str, message: str, *, skill: str = "", detail: Any = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"code": code, "message": message}
    if skill:
        out["skill"] = skill
    if detail is not None:
        out["detail"] = detail
    return out


def validate_skill(
    skill: Skill,
    registry: Optional[SkillRegistry] = None,
    *,
    strict_vars: bool = True,
    _stack: Optional[List[str]] = None,
    _seen: Optional[set[tuple[str, int]]] = None,
) -> List[Dict[str, Any]]:
    """校验单个 skill；若传 ``registry`` 则同时做互调引用/变量/环校验。

    ``strict_vars=True`` 时，``with``/``assign`` 的不一致记为错误；``False`` 时降级为
    仅直接校验 ``call`` 目标存在（宽松模式）。内部通过 ``_stack`` 做调用栈环检测
    （同一条路径上重复出现即算环，不同路径的重复合法）。

    返回错误列表（空 = 通过）。
    """
    errors: List[Dict[str, Any]] = []
    if not skill.name:
        errors.append(_err(CODE_NAME_REQUIRED, "skill 缺少 name", skill=str(skill.source_path)))
    if not skill.steps:
        errors.append(_err(CODE_NO_STEPS, f"skill {skill.name} 没有 steps", skill=skill.name))
    for side in skill.side_effects:
        if side not in SIDE_EFFECTS:
            errors.append(
                _err(
                    CODE_BAD_SIDE_EFFECT,
                    f"skill {skill.name} 的 side_effects 含非法值 {side!r}"
                    f"（合法: {sorted(SIDE_EFFECTS)}）",
                    skill=skill.name,
                )
            )

    if not isinstance(skill.version, str) or not skill.version.strip():
        errors.append(
            _err(
                CODE_FLOW_VERSION_BAD,
                f"skill {skill.name} 的 version 必须是非空字符串（当前 {skill.version!r}），"
                f"供注册表变更影响分析使用",
                skill=skill.name,
            )
        )

    if registry is None:
        return errors

    stack = _stack if _stack is not None else []
    seen = _seen if _seen is not None else set()

    for step in skill.steps:
        if not step.is_call or step.call is None:
            continue
        target = step.call.target
        if not target:
            errors.append(
                _err(
                    CODE_CALL_NO_TARGET,
                    f"skill {skill.name} 存在无 target 的 call 步骤",
                    skill=skill.name,
                )
            )
            continue
        callee = registry.get(target)
        if callee is None:
            errors.append(
                _err(
                    CODE_UNKNOWN_TARGET,
                    f"skill {skill.name} 调用了不存在的 skill {target!r}",
                    skill=skill.name,
                    detail=target,
                )
            )
            continue
        # 环检测：调用栈上重复出现即环（同一路径重复引用）。
        if target in stack:
            cycle_path = stack + [target]
            errors.append(
                _err(
                    CODE_CYCLE,
                    f"skill 环引用: {' -> '.join(cycle_path)}",
                    skill=skill.name,
                    detail=cycle_path,
                )
            )
            continue
        # 变量一致性（严格模式）。
        if strict_vars:
            with_keys = set(step.call.with_vars.keys())
            default_keys = set(callee.defaults.keys())
            unknown_with = with_keys - default_keys - set(callee.inputs)
            # with 允许命中 defaults 或 inputs；两者都未声明才算未知。
            if unknown_with:
                errors.append(
                    _err(
                        CODE_UNKNOWN_WITH,
                        f"skill {skill.name} 给 {target!r} 传了未声明的入参 {sorted(unknown_with)}"
                        f"（被调 skill 未在 inputs/defaults 声明）",
                        skill=skill.name,
                        detail=sorted(unknown_with),
                    )
                )
            assign_keys = set(step.call.assign.keys())
            output_keys = set(callee.outputs)
            unknown_assign = assign_keys - output_keys
            if unknown_assign:
                errors.append(
                    _err(
                        CODE_UNKNOWN_ASSIGN,
                        f"skill {skill.name} assign 了 {target!r} 未声明的产出"
                        f"{sorted(unknown_assign)}（被调 skill 的 outputs 未包含）",
                        skill=skill.name,
                        detail=sorted(unknown_assign),
                    )
                )
        # 递归校验被调 skill（同一栈路径，防止环扩展实现）。
        errors.extend(
            validate_skill(
                callee,
                registry,
                strict_vars=strict_vars,
                _stack=stack + [skill.name],
                _seen=seen,
            )
        )
    return errors


def validate_registry(registry: SkillRegistry, *, strict_vars: bool = True) -> Dict[str, Any]:
    """校验注册表内全部 skill，返回 ``{valid, errors}``。

    ``valid`` 为 True 当且仅当没有任何错误。环检测、引用完整性、变量一致性汇总为一份
    去重的错误列表（避免同一 skill 被多次递归时重复报错；用 (code, skill, detail) 去重）。
    """
    all_errors: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, Any]] = set()
    for name in registry.names():
        skill = registry.get(name)
        if skill is None:
            continue
        for err in validate_skill(skill, registry, strict_vars=strict_vars):
            key = (err["code"], err.get("skill", ""), str(err.get("detail", "")))
            if key not in seen:
                seen.add(key)
                all_errors.append(err)
    return {"valid": not all_errors, "errors": all_errors}

#: ``when:`` 条件分支支持的有穷算子（不执行任意表达式，防注入）。
WHEN_OPS: set[str] = {"equals", "not_equals", "in", "exists"}
#: ``wait:`` 异步轮询支持的有穷算子（等待目标字段等于期望值）。
WAIT_OPS: set[str] = {"equals"}


def validate_flow_extras(
    flow: Dict[str, Any], entries: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """校验 flow 编排新能力（数据驱动/条件/轮询/资源清理）的 schema 与算子合法性。

    纯静态、零网络（``LLMOFF=1`` 安全）。校验对象是 flow 的**可选扩展字段**：

    - 顶层 ``dataset:``：数据驱动多轮（§7.3）；每行是映射且含 ``case`` 键。
    - 顶层 ``resources:``：资源清理（§10.2）；每行含 ``type/id/cleanup``。
    - 条目级 ``when:``：条件分支（§9.2）；``field`` 必填 + 恰一个算子
      ``equals/not_equals/in/exists``。
    - 条目级 ``wait:``：异步轮询（§9.1）；``field/equals`` 必填，
      ``timeout_seconds/interval_seconds`` 可选正数。

    返回 ``{valid, errors: [{code, message, detail?}]}``，与 :func:`validate_registry`
    结构一致。dataset/resources 是 flow 顶层，entries 是 ``flow:`` 列表本体。
    """
    errors: List[Dict[str, Any]] = []

    # ---- 顶层 dataset（数据驱动多轮）----
    dataset = flow.get("dataset")
    if dataset is not None:
        if not isinstance(dataset, list):
            errors.append(
                _err(CODE_FLOW_DATASET_BAD, "flow 顶层 dataset 必须是列表（每行一组入参）")
            )
        else:
            for idx, row in enumerate(dataset):
                if not isinstance(row, dict) or "case" not in row:
                    errors.append(
                        _err(
                            CODE_FLOW_DATASET_BAD,
                            f"flow dataset[{idx}] 必须是映射且含 case 键（标注该行数据场景）",
                            detail=idx,
                        )
                    )

    # ---- 顶层 resources（资源清理登记）----
    resources = flow.get("resources")
    if resources is not None:
        if not isinstance(resources, list):
            errors.append(
                _err(CODE_FLOW_RESOURCE_BAD, "flow 顶层 resources 必须是列表")
            )
        else:
            for idx, res in enumerate(resources):
                req_keys = ("type", "id", "cleanup")
                if not isinstance(res, dict) or not all(k in res for k in req_keys):
                    errors.append(
                        _err(
                            CODE_FLOW_RESOURCE_BAD,
                            f"flow resources[{idx}] 必须含 type/id/cleanup 三个键",
                            detail=idx,
                        )
                    )

    # ---- 条目级 when（条件分支）与 wait（异步轮询）----
    for n, entry in enumerate(entries):
        when = entry.get("when")
        if when is not None:
            if not isinstance(when, dict) or not isinstance(when.get("field"), str):
                errors.append(
                    _err(
                        CODE_FLOW_WHEN_BAD,
                        f"flow 条目[{n}] 的 when 必须是映射且 field 为字符串（引用 ctx 变量）",
                        detail=n,
                    )
                )
            else:
                op_keys = [k for k in when if k in WHEN_OPS]

                if len(op_keys) != 1:
                    errors.append(
                        _err(
                            CODE_FLOW_WHEN_BAD,
                            f"flow 条目[{n}] 的 when 必须恰含一个算子 "
                            f"(equals/not_equals/in/exists)，实际 {op_keys or '无'}",
                            detail=n,
                        )
                    )
                else:
                    op = op_keys[0]
                    # 值直接挂在算子键下（equals: 1 / in: [1,2] / exists: true），
                    # 与方案 §9.2 风格一致；不存在独立的 value 键。
                    if op == "in" and not isinstance(when.get("in"), list):
                        errors.append(
                            _err(
                                CODE_FLOW_WHEN_BAD,
                                f"flow 条目[{n}] 的 when.in 的值必须是列表",
                                detail=n,
                            )
                        )
                    elif op in ("equals", "not_equals") and (
                        op not in when or isinstance(when.get(op), dict)
                    ):
                        errors.append(
                            _err(
                                CODE_FLOW_WHEN_BAD,
                                f"flow 条目[{n}] 的 when.{op} 必须给标量期望值",
                                detail=n,
                            )
                        )
                    # exists 不需要值：只判断字段是否存在于 ctx。
        wait = entry.get("wait")
        if wait is not None:
            if not isinstance(wait, dict) or not isinstance(wait.get("field"), str):
                errors.append(
                    _err(
                        CODE_FLOW_WAIT_BAD,
                        f"flow 条目[{n}] 的 wait 必须是映射且 field 为字符串",
                        detail=n,
                    )
                )
            else:
                op_keys = [k for k in wait if k in WAIT_OPS]
                if len(op_keys) != 1 or op_keys[0] != "equals":
                    errors.append(
                        _err(
                            CODE_FLOW_WAIT_BAD,
                            f"flow 条目[{n}] 的 wait 必须恰含 equals 算子（等待字段等于期望值）",
                            detail=n,
                        )
                    )
                for key in ("timeout_seconds", "interval_seconds"):
                    val = wait.get(key)
                    if val is not None and (not isinstance(val, (int, float)) or val <= 0):
                        errors.append(
                            _err(
                                CODE_FLOW_WAIT_BAD,
                                f"flow 条目[{n}] 的 wait.{key} 必须是正数",
                                detail=n,
                            )
                        )

    return {"valid": not errors, "errors": errors}


def summary(registry: SkillRegistry) -> Dict[str, Any]:
    """生成人类/CLI 可读的 skill 目录摘要（不含校验判定，便于列表/图）。"""
    return {
        "count": len(registry.items),
        "skills": [
            {
                "name": s.name,
                "works_on": s.works_on,
                "inputs": list(s.inputs),
                "outputs": list(s.outputs),
                "side_effects": list(s.side_effects),
                "calls": s.call_targets(),
                "source": s.source_path,
            }
            for s in registry.items.values()
        ],
    }
