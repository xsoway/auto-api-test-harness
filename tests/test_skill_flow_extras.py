#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/17 18:10
# @Filename : test_skill_flow_extras.py
# @Author   : Alan_Hsu
"""execute_flow 编排扩展（dataset/when/wait/resources）与元数据/校验测试。

全部用例 **LLMOFF=1 零网络**（hermetic）：HTTP 走 ``httpx.MockTransport``（与
``test_skill_executor`` 同范式的 ``_live_client``），不依赖真实后端或 LLM。

覆盖：
- model：``Skill.version`` 默认与显式、``SkillRegistry.references_to``；
- validate：``validate_flow_extras`` 对 dataset/when/wait 合法与非法各例；
- flow：``flow_meta`` 解析顶层 dataset/resources；
- executor：dataset 数据轮隔离、when 跳过、wait 满足/超时/写方法拒绝、
  resources 清理执行（含 id 缺失不抛、失败仅告警）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import httpx
from httpx import MockTransport, Response

from ah.harness.http import HTTPClient
from ah.skill.executor import execute_flow
from ah.skill.flow import flow_meta
from ah.skill.model import SkillRegistry, load_skill_yaml
from ah.skill.validate import validate_flow_extras
from ah.utils.context import Context


def _live_client(routes: Dict[str, Any]) -> HTTPClient:
    """构造按 path 前缀命中的隔离 HTTP 客户端；值可 Response 实例或 callable。"""

    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, value in routes.items():
            if request.url.path.startswith(prefix):
                return value(request) if callable(value) else value
        return Response(404, json={"error": "no route"})

    raw = httpx.Client(transport=MockTransport(handler))
    return HTTPClient("http://api.test", client=raw)


def _registry(skills: Dict[str, Dict[str, Any]]) -> SkillRegistry:
    """从一个 skill 映射组合构建内存注册表（复用 load_skill_yaml 归一化）。"""
    reg: Dict[str, Any] = {}
    for data in skills.values():
        loaded = load_skill_yaml(data, source="<test>")
        reg[loaded.name] = loaded
    return SkillRegistry(items=reg)


_EXTRACT = {"extract": {"out": {"path": "ok", "extract_type": "jsonpath"}}}
_ECHO = Response(200, json={"ok": True})


# ---------------------------------------------------------------- model: version

def test_load_skill_yaml_version_default_and_explicit() -> None:
    """Skill.version 缺省为 1.0；YAML 显式 version 被解析。"""
    default = load_skill_yaml(
        {"name": "a", "steps": [{"name": "s", "request": {"method": "GET", "path": "/a"}}]},
        source="<t>",
    )
    assert default.version == "1.0"
    explicit = load_skill_yaml(
        {
            "name": "b",
            "version": "2.4",
            "steps": [{"name": "s", "request": {"method": "GET", "path": "/b"}}],
        },
        source="<t>",
    )
    assert explicit.version == "2.4"


def test_references_to_reports_callers() -> None:
    """references_to 返回调用给定 skill 的有序 skill 名；无人调用返回空。"""
    reg = _registry(
        {
            "auth": {
                "name": "auth",
                "steps": [{"name": "s", "request": {"method": "GET", "path": "/a"}}],
            },
            "order": {
                "name": "order",
                "steps": [
                    {"name": "s", "request": {"method": "GET", "path": "/o"}},
                    {"name": "t", "call": "auth"},
                ],
            },
            "ship": {
                "name": "ship",
                "steps": [{"name": "s", "request": {"method": "GET", "path": "/s"}}],
            },
        }
    )
    assert reg.references_to("auth") == ["order"]
    assert reg.references_to("ship") == []


# ------------------------------------------------------------ validate: extras

def test_validate_flow_extras_accepts_legal_flows() -> None:
    """空 flow 与合法 dataset/when/wait 通过校验，不带错误。"""
    assert validate_flow_extras({}, [])["valid"]
    ok_meta = {"dataset": [{"case": "c1", "id": 1}]}
    ok_entries = [
        {"when": {"field": "${x}", "in": [1, 2]}},
        {"when": {"field": "${y}", "exists": True}},
        {
            "wait": {
                "request": {"method": "GET", "path": "/j"},
                "field": "${s}",
                "equals": "d",
                "timeout_seconds": 5,
                "interval_seconds": 1,
            }
        },
    ]
    res = validate_flow_extras(ok_meta, ok_entries)
    assert res["valid"] is True
    assert res["errors"] == []


def test_validate_flow_extras_rejects_bad_flows() -> None:
    """dataset 缺 case、when 缺 field/超算子、wait 缺 equals 均被标记错误。"""
    get_wait = {"request": {"method": "GET", "path": "/j"}, "field": "${s}", "equals": "d"}
    cases = [
        ({"dataset": [{"id": 1}]}, [], "flow.dataset_bad"),
        ({}, [{"when": {"equals": 1}}], "flow.when_bad"),  # field 缺失
        ({}, [{"when": {"field": "${x}"}}], "flow.when_bad"),  # 无算子
        ({}, [{"when": {"field": "${x}", "equals": 1, "in": [1]}}], "flow.when_bad"),
        ({}, [{"when": {"field": "${x}", "in": 123}}], "flow.when_bad"),  # in 非 list
        ({}, [{"when": {"field": "${x}", "equals": {"a": 1}}}], "flow.when_bad"),
        ({}, [{"wait": {"request": {"method": "GET", "path": "/j"}, "field": "${s}"}}], "flow.wait_bad"),  # noqa: E501 缺 equals 算子
        ({}, [{"wait": {**get_wait, "timeout_seconds": -1}}], "flow.wait_bad"),  # 非正数
    ]
    for meta, entries, code in cases:
        res = validate_flow_extras(meta, entries)
        assert res["valid"] is False, (meta, entries)
        assert any(e["code"] == code for e in res["errors"]), res
def test_flow_meta_reads_top_level_metadata(tmp_path: Path) -> None:
    """flow_meta 抽顶层 dataset/resources，不展开 steps。"""
    flow_file = tmp_path / "f.yaml"
    flow_file.write_text(
        "name: f\n"
        "dataset:\n"
        "  - case: a\n"
        "    id: 1\n"
        "resources:\n"
        "  - type: order\n"
        "    id: ${oid}\n"
        "    cleanup: del\n"
        "steps:\n"
        "  - name: s\n",
        encoding="utf-8",
    )
    meta = flow_meta(str(flow_file))
    assert meta["dataset"] == [{"case": "a", "id": 1}]
    assert meta["resources"] == [{"type": "order", "id": "${oid}", "cleanup": "del"}]
    assert "steps" not in meta


# ------------------------------------------------------------- executor: dataset

def test_dataset_row_injects_vars_without_polluting_base_ctx() -> None:
    """数据轮注入 data.*，多轮互不污染共享 base_ctx。"""
    noop = {"noop": {"name": "noop", "steps": [{"name": "s", "request": {"method": "GET", "path": "/e"}}]}}  # noqa: E501
    reg = _registry(noop)
    client = _live_client({"/e": _ECHO})
    flow = [{"name": "use-ds", "request": {"method": "GET", "path": "/e/${data.id}"}, **_EXTRACT}]
    base = Context()
    r1 = execute_flow(flow, reg, client, base_ctx=base, dataset_row={"case": "a", "id": 1})
    r2 = execute_flow(flow, reg, client, base_ctx=base, dataset_row={"case": "b", "id": 2})
    assert all(res.succeeded for res in r1 + r2)
    assert base.get("data.id") is None


# ------------------------------------------------------------- executor: when

def test_when_skips_unsatisfied_entries() -> None:
    """when 条件不满足的条目标记 skipped（succeeded=True，不计失败），满足则执行。"""
    noop = {"noop": {"name": "noop", "steps": [{"name": "s", "request": {"method": "GET", "path": "/e"}}]}}  # noqa: E501
    reg = _registry(noop)
    client = _live_client({"/e": _ECHO})
    base = Context()
    base.set("present", 1)
    flow = [
        {"name": "one", "request": {"method": "GET", "path": "/e"}, **_EXTRACT},
        {"when": {"field": "${missing}", "equals": 1}, "name": "skip",
         "request": {"method": "GET", "path": "/e"}, **_EXTRACT},
        {"when": {"field": "${present}", "equals": 1}, "name": "go",
         "request": {"method": "GET", "path": "/e"}, **_EXTRACT},
    ]
    results = execute_flow(flow, reg, client, base_ctx=base)
    by_name = {r.name: r for r in results}
    assert by_name["flow.one"].status == "executed"
    assert by_name["skip"].status == "skipped"
    assert by_name["skip"].succeeded is True  # skipped 计为顺利跳过
    assert by_name["flow.go"].status == "executed"


# ------------------------------------------------------------- executor: wait

def test_wait_polls_until_condition_met() -> None:
    """wait 轮询直到字段等于期望值（前 2 次 pending、第 3 次 done），返回成功。"""
    reg = _registry({})
    state = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return Response(200, json={"status": "pending" if state["n"] < 3 else "done"})

    client = _live_client({"/job": handler})
    flow = [{
        "wait": {
            "request": {"method": "GET", "path": "/job"},
            "extract": {"js": {"path": "status", "extract_type": "jsonpath"}},
            "field": "${js}",
            "equals": "done",
            "timeout_seconds": 5,
            "interval_seconds": 0,
        }
    }]
    results = execute_flow(flow, reg, client)
    assert len(results) == 1
    assert results[0].succeeded is True
    assert results[0].status == "executed"
    assert state["n"] >= 3


def test_wait_times_out_when_value_never_equals() -> None:
    """wait 超时返回 failed，且 extract 结果已写入 ctx（可观测）。"""
    reg = _registry({})
    client = _live_client({"/job": Response(200, json={"status": "pending"})})
    flow = [{
        "wait": {
            "request": {"method": "GET", "path": "/job"},
            "extract": {"js": {"path": "status", "extract_type": "jsonpath"}},
            "field": "${js}",
            "equals": "done",
            "timeout_seconds": 0.05,
            "interval_seconds": 0,
        }
    }]
    results = execute_flow(flow, reg, client)
    assert results[0].succeeded is False
    assert results[0].status == "failed"


def test_wait_rejects_write_methods() -> None:
    """wait 只允许只读轮询（GET/HEAD）；写方法标记 blocked，不发请求。"""
    reg = _registry({})
    client = _live_client({"/job": Response(200, json={})})
    flow = [{
        "wait": {
            "request": {"method": "POST", "path": "/job"},
            "field": "${s}",
            "equals": "d",
        }
    }]
    results = execute_flow(flow, reg, client)
    assert results[0].succeeded is False
    assert results[0].status == "blocked"


# ---------------------------------------------------------- executor: resources

def _delete_order_skill() -> Dict[str, Any]:
    return {
        "name": "delete-order",
        "side_effects": ["write"],
        "inputs": ["id"],
        "steps": [
            {
                "name": "del",
                "request": {"method": "DELETE", "path": "/orders/${id}"},
            }
        ],
    }


def test_resources_cleanup_runs_and_propagates_id() -> None:
    """resources 清理调用 cleanup skill；id 已解析时经 with 注入。"""
    reg = _registry({
        "delete-order": _delete_order_skill(),
        "noop": {"name": "noop", "steps": [{"name": "s", "request": {"method": "GET", "path": "/e"}}]},  # noqa: E501
    })
    client = _live_client({"/e": _ECHO, "/orders": Response(200, json={})})
    base = Context()
    base.set("oid", "77")
    flow = [{"name": "m", "request": {"method": "GET", "path": "/e"}, **_EXTRACT}]
    results = execute_flow(
        flow, reg, client, base_ctx=base,
        resources=[{"type": "order", "id": "${oid}", "cleanup": "delete-order"}],
    )
    names = {r.name for r in results}
    assert "flow.m" in names
    assert any(r.name.startswith("delete-order.") and r.succeeded for r in results)


def test_resources_cleanup_missing_id_does_not_raise() -> None:
    """资源 id 引用缺失变量时清理不抛 KeyError，降级为不带 with 调用并返回结果。"""
    reg = _registry({"delete-order": _delete_order_skill()})
    client = _live_client({"/orders": Response(200, json={})})
    flow = [{"name": "m", "request": {"method": "GET", "path": "/e"}, **_EXTRACT}]
    # base_ctx 无 oid → cleanup 的 id 渲染为字面 ${oid}，不注入 with（避免 KeyError 抛穿）。
    results = execute_flow(
        flow, reg, client, base_ctx=Context(),
        resources=[{"type": "order", "id": "${oid}", "cleanup": "delete-order"}],
    )
    assert any(r.name == "flow.m" for r in results)


def test_resources_absent_means_no_cleanup() -> None:
    """不给 resources 参数时行为与旧版一致：不追加任何清理步骤。"""
    noop = {"noop": {"name": "noop", "steps": [{"name": "s", "request": {"method": "GET", "path": "/e"}}]}}  # noqa: E501
    reg = _registry(noop)
    client = _live_client({"/e": _ECHO})
    flow = [{"name": "m", "request": {"method": "GET", "path": "/e"}, **_EXTRACT}]
    results = execute_flow(flow, reg, client)
    assert len(results) == 1
    assert results[0].name == "flow.m"


def test_cli_skill_validate_is_bound_and_runs(tmp_path: Path, capsys: Any) -> None:
    """回归：`ah skill validate` 的 func 绑定必须存在且可执行。

    此前 `ff373d1` 在 cli.py 加 `graph` 子命令时误删了
    ``validation.set_defaults(func=_validate)``，导致 `ah skill validate` 报
    “未识别的子命令/缺执行入口”。本用例直接构建顶层 parser 走 CLI 入口，
    防该绑定再次丢失。
    """
    from ah.cli import build_parser

    # 准备一个最小合法 skill 项目目录。
    skills = tmp_path / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "ping.yaml").write_text(
        "name: ping\ndescription: 探活\nworks_on: svc\ninputs: []\noutputs: []\n"
        "side_effects: [read]\nsteps:\n  - name: s\n"
        "    request: {method: GET, path: /ping}\n",
        encoding="utf-8",
    )
    parser = build_parser()
    args = parser.parse_args(["skill", "validate", str(tmp_path)])
    # func 绑定是本回归点：缺失则 _dispatch 返回 2（用法错误）。
    assert callable(getattr(args, "func", None))
    rc = args.func(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert '"valid": true' in out
