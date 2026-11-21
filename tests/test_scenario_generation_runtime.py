#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/28 15:45
# @Filename : test_scenario_generation_runtime.py
# @Author   : Alan_Hsu
"""生成产物在合成状态桩上换 ID/换列表顺序执行；不访问真实 API。"""

from __future__ import annotations

import json

import httpx
import pytest

from ah.core.orchestrator import run_flow
from ah.harness.http import HTTPClient
from ah.harness.loader import load_cases
from ah.scenario.compile import write_scenario
from ah.scenario.validate import validate_model
from ah.utils.context import Context
from tests.test_scenario_generation import confirm, entry, model_for


def execute(model, tmp_path, handler):
    path = tmp_path / "synthetic-chain.yaml"
    write_scenario(model, path)
    ctx = Context({name: "synthetic-external-input" for name in model["inputs"]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as raw:
        return run_flow(load_cases(path), ctx, HTTPClient("https://synthetic.invalid", client=raw))


@pytest.mark.parametrize(
    "user_id,item_id", [("new-user-a", "new-item-a"), ("new-user-b", "new-item-b")]
)
def test_multi_upstream_consumes_changed_ids(user_id, item_id, tmp_path):
    model = confirm(
        model_for(
            [
                entry("/synthetic/users", {"id": "old-user"}),
                entry("/synthetic/items", {"id": "old-item"}, second=1),
                entry(
                    "/synthetic/orders",
                    {"id": "old-order"},
                    {"buyer": "old-user", "item": "old-item"},
                    second=2,
                ),
            ]
        )
    )
    state = {}

    def handler(request):
        if request.url.path == "/synthetic/users":
            state["user"] = user_id
            return httpx.Response(200, json={"id": user_id})
        if request.url.path == "/synthetic/items":
            state["item"] = item_id
            return httpx.Response(200, json={"id": item_id})
        assert json.loads(request.content) == {"buyer": state["user"], "item": state["item"]}
        return httpx.Response(200, json={"id": "new-order"})

    assert all(result.succeeded for result in execute(model, tmp_path, handler))


@pytest.mark.parametrize("reverse", [False, True])
def test_uuid_business_key_survives_list_reordering(reverse, tmp_path):
    original = "1d90afc3-1234-4234-a234-123456789abc"
    changed = "2d90afc3-5678-4234-a234-123456789abc"
    model = confirm(
        model_for(
            [
                entry("/synthetic/items", {"items": [{"sku": "wanted", "id": original}]}),
                entry("/synthetic/orders", {}, {"item": original}, second=1),
            ]
        )
    )
    model["edges"][0]["source"]["selector"] = 'items[?sku == `"wanted"`].id | [0]'
    model["edges"][0]["selection_confirmed"] = True
    items = [{"sku": "other", "id": "must-not-use"}, {"sku": "wanted", "id": changed}]
    if reverse:
        items.reverse()

    def handler(request):
        if request.url.path == "/synthetic/items":
            return httpx.Response(200, json={"items": items})
        assert json.loads(request.content)["item"] == changed
        return httpx.Response(200, json={})

    assert all(result.succeeded for result in execute(model, tmp_path, handler))


def test_negative_chain_rejects_expected_denial_and_blocks_failed_upstream(tmp_path):
    model = confirm(
        model_for(
            [
                entry("/synthetic/users", {"id": "old-user"}),
                entry("/synthetic/reject", {}, {"buyer": "old-user"}, second=1, status=403),
            ]
        )
    )
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/synthetic/users":
            return httpx.Response(500, json={"id": "must-not-publish"})
        raise AssertionError("失败上游不得发出下游请求")

    results = execute(model, tmp_path, handler)
    assert set(calls) == {"/synthetic/users"}
    assert results[0].verdict == "failed"
    assert results[0].extracted == {}
    assert results[1].status == "blocked"
    assert results[1].attempts == 0


def test_negative_chain_accepts_explicit_http_denial_without_business_claim(tmp_path):
    model = confirm(
        model_for(
            [
                entry("/synthetic/users", {"id": "old-user"}),
                entry("/synthetic/reject", {}, {"buyer": "old-user"}, second=1, status=403),
            ]
        )
    )

    def handler(request):
        if request.url.path == "/synthetic/users":
            return httpx.Response(200, json={"id": "new-user"})
        assert json.loads(request.content)["buyer"] == "new-user"
        return httpx.Response(403, json={"error": "synthetic forbidden"})

    assert all(result.succeeded for result in execute(model, tmp_path, handler))
    assert any(warning["code"] == "transport_only" for warning in validate_model(model)["warnings"])
