#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2026-10-09
# @Filename: test_debug.py
# @Author  : Alan_Hsu
"""⑤ 调试工具验证：ah debug（单用例 dump）与 ah openapi（paths→cases 导入）。

覆盖：debug 按名找用例 + stub 执行返回 0；openapi 解析 paths→用例、写入项目
data/、错误处理（缺文件/无 paths）。全部 hermetic（临时 YAML、stub 传输）。
"""

from __future__ import annotations

import pytest

from ah.debug import _case_name, _find_case, _openapi_to_cases, _run_openapi
from ah.harness.loader import load_cases


@pytest.fixture(autouse=True)
def _llm_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制关闭 LLM：确保零网络依赖（hermetic）。"""
    monkeypatch.setenv("LLMOFF", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


# ───────────────────────── openapi → cases ─────────────────────────


def test_openapi_paths_to_cases_expands_methods():
    """每个 path×method 生成一条用例，断言 200。"""
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/users": {"get": {"responses": {200: {}}},
                       "post": {"responses": {201: {}}}},
            "/users/{id}": {"get": {"responses": {200: {}}}},
            "/trace": {"trace": {"responses": {200: {}}}},
        },
    }
    cases = _openapi_to_cases(spec)
    assert [c.request["method"] for c in cases] == ["GET", "POST", "GET", "TRACE"]
    assert all(c.assert_["status_code"] == 200 for c in cases)
    assert all(c.suite == "regression" for c in cases)


def test_openapi_case_name_is_stable_and_safe():
    """路径模板文件名安全化：/users/{id}/list → users_id_list_get。"""
    assert _case_name("/users/{id}/list", "get") == "users_id_list_get"
    assert _case_name("/", "get") == "root_get"
    assert _case_name("/v1/orders", "POST") == "v1_orders_post"


def test_openapi_import_writes_yaml_to_project(tmp_path, monkeypatch):
    """真实 CLI 导出单个 cases 文档，load_cases 重新加载每个操作。"""
    from ah.cli import main

    monkeypatch.setenv("AH_LAYA", "off")
    project = tmp_path / "projects" / "p1"
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /users:\n"
        "    get:\n"
        "      responses: {200: {}}\n"
        "  /orders:\n"
        "    post:\n"
        "      responses: {201: {}}\n",
        encoding="utf-8",
    )
    code = main(["--llm-off", "openapi", str(project), str(spec), "--name", "imported"])
    assert code == 0
    dst = project / "data" / "imported.yaml"
    assert dst.is_file()
    cases = load_cases(dst)
    assert [(case.name, case.request["method"], case.request["path"]) for case in cases] == [
        ("users_get", "GET", "/users"),
        ("orders_post", "POST", "/orders"),
    ]
    assert all(case.assert_ == {"status_code": 200} for case in cases)


def test_openapi_missing_file_returns_2(tmp_path):
    """缺文件 → 返回 2（环境错误）。"""
    args = type("A", (), {"project": "x", "file": str(tmp_path / "no.yaml"),
                          "name": None})()
    assert _run_openapi(args) == 2


def test_openapi_no_paths_returns_2(tmp_path):
    """规范无 paths → 返回 2。"""
    spec = tmp_path / "s.yaml"
    spec.write_text("swagger: '2.0'\ninfo: {}\n", encoding="utf-8")
    args = type("A", (), {"project": "x", "file": str(spec), "name": None})()
    code = _run_openapi(args)
    assert code == 2


def test_openapi_invalid_yaml_returns_2(tmp_path):
    """坏 YAML → 返回 2。"""
    spec = tmp_path / "bad.yaml"
    spec.write_text("paths: [unclosed\n  bad", encoding="utf-8")
    args = type("A", (), {"project": "x", "file": str(spec), "name": None})()
    assert _run_openapi(args) == 2


# ───────────────────────── debug：按名找用例 + stub 执行 ─────────────────────────


def _write_case_project(tmp_path) -> str:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "c.yaml").write_text(
        "name: get-users\n"
        "request:\n  method: GET\n  path: /users\n"
        "assert:\n  status_code: 200\n",
        encoding="utf-8",
    )
    return str(tmp_path)


def test_debug_finds_case_by_name(tmp_path):
    """_find_case 从项目 data/ 按名字定位用例。"""
    project = _write_case_project(tmp_path)
    case = _find_case(project, "get-users")
    assert case.name == "get-users"
    assert case.request["path"] == "/users"


def test_debug_missing_case_raises(tmp_path):
    """找不到用例 → 抛 FileNotFoundError。"""
    project = _write_case_project(tmp_path)
    with pytest.raises(FileNotFoundError):
        _find_case(project, "no-such")


def test_debug_stub_run_single_returns_zero(tmp_path, capsys):
    """ah debug --stub：单用例执行完成并输出 dump，返回 0。"""
    from ah.debug import _run_debug

    project = _write_case_project(tmp_path)
    args = type("A", (), {"project": project, "case_name": "get-users", "stub": True})()
    assert _run_debug(args) == 0
    out = capsys.readouterr().out
    assert "get-users" in out
    assert "GET /users" in out


def test_cli_parser_accepts_debug_and_openapi():
    """CLI 可解析 debug / openapi 子命令。"""
    from ah.cli import build_parser

    parser = build_parser()
    a1 = parser.parse_args(["debug", "proj", "case", "--stub"])
    assert a1.command == "debug" and a1.stub is True
    a2 = parser.parse_args(["openapi", "proj", "spec.yaml", "--name", "gen"])
    assert a2.command == "openapi" and a2.name == "gen"


def test_openapi_import_refuses_to_replace_existing_case(tmp_path):
    """重新导入不得覆盖用户维护的目标文件。"""
    project = tmp_path / "project"
    data = project / "data"
    data.mkdir(parents=True)
    destination = data / "manual.yaml"
    original = "name: manually-maintained\nrequest: {method: GET, path: /manual}\n"
    destination.write_text(original, encoding="utf-8")
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        "openapi: 3.0.3\npaths:\n  /generated:\n"
        "    get:\n      operationId: generated\n      responses: {'200': {}}\n",
        encoding="utf-8",
    )
    args = type("Args", (), {
        "project": str(project), "file": str(spec), "name": "manual"
    })()
    assert _run_openapi(args) == 2
    assert destination.read_text(encoding="utf-8") == original
