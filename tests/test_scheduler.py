#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2026-10-08
# @Filename: test_scheduler.py
# @Author  : Alan_Hsu
"""WS3 调度器（scheduled-run / ci-trigger / flaky-quarantine）的 hermetic 验证。

覆盖可观测 bug 场景（非 wiring 复制）：
- scheduled-run 按 suite 筛选用例，且仅命中的用例进报告、历史；
- 判闸：任一失败返回非 0；
- ci-trigger 的确定性路径匹配（LLM 关闭）与 [] 变更跳过；
- flaky 隔离：连续 flaky 达阈值才隔离、主回归排除、单独 --suite 可跑、
  YAML 写回合并 tags。
全部用例零网络依赖（stub 传输 / 纯本地算术）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ah.harness.loader import load_cases
from ah.predict.history import HistoryStore
from ah.reporting.writer import ResultRecord
from ah.scheduler import (
    QUARANTINE_SUITE,
    QUARANTINE_TAG,
    find_flaky_cases,
    load_project_cases,
    quarantine_cases,
    resolve_project_dir,
    run_ci_trigger,
    run_flaky_quarantine,
    run_scheduled_run,
    select_impacted_cases,
)


@pytest.fixture(autouse=True)
def _llm_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制关闭 LLM：确保所有用例 URL 匹配走确定性回退、零网络依赖（hermetic）。"""
    monkeypatch.setenv("LLMOFF", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


# ───────────────────────── 测试夹具 ─────────────────────────


def _args(**kw) -> SimpleNamespace:
    """构造 argparse 风格的参数对象（缺省其默认值）。"""
    defaults = {
        "suite": "regression",
        "priority": None,
        "env": None,
        "tags": None,
        "stub": True,
        "no_gate": False,
        "basename": None,
        "changes": None,
        "diff": "HEAD",
        "min": 2,
        "dry_run": False,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _write_project(root: Path, files: dict[str, str]) -> Path:
    """在 root 下生成一个业务项目（data/*.yaml）。返回项目目录。"""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (data / name).write_text(body, encoding="utf-8")
    return root


#: 一个含 smoke/regression/prod 三档的小数据集（接口路径各不相同）。
_SMOKE_CASE = """\
name: health-check
request:
  method: GET
  path: /health
suite: smoke
priority: P0
tags: [fast]
assert:
  status_code: 200
"""
_REGRESSION_CASE = """\
name: list-users
request:
  method: GET
  path: /users
suite: regression
priority: P1
tags: [core]
assert:
  status_code: 200
"""
_PROD_CASE = """\
name: place-order
request:
  method: POST
  path: /orders
suite: prod
priority: P0
tags: [pay]
assert:
  status_code: 201
"""

# ───────────────────────── scheduled-run ─────────────────────────


def test_scheduled_run_suite_filter_and_report(tmp_path):
    """scheduled-run --suite smoke 只跑 smoke 用例、出报告、写历史。"""
    project = _write_project(
        tmp_path,
        {
            "a.yaml": _SMOKE_CASE,
            "b.yaml": _REGRESSION_CASE,
            "c.yaml": _PROD_CASE,
        },
    )
    # 用 --suite smoke + stub：health-check 通过 -> 0。
    code = run_scheduled_run(_args(project=str(project), suite="smoke"))
    assert code == 0
    # 报告与历史都只包含 smoke 一条（health-check）。
    report_dir = project / "report"
    md = next(report_dir.glob("scheduled-smoke-*.md"))
    assert "health-check" in md.read_text(encoding="utf-8")
    assert "list-users" not in md.read_text(encoding="utf-8")
    rows = HistoryStore(report_dir / "history.jsonl").load()
    assert [r["name"] for r in rows] == ["health-check"]
    assert rows[0]["result"] == "PASS"


def test_scheduled_run_empty_suite_is_ok(tmp_path):
    """suite 未命中用例不判闸、返回 0，不写报告。"""
    project = _write_project(tmp_path, {"a.yaml": _PROD_CASE})
    code = run_scheduled_run(_args(project=str(project), suite="smoke"))
    assert code == 0


def test_scheduled_run_failure_gates_nonzero(tmp_path):
    """判闸语义：任一失败（非 --no-gate）返回非 0。"""
    # 一个必败用例：断言 201 但 200（stub 恒回 200）。
    bad = _SMOKE_CASE.replace("200", "500")
    project = _write_project(tmp_path, {"a.yaml": bad})
    code = run_scheduled_run(_args(project=str(project), suite="smoke"))
    assert code != 0
    # --no-gate 关闭判闸 -> 0。
    code2 = run_scheduled_run(
        _args(project=str(project), suite="smoke", no_gate=True)
    )
    assert code2 == 0


def test_scheduled_run_priority_combination(tmp_path):
    """--suite + --priority 组合过滤（AND 语义）。"""
    project = _write_project(
        tmp_path, {"a.yaml": _SMOKE_CASE, "b.yaml": _REGRESSION_CASE}
    )
    # regression 只有 list-users（P1）；request P0 仍命中（用例本身 P1 不匹配）。
    code = run_scheduled_run(
        _args(project=str(project), suite="regression", priority="P1")
    )
    assert code == 0
    md = next((project / "report").glob("scheduled-regression-*.md"))
    assert "list-users" in md.read_text(encoding="utf-8")
    assert "health-check" not in md.read_text(encoding="utf-8")


# ───────────────────────── ci-trigger ─────────────────────────


def _impact_project(tmp_path) -> Path:
    return _write_project(
        tmp_path,
        {
            "users.yaml": _REGRESSION_CASE,  # 接口 /users
            "orders.yaml": _PROD_CASE,  # 接口 /orders
        },
    )


def test_ci_trigger_deterministic_path_match(tmp_path):
    """LLM 关闭时：变更 /users 相关路径只命中 users 接口用例。"""
    project = _impact_project(tmp_path)
    cases = load_project_cases(project)
    # 变更路径命中 /users 接口（接口前缀匹配）。
    impacted = select_impacted_cases(cases, ["src/users/domain.py"])
    assert [c.name for c in impacted] == ["list-users"]
    # 变更订单相关则命中 orders。
    impacted2 = select_impacted_cases(cases, ["api/orders/route.py"])
    assert [c.name for c in impacted2] == ["place-order"]
    # 无关变更不命中任何接口。
    impacted3 = select_impacted_cases(cases, ["docs/readme.md"])
    assert impacted3 == []


def test_ci_trigger_runs_impacted_and_reports(tmp_path):
    """--changes 显式路径：只跑受影响接口且出报告/历史。"""
    project = _impact_project(tmp_path)
    code = run_ci_trigger(
        _args(project=str(project), changes="src/users/domain.py")
    )
    assert code == 0
    md = next((project / "report").glob("ci-*.md"))
    text = md.read_text(encoding="utf-8")
    assert "list-users" in text
    assert "place-order" not in text
    rows = HistoryStore(project / "report" / "history.jsonl").load()
    assert [r["name"] for r in rows] == ["list-users"]


def test_ci_trigger_no_changes_returns_zero(tmp_path):
    """无变更（或全被忽略）返回 0，不写报告。"""
    project = _impact_project(tmp_path)
    code = run_ci_trigger(_args(project=str(project), changes="docs/readme.md"))
    assert code == 0
    assert not list((project / "report").glob("ci-*.md"))


def test_ci_trigger_metrics_impacted_failure_gates(tmp_path):
    """受影响用例失败（stub 恒 200 -> 断言 500）判闸非 0。"""
    bad = _REGRESSION_CASE.replace("200", "500")
    project = _write_project(tmp_path, {"users.yaml": bad})
    code = run_ci_trigger(
        _args(project=str(project), changes="src/users/domain.py")
    )
    assert code != 0
    code2 = run_ci_trigger(
        _args(project=str(project), changes="src/users/domain.py", no_gate=True)
    )
    assert code2 == 0


# ───────────────────────── flaky 隔离仓 ─────────────────────────


def _history_rows(*specs: tuple[str, bool]) -> list[dict]:
    """构造历史行：每组 (name, flaky_bool)，按顺序排列。"""
    rows = []
    for idx, (name, flaky) in enumerate(specs):
        rows.append(
            {
                "name": name,
                "interface": name,
                "result": "PASS" if flaky else "FAIL",
                "flaky": flaky,
                "ts": idx,
            }
        )
    return rows


def test_find_flaky_cases_consecutive_threshold():
    """连续 flaky ≥ N 才隔离；非连续不隔离。"""
    rows = _history_rows(
        ("A", True), ("A", True), ("B", True), ("B", False), ("B", True)
    )
    assert find_flaky_cases(rows, min_consecutive=2) == ["A"]
    # B 只有 1 连击（被 FAIL 打断），不到 2。
    assert find_flaky_cases(rows, min_consecutive=2).count("B") == 0
    # 阈值 3 时 A 也不够。
    assert find_flaky_cases(rows, min_consecutive=3) == []


def test_find_flaky_cases_interleaved_by_other_cases():
    """不同用例交错不影响各自的连续计数。"""
    rows = _history_rows(
        ("A", True), ("B", False), ("A", True), ("A", True)
    )
    assert find_flaky_cases(rows, min_consecutive=2) == ["A"]


def test_quarantine_writes_back_tag_into_yaml(tmp_path):
    """flaky 用例写回 YAML 的 tags（合并，不产生重复键）。"""
    # 先带一个既有 tags 的单用例文件。
    project = _write_project(
        tmp_path, {"a.yaml": _SMOKE_CASE}  # 已有 tags: [fast]
    )
    cases = load_project_cases(project)
    flaky = find_flaky_cases(
        _history_rows(
            ("health-check", True), ("health-check", True), ("health-check", True)
        )
    )
    assert flaky == ["health-check"]
    q, ann = quarantine_cases(cases, flaky, write_back=True)
    assert q == ["health-check"]
    assert ann == []
    # 重读 YAML：tags 合并为 [fast, quarantined]，且仍能被 yaml 解析。
    reloaded = load_cases(str(project / "data" / "a.yaml"))
    assert QUARANTINE_TAG in reloaded[0].tags
    assert "fast" in reloaded[0].tags
    # 二次隔离幂等：不再写回（已含该标签）。
    q2, ann2 = quarantine_cases(reloaded, ["health-check"], write_back=True)
    assert q2 == ["health-check"]


def test_quarantine_without_tags_key_inserts(tmp_path):
    """无既有 tags 键时：name 行后插入 tags 且合法。"""
    project = _write_project(
        tmp_path, {"b.yaml": _REGRESSION_CASE}  # 无 tags 键（仅断言字段）
    )
    cases = load_project_cases(project)
    q, ann = quarantine_cases(cases, ["list-users"], write_back=True)
    assert q == ["list-users"]
    reloaded = load_cases(str(project / "data" / "b.yaml"))
    assert QUARANTINE_TAG in reloaded[0].tags


def test_scheduled_run_excludes_quarantined(tmp_path):
    """打标签后：主回归默认剔除隔离仓，--suite quarantine 单独可跑。"""
    project = _write_project(tmp_path, {"a.yaml": _REGRESSION_CASE})
    # 打上隔离标签并写回。
    cases = load_project_cases(project)
    quarantine_cases(cases, ["list-users"], write_back=True)
    # 回归默认剔除隔离仓：无用例可跑、不写回归报告，返回 0。
    code = run_scheduled_run(_args(project=str(project), suite="regression"))
    assert code == 0
    assert not list((project / "report").glob("scheduled-regression-*.md"))
    # 单独 --suite quarantine 跑。
    code2 = run_scheduled_run(
        _args(project=str(project), suite=QUARANTINE_SUITE)
    )
    assert code2 == 0
    md2 = next((project / "report").glob(f"scheduled-{QUARANTINE_SUITE}-*.md"))
    assert "list-users" in md2.read_text(encoding="utf-8")


def test_flaky_quarantine_cli_dry_run_and_write(tmp_path):
    """run_flaky_quarantine：--dry-run 不写回，正常模式写回 YAML。"""
    project = _write_project(tmp_path, {"a.yaml": _REGRESSION_CASE})
    # 先积累历史：list-users 连续 3 次 flaky。
    store = HistoryStore(project / "report" / "history.jsonl")
    store.append_all(
        [ResultRecord(name="list-users", result="PASS", flaky=True)] * 3
    )
    # dry-run：不打标、不写回，YAML 保持不变。
    code = run_flaky_quarantine(
        _args(project=str(project), min=2, dry_run=True)
    )
    assert code == 0
    text = (project / "data" / "a.yaml").read_text(encoding="utf-8")
    assert QUARANTINE_TAG not in text
    # 正常模式：打标写回 YAML。
    code2 = run_flaky_quarantine(_args(project=str(project), min=2))
    assert code2 == 0
    reloaded = load_cases(str(project / "data" / "a.yaml"))
    assert QUARANTINE_TAG in reloaded[0].tags


def test_flaky_quarantine_no_history_is_ok(tmp_path):
    """无历史记录时返回 0，不报错。"""
    project = _write_project(tmp_path, {"a.yaml": _REGRESSION_CASE})
    code = run_flaky_quarantine(_args(project=str(project)))
    assert code == 0


# ───────────────────────── 项目解析 ─────────────────────────


def test_resolve_project_dir(tmp_path):
    """领域解析：真实路径优先，projects/ 名其次，都不存在则报错。"""
    real = tmp_path / "proj"
    real.mkdir()
    assert resolve_project_dir(str(real)) == real
    with pytest.raises(FileNotFoundError):
        resolve_project_dir("nonexistent-project-name")
