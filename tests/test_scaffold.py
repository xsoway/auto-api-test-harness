#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026-10-08 20:37:43
# @Filename : test_scaffold.py
# @Author   : Alan_Hsu

"""WS2 · 脚手架 / 测试入口生成 / NL→用例落地（ah.scaffold）hermetic 验证。

覆盖四个可观测点：
- 脚手架目录结构完整且不会二次覆盖既有文件；
- 生成的 ``test_<name>.py`` 能被 pytest 收集并通过（hermetic 护栏）；
- ``--gen-entry`` 从多份 data/*.yaml 确定性生成可执行入口；
- add-case 的去重（_persist_case）能捕获重复用例并保证不重复入库。

全部在临时目录运行，不触碰仓库 projects/ 真实数据，LLMOFF=1 零网络。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from tempfile import mkdtemp

import yaml

from ah.harness.loader import load_cases
from ah.predict.generator import GeneratedCase
from ah.scaffold import (
    _persist_case,
    _slugify,
    add_case,
    scaffold_project,
)


def _make_project(**kwargs) -> Path:
    """在临时目录 scaffold 一个业务项目，返回项目目录。"""
    d = Path(mkdtemp())
    rc = scaffold_project(
        kwargs.get("business", "demo-biz"),
        kwargs.get("api", "https://api.example.com/v1"),
        project_dir=d,
        gen_entry=kwargs.get("gen_entry", False),
        name=kwargs.get("name", ""),
    )
    assert rc == 0, "scaffold 应返回 0"
    return d / _slugify(kwargs.get("business", "demo-biz"))


def _run_generated_pytest(proj: Path) -> subprocess.CompletedProcess:
    """在生成项目目录内跑 pytest（hermetic），返回结果。"""
    env = dict(os.environ, LLMOFF="1")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts="],
        cwd=proj,
        env=env,
        capture_output=True,
        text=True,
    )


# ── 功能5：脚手架目录结构 ───────────────────────────────────────────

def test_scaffold_creates_full_project_layout() -> None:
    """scaffold 生成 data/test/conftest/README/report 兜底结构完整。"""
    proj = _make_project(business="biz-alpha")
    assert (proj / "data" / "biz_alpha.yaml").is_file()
    # 修复过 double-test_ 前缀的回归：入口必须是 test_<name>.py，而非 test_test_<name>.py。
    assert (proj / "test_biz_alpha.py").is_file()
    assert not (proj / "test_test_biz_alpha.py").exists()
    assert (proj / "conftest.py").is_file()
    assert (proj / "README.md").is_file()
    # 骨架用例可被 load_cases 加载且带 status_code 断言。
    case = load_cases(proj / "data" / "biz_alpha.yaml")[0]
    assert case.name == "health-check"
    assert case.assert_.get("status_code") == 200


def test_scaffold_does_not_overwrite_existing_files() -> None:
    """已存在的 conftest.py / README.md / data 不被二次 scaffold 覆盖。"""
    proj = _make_project(business="biz-keep")
    conftest = proj / "conftest.py"
    conftest.write_text("# custom marker\nUSER_EDIT = True\n", encoding="utf-8")
    data = proj / "data" / "biz_keep.yaml"
    data.write_text("cases: []\n", encoding="utf-8")

    rc = scaffold_project("biz-keep", "https://alt.example.com/new", project_dir=proj.parent)
    assert rc == 0
    # 用户自定义内容保留。
    assert "# custom marker" in conftest.read_text(encoding="utf-8")
    assert load_cases(data) == []


def test_slugify_sanitizes_safely() -> None:
    """业务名清洗：中英文/特殊字符收敛为安全标识，空串回退 project。"""
    assert _slugify("示例M站") == "m"  # 非 ASCII 全部剥离
    assert _slugify("User API/v2") == "user_api_v2"
    assert _slugify("   ") == "project"


# ── 功能6：data→test 生成可被 pytest 收集并通过 ─────────────────────

def test_generated_entry_is_collectable_and_passes_hermetic() -> None:
    """生成的 test_<name>.py 可被 pytest 收集：护栏全绿、live 用例默认跳过。"""
    proj = _make_project(business="biz-run")
    result = _run_generated_pytest(proj)
    assert result.returncode == 0, f"生成入口未通过:\n{result.stdout}\n{result.stderr}"
    # 3 个护栏 + 1 个跳过（live 用例默认 skip）。
    assert "3 passed" in result.stdout and "1 skipped" in result.stdout


def test_gen_entry_regenerates_from_multiple_data_files() -> None:
    """--gen-entry 从既有（含多个）data/*.yaml 刷新出可收集执行的入口。"""
    proj = _make_project(business="biz-gen")
    # 追加第二个 data 文件，再 regen 入口。
    (proj / "data" / "extra.yaml").write_text(
        "cases:\n"
        "- name: second-case\n"
        "  request:\n"
        "    method: GET\n"
        "    path: https://x.test/b\n"
        "  assert:\n"
        "    status_code: 200\n",
        encoding="utf-8",
    )
    rc = scaffold_project("biz-gen", "https://x.test", project_dir=proj.parent, gen_entry=True)
    assert rc == 0
    # 入口仍是同一文件（不产生 test_test_*）且可收集。
    assert (proj / "test_biz_gen.py").is_file()
    result = _run_generated_pytest(proj)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    # 两条用例 → 3 护栏 + 2 个 live 跳过。
    assert "2 skipped" in result.stdout


# ── 功能7：NL→用例落地（去重 / hermetic 守卫）───────────────────────

def test_add_case_hermetic_returns_error_without_network() -> None:
    """LLMOFF=1 下 add-case 报清晰错误、不联网、不写文件。"""
    proj = _make_project(business="biz-herm")
    before = (proj / "data" / "biz_herm.yaml").read_text(encoding="utf-8")
    rc = add_case("biz-herm", "查询用户详情应返回 200", project_dir=proj.parent)
    assert rc != 0, "LLM 关闭时 add-case 应失败"
    # 数据文件未被改写。
    assert (proj / "data" / "biz_herm.yaml").read_text(encoding="utf-8") == before


def test_persist_case_appends_and_dedups() -> None:
    """_persist_case：新用例追加写入；同 (method,path,name) 重复则跳过不重复入库。"""
    proj = _make_project(business="biz-dedup")
    f = proj / "data" / "biz_dedup.yaml"
    gen = GeneratedCase(
        name="nl-order",
        request={"method": "POST", "path": "https://x.test/orders"},
        assert_={"status_code": 201},
    )

    # 追加：1（骨架）+ 1 = 2。
    assert _persist_case(f, gen, existing_keys=set()) == 0
    assert len(load_cases(f)) == 2
    # 保留原头部注释。
    head = "".join(f.read_text(encoding="utf-8").splitlines()[0:1])
    assert head == "# --- ah scaffold ---"

    # 重复：existing_keys 命中 -> 跳过，仍是 2 条。
    keys = {(c.request.get("method", "GET"), c.request_path, c.name) for c in load_cases(f)}
    assert _persist_case(f, gen, existing_keys=keys) == 0
    assert len(load_cases(f)) == 2


def test_persist_case_single_case_file_upgraded() -> None:
    """单用例文件（无 cases: 列表）也能被追加，归一化成 cases 列表。"""
    d = Path(mkdtemp())
    f = d / "solo.yaml"
    f.write_text(
        "name: one\nrequest:\n  method: GET\n  path: /a\nassert:\n  status_code: 200\n",
        encoding="utf-8",
    )
    gen = GeneratedCase(
        name="two",
        request={"method": "GET", "path": "https://x.test/b"},
        assert_={"status_code": 200},
    )
    assert _persist_case(f, gen, existing_keys=set()) == 0
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    assert isinstance(data.get("cases"), list)
    assert len(data["cases"]) == 2


def test_persist_case_preserves_unique_names() -> None:
    """去重后库内 name 唯一（避免参数化 id 冲突）。"""
    proj = _make_project(business="biz-uniq")
    f = proj / "data" / "biz_uniq.yaml"
    g1 = GeneratedCase(
        name="u1", request={"method": "GET", "path": "https://x.test/1"},
        assert_={"status_code": 200},
    )
    g2 = GeneratedCase(
        name="u2", request={"method": "GET", "path": "https://x.test/2"},
        assert_={"status_code": 200},
    )
    assert _persist_case(f, g1, set()) == 0
    assert _persist_case(f, g2, set()) == 0
    names = [c.name for c in load_cases(f)]
    assert len(names) == len(set(names))
