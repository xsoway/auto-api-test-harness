#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/28 16:30
# @Filename : test_scenario_safety.py
# @Author   : Alan_Hsu
"""生成门禁的隐私、作用域、类型及稳定来源边界。"""

from __future__ import annotations

import json

import pytest

from ah.scenario.compile import compile_model
from ah.scenario.dependencies import discover
from ah.scenario.validate import validate_model
from tests.test_scenario_generation import catalog, confirm, entry, model_for


def test_prior_response_credentials_are_externalized_in_later_alias_fields():
    secret = "prior-response-credential-canary"
    model = model_for(
        [
            entry("/synthetic/users", {"token": secret}),
            entry("/synthetic/orders", {}, {"buyer": secret}, second=1),
        ]
    )
    assert secret not in json.dumps(model)
    reference = model["steps"][1]["request"]["body"]["buyer"]
    assert reference.startswith("${input_")
    assert model["inputs"][reference[2:-1]]["sensitive"] is True


def test_api_key_changes_create_distinct_authentication_scopes():
    captures = [
        entry("/synthetic/users", {"id": "shared-id"}),
        entry("/synthetic/orders", {}, {"buyer": "shared-id"}, second=1),
    ]
    for capture, principal in zip(captures, ["principal-a", "principal-b"]):
        capture["request"]["headers"] = [{"name": "X-API-Key", "value": principal}]
        capture["request"]["cookies"] = []
    model = model_for(captures)
    assert model["steps"][0]["scope"]["auth"] != model["steps"][1]["scope"]["auth"]
    assert model["edges"] == []


def test_schema_snapshot_keeps_property_names_but_drops_vendor_credentials():
    source = catalog()
    operation = next(op for op in source["operations"] if op["path"] == "/synthetic/orders")
    names = ["description", "enum", "examples", "token"]
    operation["request_schema"] = {
        "type": "object",
        "required": names,
        "properties": {name: {"type": "string"} for name in names},
        "x-api-key": "schema-vendor-credential-canary",
    }
    captured = entry(
        "/synthetic/orders",
        {},
        {
            "description": "hello",
            "enum": "ordinary-field",
            "examples": "ordinary-example",
            "token": "captured-token-canary",
        },
    )
    model = discover(source, [{"log": {"entries": [captured]}}])
    assert "schema-vendor-credential-canary" not in json.dumps(model)
    public = next(op for op in model["catalog"]["operations"] if op["path"] == "/synthetic/orders")
    assert set(public["request_schema"]["properties"]) == set(names)
    assert validate_model(model)["valid"]
    assert compile_model(model)["scenario"][0]["request"]["json"]["description"] == "hello"


def test_confirmed_inner_filter_does_not_authorize_positional_ancestors():
    model = confirm(
        model_for(
            [
                entry(
                    "/synthetic/items",
                    {
                        "groups": [
                            {"items": [{"sku": "WANTED", "id": "group-a"}]},
                            {"items": [{"sku": "WANTED", "id": "group-b"}]},
                        ]
                    },
                ),
                entry("/synthetic/orders", {}, {"item": "group-a"}, second=1),
            ]
        )
    )
    model["edges"][0]["source"]["selector"] = 'groups[0].items[?sku == `"WANTED"`].id | [0]'
    model["edges"][0]["selection_confirmed"] = True
    assert not validate_model(model)["valid"]
    with pytest.raises(ValueError):
        compile_model(model)


@pytest.mark.parametrize("kind", ["interpolation", "producer-reference"])
def test_request_references_cannot_bypass_declared_field_types(kind):
    if kind == "interpolation":
        model = model_for([entry("/synthetic/orders", {}, {"buyer": 1})])
        step = model["steps"][0]
        model["inputs"]["number"] = {"type": "integer", "scope": step["scope"]}
        step["request"]["body"]["buyer"] = "${number}-suffix"
        field = "buyer"
    else:
        model = confirm(
            model_for(
                [
                    entry("/synthetic/users", {"id": "buyer-a"}),
                    entry("/synthetic/orders", {}, {"buyer": "buyer-a"}, second=1),
                ]
            )
        )
        model["edges"][0]["variable"] = "producer_id"
        model["steps"][1]["request"]["body"]["item"] = "${producer_id}"
        field = "item"
    operation = next(
        op for op in model["catalog"]["operations"] if op["path"] == "/synthetic/orders"
    )
    operation["request_schema"]["properties"][field]["type"] = "integer"
    assert not validate_model(model)["valid"]
    with pytest.raises(ValueError):
        compile_model(model)


def test_array_pointer_alias_cannot_overwrite_another_confirmed_binding():
    model = confirm(
        model_for(
            [
                entry("/synthetic/users", {"id": "same"}),
                entry("/synthetic/items", {"id": "same"}, second=1),
                entry("/synthetic/orders", {}, {"buyer": "same"}, second=2),
            ]
        )
    )
    model["steps"][2]["request"]["body"] = {"items": [{"owner": "same"}]}
    operation = next(
        op for op in model["catalog"]["operations"] if op["path"] == "/synthetic/orders"
    )
    operation["request_schema"] = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"owner": {"type": "string"}}},
            }
        },
    }
    assert len(model["edges"]) == 2
    for edge, pointer in zip(model["edges"], ["/items/0/owner", "/items/00/owner"]):
        edge["target"]["pointer"] = pointer
    assert not validate_model(model)["valid"]
    with pytest.raises(ValueError):
        compile_model(model)


def test_header_type_evidence_cannot_be_relabelled_as_body_evidence():
    producer = entry("/synthetic/users", {})
    producer["response"]["headers"] = [{"name": "id", "value": "header-id"}]
    model = confirm(
        model_for(
            [
                producer,
                entry("/synthetic/orders", {}, {"buyer": "header-id"}, second=1),
            ]
        )
    )
    operation = next(
        op for op in model["catalog"]["operations"] if op["path"] == "/synthetic/users"
    )
    for response in operation["responses"].values():
        response["schema"] = {"type": "object", "properties": {}}
    edge = model["edges"][0]
    edge["source"].update(location="response.body", selector="id")
    assert not validate_model(model)["valid"]
    with pytest.raises(ValueError):
        compile_model(model)


@pytest.mark.parametrize("mutation", ["different-field", "conflicting-oracle"])
def test_business_selection_preserves_source_identity_and_manual_oracle(mutation):
    model = confirm(
        model_for(
            [
                entry(
                    "/synthetic/items",
                    {"items": [{"sku": "WANTED", "id": "correct-id", "other": 42}]},
                ),
                entry("/synthetic/orders", {}, {"item": "correct-id"}, second=1),
            ]
        )
    )
    selector = 'items[?sku == `"WANTED"`].id | [0]'
    edge = model["edges"][0]
    edge["source"]["selector"] = selector
    edge["selection_confirmed"] = True
    if mutation == "different-field":
        edge["source"]["selector"] = selector.replace(".id", ".other")
    else:
        model["steps"][0]["assertions"]["jsonpath"] = {
            'length(items[?sku == `"WANTED"`].id)': {"eq": 2},
        }
    assert not validate_model(model)["valid"]
    with pytest.raises(ValueError):
        compile_model(model)
