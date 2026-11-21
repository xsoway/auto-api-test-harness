#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15 20:10
# @Filename : test_observe_store.py
# @Author   : Alan_Hsu
"""本地可观测存储单元测试：幂等 upsert / 读 / 聚合 / 线程安全。

纯本地 SQLite 文件 IO，绝不触碰网络；LLMOFF/hermetic 下安全。
JSON 列在 store 层以原样 JSON 文本存储/读取（解析在 server 层做）。
"""
import json
import threading

from ah.observe.store import open_store


def test_upsert_run_roundtrip(tmp_path):
    """写入一条 run 后再读，标量字段一致；JSON 列以文本存取。"""
    store = open_store(tmp_path / "obs.db")
    store.upsert_run({
        "id": "r1", "name": "demo 1", "run_id": "r1",
        "start_time": "2026-10-15T12:00:00",
        "total": 5, "passed": 4, "failed": 1, "flaky": 0, "pass_rate": 0.8,
        "result_dist": {"PASS": 4, "FAIL": 1},
        "status_dist": {"200": 5},
        "avg_duration_ms": 120.5, "p95_duration_ms": 300.0,
        "total_duration_ms": 1000.0, "per_case_dur_ms": {"首页": 50.0},
        "total_cost": 0.00264, "source_file": "report_history.jsonl",
        "source_type": "history",
    })
    run = store.get_run("r1")
    assert run is not None
    assert run["id"] == "r1" and run["name"] == "demo 1"
    assert run["total"] == 5 and run["passed"] == 4 and run["failed"] == 1
    assert run["pass_rate"] == 0.8
    # JSON 列按文本存取，可 json.loads 还原成 dict。
    assert json.loads(run["result_dist"]) == {"PASS": 4, "FAIL": 1}
    assert json.loads(run["status_dist"]) == {"200": 5}
    assert json.loads(run["per_case_dur_ms"]) == {"首页": 50.0}
    assert run["source_type"] == "history"
    store.close()


def test_upsert_run_idempotent(tmp_path):
    store = open_store(tmp_path / "obs.db")
    store.upsert_run({"id": "r1", "name": "first", "total": 3})
    store.upsert_run({"id": "r1", "name": "second", "total": 5})
    (runs, _) = store.list_runs()
    assert len(runs) == 1
    assert runs[0]["name"] == "second" and runs[0]["total"] == 5
    store.close()


def test_upsert_run_skips_missing_id(tmp_path):
    """缺 id 的 run 直接跳过，不抛错。"""
    store = open_store(tmp_path / "obs.db")
    store.upsert_run({"name": "no-id"})
    (runs, total) = store.list_runs()
    assert runs == [] and total == 0
    store.close()


def test_upsert_observation_roundtrip(tmp_path):
    """observation 含 LLM 明细（input/output/model/tokens）被正确存取。"""
    store = open_store(tmp_path / "obs.db")
    store.upsert_run({"id": "r1", "name": "demo"})
    store.upsert_observation({
        "id": "o1", "run_id": "r1", "parent_id": None,
        "name": "LLM predict", "type": "GENERATION", "start_ms": 10.0,
        "latency_ms": 200.0, "level": "INFO", "status": "success",
        "input": {"role": "user", "content": "查询商品"},
        "output": {"advice": "推荐 3 款"},
        "model": "sglang-deepseek-v4-flash", "task": "predict-advice:json",
        "prompt_tokens": 120, "completion_tokens": 80, "cost": 0.001,
        "metadata": {"retry": 0}, "source": "llm_detail",
    })
    obs = store.get_observation("o1")
    assert obs is not None
    assert obs["type"] == "GENERATION"
    assert json.loads(obs["input"]) == {"role": "user", "content": "查询商品"}
    assert json.loads(obs["output"]) == {"advice": "推荐 3 款"}
    assert json.loads(obs["metadata"]) == {"retry": 0}
    assert obs["model"] == "sglang-deepseek-v4-flash"
    assert obs["prompt_tokens"] == 120 and obs["completion_tokens"] == 80
    assert obs["cost"] == 0.001
    store.close()


def test_observations_for_run_preserves_order(tmp_path):
    """observations_for_run 按 start_ms 升序返回全部子项。"""
    store = open_store(tmp_path / "obs.db")
    store.upsert_run({"id": "r1", "name": "demo"})
    store.upsert_observation({"id": "b", "run_id": "r1", "parent_id": None,
                              "name": "b", "type": "SPAN", "start_ms": 20.0})
    store.upsert_observation({"id": "a", "run_id": "r1", "parent_id": None,
                              "name": "a", "type": "SPAN", "start_ms": 5.0})
    rows = store.observations_for_run("r1")
    assert [r["id"] for r in rows] == ["a", "b"]
    store.close()


def test_metrics_aggregation(tmp_path):
    """metrics 聚合：成本/token/模型分布/成功率正确。"""
    store = open_store(tmp_path / "obs.db")
    store.upsert_run({"id": "r1", "name": "demo"})
    store.upsert_observation({"id": "g1", "run_id": "r1", "type": "GENERATION",
                              "name": "g1", "model": "gpt-4o-mini",
                              "prompt_tokens": 100, "completion_tokens": 50,
                              "cost": 0.001})
    store.upsert_observation({"id": "g2", "run_id": "r1", "type": "GENERATION",
                              "name": "g2", "model": "sglang-deepseek-v4-flash",
                              "prompt_tokens": 200, "completion_tokens": 60,
                              "cost": 0.002})
    # 用例：3 PASS 1 FAIL → 成功率 75%。
    for c in ["t1", "t2", "t3"]:
        store.upsert_test({"run_id": "r1", "name": c, "result": "PASS"})
    store.upsert_test({"run_id": "r1", "name": "t4", "result": "FAIL"})

    m = store.metrics()
    assert m["runs"] == 1
    assert m["total_cost"] == 0.003
    assert m["total_tokens"] == 410
    assert m["total_tests"] == 4 and m["passed"] == 3 and m["failed"] == 1
    assert m["success_rate"] == 75.0
    assert m["by_model"]["gpt-4o-mini"]["cost"] == 0.001
    assert m["by_model"]["sglang-deepseek-v4-flash"]["cost"] == 0.002
    store.close()

def test_analysis_aggregation(tmp_path):
    """analysis 聚合：状态分布/flaky/用例排名/耗时趋势正确。"""
    store = open_store(tmp_path / "obs.db")
    # 两个 run，各自带状态分布与 flaky。
    store.upsert_run({
        "id": "r1", "name": "run1", "start_time": "2026-10-09 10:00:00",
        "pass_rate": 100.0, "total_duration_ms": 1000.0, "flaky": 2,
        "status_dist": {"200": 10, "404": 1},
    })
    store.upsert_run({
        "id": "r2", "name": "run2", "start_time": "2026-10-09 11:00:00",
        "pass_rate": 50.0, "total_duration_ms": 2000.0, "flaky": 0,
        "status_dist": {"500": 3},
    })
    # 用例：t1 两个 run 都 PASS；t2 一个 PASS 一个 FAIL（跨 run 稳定指标）。
    for r in ("r1", "r2"):
        store.upsert_test({"run_id": r, "name": "首页", "result": "PASS",
                           "duration_ms": 100.0})
    store.upsert_test({"run_id": "r1", "name": "商品", "result": "PASS",
                       "duration_ms": 50.0})
    store.upsert_test({"run_id": "r2", "name": "商品", "result": "FAIL",
                       "duration_ms": 80.0})

    a = store.analysis()
    # 状态分布跨 run 合并。
    assert a["state_dist"] == {"200": 10, "404": 1, "500": 3}
    # flaky。
    assert a["flaky"]["total"] == 2
    assert a["flaky"]["runs_affected"] == 1 and a["flaky"]["runs_total"] == 2
    # 用例排名：商品 50% 在前（最不稳定），首页 100% 在后，均跨 2 run。
    names = [c["name"] for c in a["case_ranking"]]
    assert names == ["商品", "首页"]
    by = {c["name"]: c for c in a["case_ranking"]}
    assert by["商品"]["pass_rate"] == 50.0 and by["商品"]["total"] == 2
    assert by["首页"]["pass_rate"] == 100.0
    # 耗时趋势按时间升序。
    assert [t["time"] for t in a["duration_trend"]] == [
        "2026-10-09 10:00:00", "2026-10-09 11:00:00"]
    assert a["duration_trend"][0]["total_duration_ms"] == 1000.0
    assert a["duration_trend"][1]["pass_rate"] == 50.0
    store.close()


def test_cross_thread_read(tmp_path):
    """多线程下每线程懒连接可安全读（服务端多线程场景）。"""
    store = open_store(tmp_path / "obs.db")
    store.upsert_run({"id": "r1", "name": "demo"})
    results: list = []

    def reader():
        run = store.get_run("r1")
        results.append(run["name"] if run else None)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["demo"] * 4
    store.close()


def test_counts_summary(tmp_path):
    """counts 返回各类条目数量。"""
    store = open_store(tmp_path / "obs.db")
    store.upsert_run({"id": "r1", "name": "demo"})
    store.upsert_observation({"id": "o1", "run_id": "r1", "type": "SPAN", "name": "s"})
    store.upsert_test({"run_id": "r1", "name": "t1", "result": "PASS"})
    store.upsert_score({"id": "sc1", "run_id": "r1", "test_id": "x", "name": "quality",
                        "value": 0.9, "comment": "", "source": "predict"})
    assert store.counts() == {"runs": 1, "observations": 1, "tests": 1, "scores": 1}
    store.close()
