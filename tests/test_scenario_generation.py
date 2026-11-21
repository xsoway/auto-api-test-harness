#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/28 15:35
# @Filename : test_scenario_generation.py
# @Author   : Alan_Hsu
"""仅合成接口的离线生成验收，不宣称真实业务正确。"""

from __future__ import annotations

import copy
import json

import pytest

from ah.scenario.capture import normalize_har, typed_key
from ah.scenario.catalog import normalize_catalog
from ah.scenario.compile import compile_model, write_scenario
from ah.scenario.dependencies import discover
from ah.scenario.validate import validate_model


def catalog():
    def op(method, path, body=None):
        value = {
            "operationId": method + path.replace("/", "_"),
            "responses": {
                "200": {
                    "description": "合成响应",
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "items": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "id": {"type": "string"},
                                                "sku": {"type": "string"},
                                            },
                                        },
                                    },
                                },
                            }
                        }
                    },
                }
            },
        }
        if body:
            value["requestBody"] = {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {k: {"type": t} for k, t in body.items()},
                        }
                    }
                }
            }
        return value

    return normalize_catalog(
        {
            "openapi": "3.0.3",
            "paths": {
                "/synthetic/users": {"post": op("post", "/users")},
                "/synthetic/items": {"get": op("get", "/items")},
                "/synthetic/orders": {
                    "post": op("post", "/orders", {"buyer": "string", "item": "string"})
                },
                "/synthetic/reject": {"post": op("post", "/reject", {"buyer": "string"})},
            },
        }
    )


def entry(path, response, body=None, second=0, status=200, tenant="tenant-a"):
    return {
        "pageref": "session-a",
        "_tenant": tenant,
        "startedDateTime": f"2026-10-28T00:00:{second:02d}Z",
        "time": 10,
        "request": {
            "method": "POST" if path != "/synthetic/items" else "GET",
            "url": "https://synthetic.invalid" + path,
            "headers": [{"name": "Authorization", "value": "Bearer secret-canary"}],
            "cookies": [{"name": "session", "value": "cookie-canary"}],
            "postData": {
                "mimeType": "application/json",
                "text": json.dumps(body if body is not None else {}),
            },
        },
        "response": {
            "status": status,
            "content": {"mimeType": "application/json", "text": json.dumps(response)},
        },
    }


def model_for(entries):
    return discover(catalog(), [{"log": {"entries": entries}}], name="合成链")


def confirm(model):
    for edge in model["edges"]:
        edge["status"] = "confirmed"
    return model


def test_multi_upstream_keeps_native_values_and_requires_confirmation(tmp_path):
    model = model_for(
        [
            entry("/synthetic/users", {"id": "user-new"}),
            entry("/synthetic/items", {"id": "item-new"}, second=1),
            entry(
                "/synthetic/orders",
                {"id": "order-new"},
                {"buyer": "user-new", "item": "item-new"},
                second=2,
            ),
        ]
    )
    assert len(model["edges"]) == 2
    assert not validate_model(model)["valid"]
    result = compile_model(confirm(model))
    assert result["schema_version"] == 2
    assert result["scenario"][2]["depends_on"] == ["step-1", "step-2"]
    assert result["scenario"][2]["request"]["json"]["buyer"].startswith("${")
    output = tmp_path / "chain.yaml"
    write_scenario(model, output)
    from ah.harness.loader import load_cases

    assert len(load_cases(output)) == 3
    with pytest.raises(FileExistsError):
        write_scenario(model, output)
    serialized = json.dumps(model)
    assert "secret-canary" not in serialized and "cookie-canary" not in serialized
    assert all(edge["evidence"][0]["capture_hash"] for edge in model["edges"])


def test_uuid_nested_list_needs_business_key_selector():
    uuid = "1d90afc3-1234-4234-a234-123456789abc"
    model = model_for(
        [
            entry("/synthetic/items", {"items": [{"sku": "WANTED", "id": uuid}]}),
            entry("/synthetic/orders", {}, {"item": uuid}, second=1),
        ]
    )
    confirm(model)
    assert not validate_model(model)["valid"]
    edge = model["edges"][0]
    edge["source"]["selector"] = 'items[?sku == `"WANTED"`].id | [0]'
    edge["selection_confirmed"] = True
    assert validate_model(model)["valid"]
    assert compile_model(model)["scenario"][0]["extract"]


def test_negative_expected_response_is_not_business_oracle():
    model = model_for(
        [
            entry("/synthetic/users", {"id": "user-new"}),
            entry(
                "/synthetic/reject",
                {"error": "synthetic denial"},
                {"buyer": "user-new"},
                second=1,
                status=403,
            ),
        ]
    )
    confirm(model)
    assert compile_model(model)["scenario"][1]["assert"] == {"status_code": 403}
    model["steps"][1]["assertions"] = {}
    assert not validate_model(model)["valid"]


def test_multiple_sources_small_ids_preserve_ambiguity():
    model = model_for(
        [
            entry("/synthetic/users", {"id": "1"}),
            entry("/synthetic/items", {"id": "1"}, second=1),
            entry("/synthetic/orders", {}, {"buyer": "1"}, second=2),
        ]
    )
    assert len(model["edges"]) == 2
    confirm(model)
    assert not validate_model(model)["valid"]
    model["edges"][1]["status"] = "rejected"
    assert validate_model(model)["valid"]


@pytest.mark.parametrize("mutation", ["forward", "scope", "orphan", "cookie", "unknown", "type"])
def test_invalid_models_never_compile(mutation):
    model = confirm(
        model_for(
            [
                entry("/synthetic/users", {"id": "a"}),
                entry("/synthetic/orders", {}, {"buyer": "a"}, second=1),
            ]
        )
    )
    if mutation == "forward":
        model["steps"].reverse()
    elif mutation == "scope":
        model["steps"][1]["scope"]["tenant"] = "different"
    elif mutation == "orphan":
        model["steps"][1]["request"]["body"]["unknown"] = "${orphan}"
    elif mutation == "cookie":
        model["edges"][0]["target"]["location"] = "request.cookie"
    elif mutation == "unknown":
        model["steps"][1]["operation"] = "made-up"
    else:
        model["edges"][0]["target"]["value_type"] = "integer"
    assert not validate_model(model)["valid"]
    with pytest.raises(ValueError):
        compile_model(model)


def test_typed_keys_preserve_large_integer_and_boolean_distinction():
    assert typed_key(9007199254740993) != typed_key(9007199254740992)
    assert typed_key(True) != typed_key(1)
    assert typed_key("1") != typed_key(1)


def test_scope_and_response_completion_filtering():
    entries = [
        entry("/synthetic/users", {"id": "a"}),
        entry("/synthetic/orders", {}, {"buyer": "a"}, second=1, tenant="tenant-b"),
    ]
    assert not model_for(entries)["edges"]
    entries[1]["_tenant"] = "tenant-a"
    entries[0]["time"] = 5000
    assert not model_for(entries)["edges"]


def test_no_boolean_auto_confirmation_and_auth_redaction():
    model = model_for(
        [
            entry("/synthetic/users", {"flag": True, "password": "sensitive"}),
            entry("/synthetic/orders", {}, {"flag": True, "password": "sensitive"}, second=1),
        ]
    )
    assert all(edge["status"] == "proposed" for edge in model["edges"])
    assert "sensitive" not in json.dumps(model).replace('"sensitive": true', "")
    assert all("value" not in field for step in model["steps"] for field in step["response_fields"])


def test_normalized_har_never_exposes_credentials():
    result = normalize_har(
        {"log": {"entries": [entry("/synthetic/users", {"token": "token-canary"})]}}, catalog()
    )
    assert "secret-canary" not in json.dumps(result)
    assert "cookie-canary" not in json.dumps(result)
    assert "token-canary" not in json.dumps(result)


def test_catalog_unsupported_and_missing_assertion_block():
    model = confirm(model_for([entry("/synthetic/users", {"id": "a"})]))
    model["catalog"]["issues"].append({"code": "unsupported", "message": "合成不支持结构"})
    assert not validate_model(model)["valid"]
    other = copy.deepcopy(model)
    other["steps"][0]["assertions"] = {}
    assert not validate_model(other)["valid"]


def test_declared_openapi_link_remains_proposed_until_review():
    source = catalog()
    producer, consumer = source["operations"][:2]
    consumer["parameters"] = [
        {"name": "owner", "in": "query", "required": True, "schema": {"type": "string"}}
    ]
    producer["links"] = {
        "200": {
            "ownerLookup": {
                "operationId": consumer["operation_id"],
                "parameters": {"owner": "$response.body#/id"},
            }
        }
    }
    model = discover(source)
    assert len(model["edges"]) == 1
    edge = model["edges"][0]
    assert edge["status"] == "proposed" and edge["evidence"][0]["kind"] == "declared"
    assert validate_model(confirm(model))["valid"]
    result = compile_model(model)
    assert result["scenario"][1]["request"]["params"]["owner"].startswith("${dep_")


def test_nested_list_target_pointer_and_large_integer_stay_typed():
    source = catalog()
    source["operations"][0]["responses"]["200"]["schema"]["properties"]["id"] = {"type": "integer"}
    order = next(op for op in source["operations"] if op["path"] == "/synthetic/orders")
    order["request_schema"] = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"owner": {"type": "integer"}}},
            }
        },
    }
    model = discover(
        source,
        [
            {
                "log": {
                    "entries": [
                        entry("/synthetic/users", {"id": 9007199254740993}),
                        entry(
                            "/synthetic/orders",
                            {},
                            {"items": [{"owner": 9007199254740993}]},
                            second=1,
                        ),
                    ]
                }
            }
        ],
    )
    assert model["edges"][0]["target"]["pointer"] == "/items/0/owner"
    result = compile_model(confirm(model))
    assert result["scenario"][1]["request"]["json"]["items"][0]["owner"].startswith("${dep_")


def test_secret_value_aliases_are_externalized_not_matched():
    raw = entry(
        "/synthetic/orders",
        {"token": "token-canary"},
        {"buyer": "token-canary", "item": "cookie-canary"},
    )
    result = normalize_har({"log": {"entries": [raw]}}, catalog())
    serialized = json.dumps(result)
    assert "token-canary" not in serialized and "cookie-canary" not in serialized
    assert result["limitations"][0]["code"] == "external_auth"


@pytest.mark.parametrize("body", [[], "wrong-container", 42])
def test_request_container_type_mismatch_cannot_compile(body):
    model = model_for([entry("/synthetic/orders", {}, body)])
    assert not validate_model(model)["valid"]


def test_swagger_required_body_is_one_typed_external_input():
    source = normalize_catalog(
        {
            "swagger": "2.0",
            "paths": {
                "/synthetic/create": {
                    "post": {
                        "operationId": "create",
                        "parameters": [
                            {
                                "name": "payload",
                                "in": "body",
                                "required": True,
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                },
                            }
                        ],
                        "responses": {
                            "200": {"description": "合成响应", "schema": {"type": "object"}}
                        },
                    }
                }
            },
        }
    )
    model = discover(source)
    assert len(model["inputs"]) == 1
    assert next(iter(model["inputs"].values()))["type"] == "object"
    assert isinstance(model["steps"][0]["request"]["body"], str)
    assert validate_model(model)["valid"]
