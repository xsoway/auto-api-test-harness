#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15 20:20
# @Filename : test_observe_server.py
# @Author   : Alan_Hsu
"""本地可观测 HTTP 服务端到端测试。

用 ``serve(block=False)`` 起后台线程 + ``urllib`` 打本地端口，测完 shutdown。
全程本地回环，零网络；LLMOFF/hermetic 下安全。
"""
import json
import urllib.request

import pytest

from ah.observe.server import serve
from ah.observe.store import open_store


@pytest.fixture()
def server(tmp_path):
    """seed 一个含 LLM 明细/用例/打分的 store，并起后台 HTTP 服务。"""
    store = open_store(tmp_path / "obs.db")
    store.upsert_run({
        "id": "r1", "name": "demo-1", "run_id": "r1",
        "start_time": "2026-10-15T12:00:00", "total": 2, "passed": 1, "failed": 1,
        "pass_rate": 0.5, "result_dist": {"PASS": 1, "FAIL": 1},
        "status_dist": {"200": 1, "500": 1},
        "per_case_dur_ms": {"t1": 10.0, "t2": 20.0}, "total_cost": 0.003,
    })
    store.upsert_observation({
        "id": "g1", "run_id": "r1", "parent_id": None, "name": "LLM predict",
        "type": "GENERATION", "start_ms": 5.0, "latency_ms": 120.0, "level": "INFO",
        "status": "success", "input": {"q": "帮我找车"}, "output": {"a": "3 款"},
        "model": "sglang-deepseek-v4-flash", "task": "predict-advice:json",
        "prompt_tokens": 120, "completion_tokens": 80, "cost": 0.002,
        "metadata": {"retry": 0}, "source": "llm_detail",
    })
    store.upsert_observation({
        "id": "s1", "run_id": "r1", "parent_id": "g1", "name": "子观察",
        "type": "SPAN", "start_ms": 10.0, "latency_ms": 50.0, "level": "INFO",
        "status": "success", "input": None, "output": None, "model": "",
        "task": "", "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0,
        "metadata": {}, "source": "report",
    })
    for name, result in [("t1", "PASS"), ("t2", "FAIL")]:
        store.upsert_test({
            "run_id": "r1", "name": name, "result": result,
            "duration_ms": 10.0, "failure": "",
            "request": {"method": "GET", "path": f"/api/{name}"},
            "response": {"status_code": 200, "text": "ok" if result == "PASS" else "boom"},
            "assertions": [
                {"type": "status_code", "logic": "==", "expected": "200",
                 "actual": "200" if result == "PASS" else "500",
                 "passed": result == "PASS"},
            ],
            "llm_notes": {},
        })
    store.upsert_score({"id": "sc1", "run_id": "r1", "test_id": "t1", "name": "quality",
                        "value": 0.9, "comment": "优", "source": "predict"})

    srv = serve(store, host="127.0.0.1", port=0, block=False)
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    yield base, store
    srv.shutdown()
    srv.server_close()
    store.close()


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def test_metrics_endpoint(server):
    base, _ = server
    m = _get(f"{base}/api/metrics")
    assert m["runs"] == 1
    # total_cost 来自 observations 成本之和（g1=0.002, s1=0），非 run.total_cost 字段。
    assert m["total_cost"] == 0.002
    assert m["total_tokens"] == 200  # 120 + 80
    assert m["total_tests"] == 2 and m["passed"] == 1 and m["failed"] == 1
    assert m["success_rate"] == 50.0
    assert "sglang-deepseek-v4-flash" in m["by_model"]


def test_traces_list(server):
    base, _ = server
    resp = _get(f"{base}/api/traces")
    assert resp["total"] == 1 and len(resp["data"]) == 1
    assert resp["data"][0]["id"] == "r1"


def test_trace_detail_with_tree(server):
    base, _ = server
    d = _get(f"{base}/api/traces/r1")
    assert d["name"] == "demo-1"
    assert d["result_dist"] == {"PASS": 1, "FAIL": 1}  # JSON 已解析
    assert d["per_case_dur_ms"] == {"t1": 10.0, "t2": 20.0}
    # observation 树：根是 g1（父 LLM），子 s1 挂在 g1 下。
    roots = d["observations"]
    assert len(roots) == 1 and roots[0]["id"] == "g1"
    assert len(roots[0]["children"]) == 1 and roots[0]["children"][0]["id"] == "s1"
    # LLM 明细：input/output 已解析为对象，且注入 has_input/has_output 供前端展开。
    assert roots[0]["has_input"] is True and roots[0]["has_output"] is True
    assert roots[0]["input"] == {"q": "帮我找车"} and roots[0]["output"] == {"a": "3 款"}
    # 无明细的子节点（SPAN）has_input 应为 False。
    assert roots[0]["children"][0]["has_input"] is False
    # tests 与 scores 一并返回。
    assert len(d["tests"]) == 2
    assert len(d["scores"]) == 1
    # tests 的 request/response/assertions 已被 _decode_test 解码为对象（非 JSON 串）。
    t1 = next(t for t in d["tests"] if t["name"] == "t1")
    assert t1["request"] == {"method": "GET", "path": "/api/t1"}
    assert t1["response"] == {"status_code": 200, "text": "ok"}
    assert t1["assertions"][0]["passed"] is True
    t2 = next(t for t in d["tests"] if t["name"] == "t2")
    assert t2["response"]["status_code"] == 200
    assert t2["assertions"][0]["passed"] is False



def test_trace_detail_with_encoded_id(server):
    """URL 中 %3A 等编码后的 id 段应 unquote 后再查（真实 run id 含冒号）。"""
    base, store = server
    # 造一个带冒号的 run id，模拟 seed:run-N 这类包含特殊字符的实际 id。
    store.upsert_run({"id": "seed:run-3", "name": "demo-3", "run_id": "seed:run-3"})
    import urllib.parse

    enc = urllib.parse.quote("seed:run-3", safe="")
    assert "%" in enc  # 确认确实编码了冒号
    d = _get(f"{base}/api/traces/{enc}")
    assert d["id"] == "seed:run-3"
    assert d["name"] == "demo-3"



def test_observation_endpoint(server):
    base, _ = server
    obs = _get(f"{base}/api/observations/g1")
    assert obs["type"] == "GENERATION"
    assert obs["model"] == "sglang-deepseek-v4-flash"
    assert obs["metadata"] == {"retry": 0}  # JSON 已解析
    assert json.loads(obs["input"]) == {"q": "帮我找车"}


def test_tests_and_scores_endpoints(server):
    base, _ = server
    tests = _get(f"{base}/api/tests?run_id=r1")
    assert tests["total"] == 2
    assert {t["result"] for t in tests["data"]} == {"PASS", "FAIL"}
    # 按结果过滤。
    fail_only = _get(f"{base}/api/tests?run_id=r1&result=FAIL")
    assert fail_only["total"] == 1 and fail_only["data"][0]["name"] == "t2"
    # scores：无 run_id → 空。
    scores = _get(f"{base}/api/scores?run_id=r1")
    assert scores["total"] == 1 and scores["data"][0]["value"] == 0.9
def test_root_index(server):
    base, _ = server
    # web/dist 为真实前端构建产物时，/ 应托起前端 index.html（Langfuse 式单页）。
    with urllib.request.urlopen(f"{base}/", timeout=5) as r:
        html = r.read().decode("utf-8")
    assert '<div id="root"></div>' in html


def test_not_found_404(server):
    base, _ = server
    import urllib.error

    with pytest.raises(urllib.error.HTTPError) as e:
        _get(f"{base}/api/traces/nonexistent")
    assert e.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(f"{base}/api/nope")
    assert e.value.code == 404
