#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2026-10-08 21:25
# @Filename: test_telemetry.py
# @Author  : Alan_Hsu
"""Langfuse 成本聚合（浮点求和/计数/坏行隔离/LLM off 空看板）。

纯本地文件 IO，绝不触碰网络。
"""
import argparse
import json
from unittest import mock

import pytest

import ah.telemetry as tel
from ah.telemetry import (
    _validate_fields,
    aggregate_llm_stats,
    default_ledger_path,
    load_ledger,
    record_llm_call,
    run,
    summarize,
)


def test_default_ledger_path_falls_back_to_settings(tmp_path, monkeypatch):
    from ah.core import config

    monkeypatch.delenv("AH_REPORT_DIR", raising=False)
    monkeypatch.setattr(config.settings, "report_dir", str(tmp_path))
    assert default_ledger_path() == tmp_path / "llm_usage.jsonl"
    assert default_ledger_path(str(tmp_path)) == tmp_path / "llm_usage.jsonl"


def test_default_ledger_path_prefers_live_ah_report_dir(tmp_path, monkeypatch):
    """写读同源：运行期 AH_REPORT_DIR 优先于 settings.report_dir。"""
    from ah.core import config

    monkeypatch.setattr(config.settings, "report_dir", str(tmp_path / "ignored"))
    monkeypatch.setenv("AH_REPORT_DIR", str(tmp_path / "project-report"))
    assert default_ledger_path() == tmp_path / "project-report" / "llm_usage.jsonl"
    assert default_ledger_path(str(tmp_path)) == tmp_path / "llm_usage.jsonl"


def test_record_then_aggregate_mixed(tmp_path):
    """写入成功/错误混合记录 → 计数与成本正确聚合。"""
    record_llm_call(model="gpt-4o", status="success", cost=0.01, report_dir=tmp_path)
    record_llm_call(model="gpt-4o", status="success", cost=0.02, report_dir=tmp_path)
    record_llm_call(model="gpt-4o-mini", status="error", cost=0.005, report_dir=tmp_path)
    records = load_ledger(tmp_path / "llm_usage.jsonl")
    stats = aggregate_llm_stats(records)
    assert stats["requests"] == 3
    assert stats["success"] == 2
    assert stats["errors"] == 1
    assert stats["cost"] == pytest.approx(0.035)
    assert stats["success_rate"] == pytest.approx(0.6667)
    assert stats["by_model"]["gpt-4o"]["requests"] == 2
    assert stats["by_model"]["gpt-4o"]["success"] == 2
    assert stats["by_model"]["gpt-4o"]["errors"] == 0
    assert stats["by_model"]["gpt-4o-mini"]["errors"] == 1



def test_aggregate_tokens_summed_per_model_and_total(tmp_path):
    """账本带 prompt/completion_tokens → 逐条相加并落到 by_model 与全局合计。"""
    ledger = tmp_path / "llm_usage.jsonl"
    ledger.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"model": "m1", "status": "success", "cost": 0.01,
                 "prompt_tokens": 100, "completion_tokens": 50},
                {"model": "m1", "status": "success", "cost": 0.02,
                 "prompt_tokens": 40, "completion_tokens": 10},
                {"model": "m2", "status": "error", "cost": 0.005,
                 "prompt_tokens": 20, "completion_tokens": 5},
                {"model": "m3", "status": "success", "cost": 0.01},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    stats = aggregate_llm_stats(load_ledger(ledger))
    # 全局：m1=150+50=200，m2=25，m3 无 token 记 0。
    assert stats["prompt_tokens"] == 160
    assert stats["completion_tokens"] == 65
    assert stats["total_tokens"] == 225
    assert stats["by_model"]["m1"]["prompt_tokens"] == 140
    assert stats["by_model"]["m1"]["completion_tokens"] == 60
    assert stats["by_model"]["m1"]["total_tokens"] == 200
    assert stats["by_model"]["m2"]["total_tokens"] == 25
    assert stats["by_model"]["m3"]["total_tokens"] == 0


def test_missing_ledger_is_zero(tmp_path):
    """账本不存在（如 LLMOFF=1 下从未写入）→ 聚合 0/空。"""
    records = load_ledger(tmp_path / "llm_usage.jsonl")
    assert records == []
    stats = aggregate_llm_stats(records)
    assert stats["requests"] == 0
    assert stats["success"] == 0
    assert stats["errors"] == 0
    assert stats["cost"] == 0.0
    assert stats["success_rate"] == 0.0
    assert stats["by_model"] == {}


def test_bad_ledger_lines_skipped(tmp_path):
    """账本含坏行（非 JSON）→ 跳过不崩溃，有效行仍聚合。"""
    ledger = tmp_path / "llm_usage.jsonl"
    ledger.write_text(
        "not-json-line\n"
        + json.dumps({"model": "m1", "status": "error", "cost": 1.0})
        + "\n"
        '{"model": "x"}\n',
        encoding="utf-8",
    )
    records = load_ledger(ledger)
    assert len(records) == 2
    stats = aggregate_llm_stats(records)
    assert stats["requests"] == 2
    assert stats["errors"] == 1
    assert stats["cost"] == pytest.approx(1.0)


def test_record_llm_call_creates_dir(tmp_path):
    """record 若账本目录不存在会自动建目录。"""
    sub = tmp_path / "a" / "b"
    p = record_llm_call(model="m", status="success", cost=0.1, report_dir=sub)
    assert p.exists()
    assert sub.is_dir()


def test_record_llm_call_never_raises_on_dir_error(monkeypatch, tmp_path):
    """写入失败（OSError）→ 不抛出，只 log 告警。"""

    def _boom(*a, **k):
        raise OSError("read-only dir")

    monkeypatch.setattr("ah.telemetry.Path.open", _boom)
    record_llm_call(model="m", report_dir=tmp_path)  # 不应 raise


def test_run_empty_ledger_prints_zero_and_returns_zero(tmp_path, capsys, monkeypatch):
    """LLM off（空账本）→ run 打印空看板并返回 0。"""
    from ah.core import config
    from ah.llm import openai

    monkeypatch.setattr(config.settings, "report_dir", str(tmp_path))
    monkeypatch.setattr(openai, "llm_enabled", lambda: False)
    args = argparse.Namespace(
        project="demo", report_dir=str(tmp_path), fields=None, write_json=False
    )
    assert run(args) == 0
    out = capsys.readouterr().out
    assert "[telemetry] 成本: $0.000000" in out
    assert "requests: 0" in out


def test_run_write_json_report(tmp_path):
    """--json 会把聚合结果写一份 llm_telemetry.json（报告段）。"""
    record_llm_call(model="m", status="success", cost=0.5, report_dir=tmp_path)
    with mock.patch.object(tel.Path, "cwd", return_value=tmp_path):
        args = argparse.Namespace(
            project="demo", report_dir=str(tmp_path), fields=None, write_json=True
        )
        assert run(args) == 0
        data = json.loads((tmp_path / "llm_telemetry.json").read_text())
        assert data["requests"] == 1
        assert data["success"] == 1
        assert data["cost"] == pytest.approx(0.5)


def test_summarize_fields_configurable():
    """summarize 可按 fields 子集输出，不出现未选字段。"""
    stats = {
        "requests": 5,
        "success": 4,
        "errors": 1,
        "cost": 0.4,
        "success_rate": 0.8,
        "by_model": {},
        "llm_off": True,
    }
    text = summarize(stats, fields=["cost", "requests"])
    assert "成本: $0.400000" in text
    assert "requests: 5" in text
    assert "成功率" not in text


def test_validate_fields_unknown_dropped():
    assert _validate_fields(None) == [
        "cost",
        "requests",
        "success",
        "errors",
        "success_rate",
    ]
    assert _validate_fields("cost,unknown,success") == ["cost", "success"]
    assert _validate_fields(" , ") == []


class _FakeCompletion:
    """模拟 OpenAI 响应：choices[0].message.content + usage。"""

    def __init__(self, text: str, prompt_tokens: int, completion_tokens: int):
        self.choices = [
            type(
                "C",
                (),
                {"message": type("M", (), {"content": text})()},
            )()
        ]
        self.usage = type(
            "U",
            (),
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        )()


class _FakeClient:
    """模拟 OpenAI 客户端：chat.completions.create 返回固定响应。"""

    def __init__(self):
        self.calls = 0

    @property
    def chat(self) -> "_FakeClient":
        return self

    @property
    def completions(self) -> "_FakeClient":
        return self

    def create(self, **kwargs):
        self.calls += 1
        return _FakeCompletion("ok", 10, 5)


def test_llmclient_generate_writes_ledger(tmp_path, monkeypatch):
    """LLMClient.generate 真实调用后写 llm_usage.jsonl（遥测落账数据源）。"""
    import ah.llm.openai as _oa

    monkeypatch.setattr(_oa, "llm_enabled", lambda: True)
    monkeypatch.setattr(_oa.settings.llm, "api_key", "test-key")
    monkeypatch.setattr(_oa.settings.llm, "model_cheap", "gpt-4o-mini")
    monkeypatch.delenv("AH_REPORT_DIR", raising=False)
    monkeypatch.setattr(
        _oa.settings, "report_dir", str(tmp_path / "reports")
    )

    fake = _FakeClient()
    client = _oa.LLMClient(extra_client=fake)
    out = client.generate("gen-token", "请生成用户 token")

    assert out == "ok" and fake.calls == 1
    ledger = tmp_path / "reports" / "llm_usage.jsonl"
    assert ledger.is_file()
    rec = json.loads(ledger.read_text().strip().splitlines()[-1])
    assert rec["model"] == "gpt-4o-mini"
    assert rec["task"] == "gen-token"
    assert rec["status"] == "success"
    assert rec["prompt_tokens"] == 10 and rec["completion_tokens"] == 5
