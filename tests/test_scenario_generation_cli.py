#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/28 15:50
# @Filename : test_scenario_generation_cli.py
# @Author   : Alan_Hsu
"""CLI 排他写入、重复会话补证及可选语义助手的离线边界。"""

from __future__ import annotations

import argparse
import copy
import json

import pytest

from ah.llm.base import BaseLLM
from ah.scenario.cli import register
from ah.scenario.dependencies import discover
from ah.scenario.modeler import suggest_operations
from ah.scenario.validate import validate_model
from tests.test_scenario_generation import catalog, confirm, entry, model_for


def command(*args):
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers())
    parsed = parser.parse_args(args)
    return parsed.func(parsed)


def test_cli_validate_compile_is_exclusive(tmp_path, capsys):
    model = model_for(
        [
            entry("/synthetic/users", {"id": "old"}),
            entry("/synthetic/orders", {}, {"buyer": "old"}, second=1),
        ]
    )
    source = tmp_path / "relationships.json"
    source.write_text(json.dumps(model), encoding="utf-8")
    assert command("scenario", "validate", str(source)) == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False
    source.write_text(json.dumps(confirm(model)), encoding="utf-8")
    assert command("scenario", "validate", str(source)) == 0
    capsys.readouterr()
    output = tmp_path / "chain.yaml"
    notes = tmp_path / "modeling_notes.md"
    assert (
        command("scenario", "compile", str(source), "--output", str(output), "--notes", str(notes))
        == 0
    )
    original = output.read_text()
    assert command("scenario", "compile", str(source), "--output", str(output)) == 1
    assert output.read_text() == original
    assert "业务终态" in notes.read_text()


def test_discover_forbids_data_and_existing_output(tmp_path, monkeypatch):
    # 本地假目录只是已知合成接口目录，不调用真实网络或模型。
    monkeypatch.setattr("ah.scenario.cli.load_catalog", lambda *a, **kw: catalog())
    capture = tmp_path / "capture.har"
    capture.write_text(json.dumps({"log": {"entries": [entry("/synthetic/users", {"id": "old"})]}}))
    output = tmp_path / "drafts"
    args = (
        "scenario",
        "discover",
        "--openapi",
        "synthetic.json",
        "--har",
        str(capture),
        "--output-dir",
        str(output),
    )
    assert command(*args) == 0
    assert {item.name for item in output.iterdir()} == {
        "relationships.json",
        "validation.json",
        "modeling_notes.md",
    }
    assert command(*args) == 1
    assert (
        command(
            "scenario",
            "discover",
            "--openapi",
            "synthetic.json",
            "--output-dir",
            str(tmp_path / "data" / "drafts"),
        )
        == 1
    )
    assert not (tmp_path / "data").exists()


def test_repeated_captures_add_evidence_not_duplicate_steps():
    def har(user):
        return {
            "log": {
                "entries": [
                    entry("/synthetic/users", {"id": user}),
                    entry("/synthetic/orders", {}, {"buyer": user}, second=1),
                ]
            }
        }

    model = discover(catalog(), [har("old"), har("new")])
    assert len(model["steps"]) == 2 and len(model["edges"]) == 1
    assert len(model["edges"][0]["evidence"]) == 2
    assert model["edges"][0]["status"] == "proposed"


def test_catalog_artifact_scrubs_secret_examples_and_response_headers():
    source = catalog()
    op = source["operations"][0]
    op["request_schema"] = {
        "type": "object",
        "properties": {
            "password": {"type": "string", "example": "secret-example", "default": "secret-default"}
        },
    }
    op["responses"]["200"]["headers"] = {"Authorization": {"example": "Bearer secret-header"}}
    op["responses"]["200"]["example"] = {"token": "secret-response"}
    snapshot = json.dumps(discover(source))
    assert not any(
        secret in snapshot
        for secret in ("secret-example", "secret-default", "secret-header", "secret-response")
    )


class SemanticHelper(BaseLLM):
    def __init__(self, response):
        super().__init__()
        self.response = response
        self.prompts = []

    def generate(self, task, prompt, *, model="cheap"):
        raise AssertionError("语义助手不走自由文本生成")

    def judge(self, task, prompt, *, model="strong"):
        self.prompts.append(prompt)
        return self.response


def test_llmoff_never_calls_injected_backend(monkeypatch):
    monkeypatch.setenv("LLMOFF", "1")
    helper = SemanticHelper({})
    result = suggest_operations(catalog(), llm=helper)
    assert result["available"] is False and not helper.prompts


def test_semantic_model_is_constrained_to_catalog_and_no_capture(monkeypatch):
    monkeypatch.delenv("LLMOFF", raising=False)
    source = catalog()
    helper = SemanticHelper({"candidates": [{"operation": source["operations"][0]["key"]}]})
    assert suggest_operations(source, llm=helper)["available"]
    assert all(word not in helper.prompts[0] for word in ("Authorization", "Cookie", "password"))
    helper.response = {"candidates": [{"operation": "invented endpoint"}]}
    assert suggest_operations(source, llm=helper)["candidates"] == []


@pytest.mark.parametrize("model", [None, [], {}, {"model_version": 999}])
def test_malformed_models_return_json_diagnostics(model):
    result = validate_model(copy.deepcopy(model))
    assert not result["valid"] and result["errors"][0]["code"] == "model_schema"
