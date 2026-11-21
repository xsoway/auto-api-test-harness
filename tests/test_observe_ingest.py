#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15 20:15
# @Filename : test_observe_ingest.py
# @Author   : Alan_Hsu
"""本地可观测摄入单元测试：history/ledger/detail/report JSON/目录/seed。

坏行隔离是核心：单条坏行/缺字段不崩溃，跳过后继续。
纯本地文件 IO，零网络；LLMOFF/hermetic 下安全。
"""
import json

from ah.observe.ingest import (
    ingest_llm_detail,
    ingest_llm_ledger,
    ingest_report_dir,
    ingest_report_history,
    ingest_report_json,
    seed_demo,
)
from ah.observe.store import open_store
from ah.telemetry import LEDGER_FILENAME


def _write_jsonl(path, rows):
    """把 dict 列表写成 jsonl（每行一个 JSON）。"""
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_ingest_report_history(tmp_path):
    """history 摄入为 runs；含坏行时隔离跳过。"""
    store = open_store(tmp_path / "obs.db")
    h = tmp_path / "report_history.jsonl"
    _write_jsonl(h, [
        {"run_time": "2026-10-15T12:00:00", "total": 5, "passed": 4, "failed": 1,
         "pass_rate": 0.8, "result_dist": {"PASS": 4, "FAIL": 1}},
        "this-is-a-bad-line",
        {"run_time": "2026-10-15T13:00:00", "total": 3, "passed": 3, "failed": 0,
         "pass_rate": 1.0},
    ])
    n = ingest_report_history(store, h, name_prefix="tcc-m-api")
    assert n == 2  # 坏行跳过
    (runs, total) = store.list_runs()
    assert total == 2
    # list_runs 按 start_time DESC：runs[0] 是 13:00 行（无 result_dist → {}）。
    assert runs[0]["name"] == "tcc-m-api 2026-10-15T13:00:00"
    assert json.loads(runs[0]["result_dist"]) == {}
    # result_dist 在 12:00 行上。
    r2 = next(r for r in runs if r["start_time"] == "2026-10-15T12:00:00")
    assert json.loads(r2["result_dist"]) == {"PASS": 4, "FAIL": 1}
    store.close()


def test_ingest_llm_ledger(tmp_path):
    """llm_usage 摄入为 GENERATION observations（聚合账本，无 input/output）。"""
    store = open_store(tmp_path / "obs.db")
    store.upsert_run({"id": "r1", "name": "demo"})
    ledger = tmp_path / LEDGER_FILENAME
    _write_jsonl(ledger, [
        {"model": "gpt-4o-mini", "task": "predict-advice:json", "status": "success",
         "cost": 0.001, "prompt_tokens": 100, "completion_tokens": 50, "latency_ms": 200},
        {"model": "gpt-4o-mini", "task": "triage:json", "status": "error",
         "cost": 0.0005, "prompt_tokens": 20, "completion_tokens": 5, "latency_ms": 50},
    ])
    n = ingest_llm_ledger(store, ledger, run_id="r1")
    assert n == 2
    obs = store.observations_for_run("r1")
    assert [o["type"] for o in obs] == ["GENERATION", "GENERATION"]
    assert obs[0]["level"] == "INFO" and obs[1]["level"] == "ERROR"
    assert obs[0]["cost"] == 0.001
    store.close()


def test_ingest_llm_detail(tmp_path):
    """llm_detail 摄入为 GENERATION，含 input/output 明细。"""
    store = open_store(tmp_path / "obs.db")
    store.upsert_run({"id": "r1", "name": "demo"})
    d = tmp_path / "llm_detail.jsonl"
    _write_jsonl(d, [
        {"model": "sglang-deepseek-v4-flash", "task": "predict-advice:json",
         "status": "success", "cost": 0.002, "prompt_tokens": 120,
         "completion_tokens": 80, "latency_ms": 300,
         "input": {"role": "user", "content": "查找商品"},
         "output": {"advice": "推荐 3 款"}},
    ])
    n = ingest_llm_detail(store, d, run_id="r1")
    assert n == 1
    obs = store.observations_for_run("r1")[0]
    assert obs["source"] == "llm_detail"
    assert json.loads(obs["input"]) == {"role": "user", "content": "查找商品"}
    assert json.loads(obs["output"]) == {"advice": "推荐 3 款"}
    store.close()


def test_ingest_report_json(tmp_path):
    """report JSON（ResultRecord 列表）摄入为 tests + observations + scores。"""
    store = open_store(tmp_path / "obs.db")
    store.upsert_run({"id": "r1", "name": "demo"})
    rj = tmp_path / "test-tcc-m-api-20261015.json"
    rj.write_text(json.dumps({"results": [
        {"name": "测试首页", "result": "PASS", "duration": 100.0,
         "request": {"method": "GET", "path": "/api/home"},
         "response": {"status_code": 200, "text": "ok"},
         "assertions": [
             {"type": "status_code", "logic": "==", "expected": "200",
              "actual": "200", "passed": True},
         ]},
        {"name": "测试商品", "result": "FAIL", "failure_msg": "断言失败",
         "llm_notes": {"healed": False, "failure_governance": None, "recovered": False},
         "request": {"method": "GET", "path": "/api/car"},
         "response": {"status_code": 404, "text": "not found"},
         "assertions": [
             {"type": "status_code", "logic": "==", "expected": "200",
              "actual": "404", "passed": False},
         ]},
    ]}), encoding="utf-8")
    n = ingest_report_json(store, rj, run_id="r1")
    assert n == 2
    tests = store.tests_for_run("r1")
    assert len(tests) == 2
    names = {t["name"] for t in tests}
    assert names == {"测试首页", "测试商品"}
    pass_t = next(t for t in tests if t["name"] == "测试首页")
    assert pass_t["result"] == "PASS"
    fail_t = next(t for t in tests if t["name"] == "测试商品")
    assert fail_t["result"] == "FAIL"
    assert json.loads(fail_t["llm_notes"])["healed"] is False
    assert json.loads(pass_t["response"]) == {"status_code": 200, "text": "ok"}
    pass_asserts = json.loads(pass_t["assertions"])
    assert pass_asserts[0]["passed"] is True
    assert json.loads(fail_t["response"])["status_code"] == 404
    assert json.loads(fail_t["assertions"])[0]["passed"] is False
    # observations + scores 也生成。
    assert len(store.observations_for_run("r1")) >= 2
    assert len(store.scores_for_run("r1")) >= 2
    store.close()

def test_report_json_does_not_wipe_history_summary(tmp_path):
    """报告目录里 history 与 report JSON 共享 run_id：report JSON 摄入绝不能用
    字段不全的概要覆盖 history 已摄取的完整 run 行（回归：pass_rate/时长被清 0）。"""
    store = open_store(tmp_path / "obs.db")
    rd = tmp_path / "report"
    rd.mkdir()
    rt = "2026-10-15 18:56:54"
    # 1) history 先摄入完整概要。
    _write_jsonl(rd / "report_history.jsonl", [{
        "run_time": rt, "total": 38, "passed": 38, "failed": 0,
        "flaky": 0, "pass_rate": 67,
        "result_dist": {"PASS": 38, "FAIL": 0, "SKIP": 0},
        "status_dist": {"200": 38},
        "avg_duration_ms": 0.2, "p95_duration_ms": 0.4,
        "total_duration_ms": 9.2,
        "per_case_dur_ms": {"首页": 0.2, "商品": 0.3},
    }])
    ingest_report_history(store, rd / "report_history.jsonl", name_prefix="tcc-m-api")
    rid = "tcc-m-api:2026-10-15 18:56:54"
    before = store.get_run(rid)
    assert before is not None and before["pass_rate"] == 67
    assert before["total_duration_ms"] == 9.2
    # 2) 同 run_time 的 report JSON 再摄入 → 概要不得被覆盖清零。
    (rd / "report.json").write_text(json.dumps({
        "run_time": rt,
        "summary": {"total": 38, "passed": 38, "failed": 0},
        "results": [{"name": "首页", "result": "PASS", "duration": 0.2}],
    }), encoding="utf-8")
    ingest_report_dir(store, rd, name_prefix="tcc-m-api")
    after = store.get_run(rid)
    assert after["pass_rate"] == 67, f"pass_rate 被 report JSON 覆盖为 {after['pass_rate']}"
    assert after["total_duration_ms"] == 9.2
    assert after["p95_duration_ms"] == 0.4
    assert after["source_type"] == "history"
    # 3) 但 tests 仍挂到该 run，不因跳过概要而丢失。
    assert len(store.tests_for_run(rid)) == 1
    store.close()


def test_ingest_report_dir(tmp_path):
    """目录级摄入：history + ledger + detail + report JSON 一次到位。"""
    store = open_store(tmp_path / "obs.db")
    rd = tmp_path / "report"
    rd.mkdir()
    _write_jsonl(rd / "report_history.jsonl", [
        {"run_time": "2026-10-15T12:00:00", "total": 1, "passed": 1, "failed": 0,
         "pass_rate": 1.0},
    ])
    _write_jsonl(rd / LEDGER_FILENAME, [
        {"model": "gpt-4o-mini", "task": "t", "status": "success", "cost": 0.001,
         "prompt_tokens": 10, "completion_tokens": 5, "latency_ms": 100},
    ])
    (rd / "test-20261015.json").write_text(json.dumps({"results": [
        {"name": "t1", "result": "PASS", "duration": 10.0},
    ]}), encoding="utf-8")

    counts = ingest_report_dir(store, rd, name_prefix="tcc-m-api")
    assert counts["runs"] == 1
    assert counts["ledger_obs"] == 1
    assert counts["tests"] == 1
    assert len(store.list_runs()[0]) >= 1
    # ledger obs 挂到 history 同源 run id（name:run_time）。
    run = store.list_runs()[0][0]
    obs = store.observations_for_run(run["id"])
    assert any(o["model"] == "gpt-4o-mini" for o in obs)
    store.close()


def test_seed_demo_hermetic(tmp_path):
    """seed_demo 生成合成数据，run/obs/test/score 齐全，零网络。"""
    store = open_store(tmp_path / "obs.db")
    # 断言产物足够；seed 内部仅做本地 SQLite 写入，无网络。
    counts = seed_demo(store, runs=2, llm_per_run=3, cases_per_run=4)
    assert counts["runs"] == 2
    assert counts["observations"] == 2 * 3  # 每 run llm_per_run 个 observation
    # seed_demo 固定写满 case_names（5 个）的用例/打分，与 cases_per_run 无关。
    assert counts["tests"] == 2 * 5
    assert counts["scores"] == counts["tests"]
    m = store.metrics()
    assert m["runs"] == 2 and m["total_tests"] == 2 * 5
    store.close()


def test_ingest_missing_files_noop(tmp_path):
    """缺失文件/空目录摄入返回零计数，不抛错。"""
    store = open_store(tmp_path / "obs.db")
    counts = ingest_report_dir(store, tmp_path / "nonexistent", name_prefix="x")
    assert counts == {"runs": 0, "ledger_obs": 0, "detail_obs": 0, "tests": 0}
    assert ingest_report_history(store, tmp_path / "missing.jsonl") == 0
    assert ingest_llm_ledger(store, tmp_path / "missing.jsonl", run_id="r") == 0
    assert ingest_report_json(store, tmp_path / "missing.json", run_id="r") == 0
    store.close()
