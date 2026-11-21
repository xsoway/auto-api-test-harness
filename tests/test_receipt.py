#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/06/26 14:00
# @Filename : test_receipt.py
# @Author   : Alan_Hsu

"""VerificationReceipt（验真回执）单测。

覆盖《让 AI 编码 Agent 自己跑回归》三字段契约的核心判定：
- 字段二硬规则：断言通过数为 0 → 只能 UNVERIFIED，绝不 PASS。
- 三字段（跑没跑完/过没过/证据在哪）都能从回执里读到。
- 与 StepResult/ResultRecord 的鸭子类型兼容及序列化往返。
"""

from __future__ import annotations

import json

from ah.core.orchestrator import StepResult
from ah.core.receipt import build_verification_receipt
from ah.reporting.writer import ResultRecord


def _step(*, verdict="passed", status="executed", assertions=(), started="", ended=""):
    """构造一个最小 StepResult，断言列表为 (passed,) 元组序列。"""
    return StepResult(
        name="t",
        succeeded=verdict == "passed",
        status=status,
        verdict=verdict,
        assertions=[{"type": "eq", "passed": p} for p in assertions] or [],
        started_at=started,
        ended_at=ended,
    )


# ---------- 字段二：断言与步骤分开计数 ----------

def test_passes_only_when_assertions_positive_and_all_green():
    """有一张通过断言 → 才给 PASS；步骤/断言分开给总数。"""
    results = [_step(verdict="passed", assertions=(True, True))]
    receipt = build_verification_receipt(results)
    assert receipt["verdict"] == "PASS"
    assert receipt["status"] == "completed"
    assert receipt["steps"] == {"total": 1, "passed": 1, "failed": 0}
    assert receipt["assertions"] == {"total": 2, "passed": 2, "failed": 0}


def test_zero_assertion_hard_rule_never_passes():
    """硬规则：一步 0 断言，即便上层到 passed → UNVERIFIED，不是 PASS。"""
    # 场景 A：上层标 passed 但没有任何断言（模拟爬虫/烟雾用例零断言）。
    no_assert = [_step(verdict="passed", assertions=())]
    receipt = build_verification_receipt(no_assert)
    assert receipt["verdict"] == "UNVERIFIED"
    assert receipt["status"] == "completed"
    assert receipt["assertions"] == {"total": 0, "passed": 0, "failed": 0}

    # 场景 B：步骤标 passed 但断言全是失败 → 同样不是 PASS。
    bad = [_step(verdict="passed", assertions=(False,))]
    receipt = build_verification_receipt(bad)
    assert receipt["verdict"] == "FAIL"
    assert receipt["assertions"] == {"total": 1, "passed": 0, "failed": 1}


def test_assertion_failure_overrides_step_pass():
    """步骤通过但某条断言失败 → 整体 FAIL。"""
    results = [_step(verdict="passed", assertions=(True, True, False))]
    receipt = build_verification_receipt(results)
    assert receipt["verdict"] == "FAIL"
    assert receipt["assertions"] == {"total": 3, "passed": 2, "failed": 1}


def test_step_unverified_maps_and_counts():
    """unverified 步骤计为失败侧目标（非通过），且遇失败步骤整体 FAIL。"""
    results = [
        _step(verdict="unverified", assertions=()),
        _step(verdict="passed", assertions=(True,)),
    ]
    receipt = build_verification_receipt(results)
    assert receipt["verdict"] == "FAIL"
    # 未验证那一步不算通过也不算失败 → 只是不计进 passed；有断言的全过。
    assert receipt["steps"]["passed"] == 1
    assert receipt["steps"]["failed"] == 0


def test_empty_run_maps_to_not_executed():
    """没有任何结果 → 不是通过，是未执行候选（SKIP 语义）。"""
    receipt = build_verification_receipt([])
    assert receipt["status"] == "not_executed"
    assert receipt["verdict"] == "SKIP"


# ---------- 字段一：跑没跑完 + 起止时间 ----------

def test_time_fields_are_present_and_derived():
    """开始/结束时间从结果取最早/最晚；显式传入优先。"""
    results = [
        _step(
            verdict="passed", assertions=(True,),
            started="2026-01-01T10:00:00", ended="2026-01-01T10:00:05",
        ),
        _step(
            verdict="passed", assertions=(True,),
            started="2026-01-01T10:00:02", ended="2026-01-01T10:00:09",
        ),
    ]
    receipt = build_verification_receipt(results)
    assert receipt["started_at"] == "2026-01-01T10:00:00"
    assert receipt["ended_at"] == "2026-01-01T10:00:09"

    # 显式传参覆盖推导结果。
    receipt = build_verification_receipt(results, started_at="X", ended_at="Y")
    assert receipt["started_at"] == "X"
    assert receipt["ended_at"] == "Y"


def test_new_step_result_carries_started_ended_roundtrip():
    """StepResult.{to_dict,from_dict} 保留 started_at/ended_at（持久化往返）。"""
    sr = _step(
        verdict="passed", assertions=(True,),
        started="2026-06-01T09:00:00", ended="2026-06-01T09:00:01",
    )
    restored = StepResult.from_dict(sr.to_dict())
    assert restored.started_at == "2026-06-01T09:00:00"
    assert restored.ended_at == "2026-06-01T09:00:01"


def test_result_record_roundtrip_via_from_step_from_dict():
    """ResultRecord 从 StepResult 转换后再从 dict 重建，起止时间不丢。"""
    sr = _step(
        verdict="passed", assertions=(True,),
        started="2026-06-01T09:00:00", ended="2026-06-01T09:00:01",
    )
    rec = ResultRecord.from_step(sr)
    assert rec.started_at == "2026-06-01T09:00:00"
    assert rec.ended_at == "2026-06-01T09:00:01"
    restored = ResultRecord.from_dict(rec.to_dict())
    assert restored.started_at == "2026-06-01T09:00:00"
    assert restored.ended_at == "2026-06-01T09:00:01"


def test_old_records_without_timestamps_are_tolerant():
    """旧格式记录缺 started_at/ended_at → 回执推导为空串而非报错。"""
    sr = _step(verdict="passed", assertions=(True,))
    rec = ResultRecord.from_step(sr)
    payload = dict(rec.to_dict())
    payload.pop("started_at", None)
    payload.pop("ended_at", None)
    restored = ResultRecord.from_dict(payload)
    assert restored.started_at == ""
    assert restored.ended_at == ""


# ---------- 字段三：证据在哪 ----------

def test_evidence_passthrough_and_execution_id():
    """证据地址原样带进回执；execution_id 缺省按结束时间生成。"""
    results = [_step(verdict="passed", assertions=(True,), ended="2026-06-01T09:00:01")]
    receipt = build_verification_receipt(
        results,
        environment="staging",
        commit_sha="abcd1234",
        evidence={"report_md": "/tmp/x.md", "report_json": "/tmp/x.json"},
    )
    assert receipt["environment"] == "staging"
    assert receipt["commit"] == "abcd1234"
    assert receipt["evidence"]["report_md"] == "/tmp/x.md"
    assert "2026-06-01T09:00:01" in receipt["execution_id"]
    assert receipt["receipt_schema"] == "verification-receipt/1"


# ---------- 序列化：可直接被 Agent json 消费 ----------

def test_receipt_is_json_serializable():
    """回执断言可直接 json.dumps（Agent 一条命令即可消费）。"""
    results = [
        _step(
            verdict="passed", assertions=(True,),
            started="2026-06-01T09:00:00", ended="2026-06-01T09:00:01",
        )
    ]
    receipt = build_verification_receipt(results)
    text = json.dumps(receipt, ensure_ascii=False)
    assert json.loads(text)["verdict"] == "PASS"


# ---------- 鸭子类型：ResultRecord 也走同一判定 ----------

def test_receipt_consumes_result_record_instances():
    """回执同时接受 ResultRecord（历史/报告路径）输入，判定一致。"""
    rec = ResultRecord.from_step(_step(verdict="passed", assertions=(True,)))
    receipt = build_verification_receipt([rec])
    assert receipt["verdict"] == "PASS"
    assert receipt["assertions"] == {"total": 1, "passed": 1, "failed": 0}
# ---------- CLI 集成：ah run --json 产出回执 ----------

def test_cmd_run_json_emits_receipt_with_evidence(tmp_path, capsys, monkeypatch):
    """``ah run <project> --json`` 输出三字段契约回执，含可读回的证据地址。"""
    import argparse

    import yaml

    from ah import evolution
    from ah.core import config

    # 1) 造一个最小业务项目：一条带状态码断言的绿色用例。
    data = tmp_path / "data"
    data.mkdir()
    (data / "ok.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "name": "ok",
                "env": "all",
                "suite": "smoke",
                "request": {"method": "GET", "path": "/health"},
                "assert": {"status_code": 200},
            }
        ),
        encoding="utf-8",
    )
    # 2) 钉住隔离传输 + 报告目录，避免写真实报告路径与外部网络。
    monkeypatch.setenv("AH_LOCAL_OBS", "0")
    monkeypatch.setattr(config.settings, "report_dir", str(tmp_path / "reports"))
    monkeypatch.setattr(evolution, "_hermetic_client", lambda trace=None: _isolated_client())

    code = evolution._cmd_run(
        argparse.Namespace(
            project=str(tmp_path), env="all", suite="smoke",
            priority=None, tags=None, json=True, commit="deadbeef", basename="cli-receipt",
        )
    )
    assert code == 0
    out = capsys.readouterr().out
    # 3) 从 stdout 里剥离出回执 JSON（回执是最后一段确定性的 JSON 文档）。
    receipt = json.loads(out[out.index("{") :])
    assert receipt["verdict"] == "PASS"
    assert receipt["commit"] == "deadbeef"
    assert receipt["environment"] == "all"
    assert receipt["steps"]["total"] == 1
    assert receipt["assertions"]["total"] == 1
    assert receipt["started_at"]
    assert receipt["ended_at"]
    # 证据地址真实存在，Agent 可直接读回。
    md = receipt["evidence"]["report_md"]
    assert md  # 路径非空
    assert tmp_path.as_posix() in md  # 落在钉住的报告目录下


def _isolated_client():
    """构造一个确定性隔离 HTTP 客户端（零网络）。"""
    import httpx as _httpx

    from ah.harness.http import HTTPClient

    transport = _httpx.MockTransport(lambda _request: _httpx.Response(200, json={"code": 0}))
    raw = _httpx.Client(transport=transport)
    return HTTPClient("http://hermetic.local", client=raw)
