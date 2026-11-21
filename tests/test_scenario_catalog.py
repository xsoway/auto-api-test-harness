#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/28 15:30
# @Filename : test_scenario_catalog.py
# @Author   : Alan_Hsu
"""OpenAPI 目录的离线解析、引用边界和稳定标识行为。"""

from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from ah.scenario.catalog import load_catalog, normalize_catalog


def _spec():
    return {
        "openapi": "3.0.3",
        "security": [{"bearer": []}],
        "paths": {
            "/users/{id}": {
                "parameters": [{"$ref": "#/components/parameters/Id"}],
                "get": {
                    "operationId": "getUser",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "uuid"},
                        }
                    ],
                    "responses": {200: {"$ref": "#/components/responses/User"}},
                },
                "post": {
                    "operationId": "updateUser",
                    "security": [],
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/User"}}
                        }
                    },
                    "responses": {"201": {"description": "created"}},
                },
            },
        },
        "components": {
            "parameters": {
                "Id": {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
            },
            "schemas": {
                "User": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                }
            },
            "responses": {
                "User": {
                    "description": "user",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/User"}}
                    },
                    "links": {
                        "edit": {
                            "operationId": "updateUser",
                            "parameters": {"id": "$response.body#/id"},
                        }
                    },
                }
            },
        },
    }


def test_catalog_resolves_content_refs_parameters_security_and_links(tmp_path):
    document = _spec()
    original = deepcopy(document)
    src = tmp_path / "openapi.yaml"
    src.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    catalog = load_catalog(src, service="accounts")
    assert set(catalog) == {"service", "spec_hash", "operations", "issues"}
    assert catalog["issues"] == []
    get, post = catalog["operations"]
    assert get["key"] == "accounts GET /users/{id}"
    assert get["service"] == "accounts" and get["method"] == "GET"
    assert get["operation_id"] == "getUser"
    assert get["parameters"] == [
        {
            "name": "id",
            "in": "path",
            "required": True,
            "schema": {"type": "string", "format": "uuid"},
        }
    ]
    assert get["responses"]["200"]["schema"]["properties"]["name"] == {"type": "string"}
    assert get["links"]["200"]["edit"]["operationId"] == "updateUser"
    assert get["security"] == [{"bearer": []}]
    assert post["security"] == []
    assert post["request_schema"]["required"] == ["name"]
    assert document == original
    assert catalog["spec_hash"] == normalize_catalog(document)["spec_hash"]


def test_catalog_hash_is_order_stable_and_tracks_spec_changes():
    document = _spec()
    reordered = dict(reversed(list(document.items())))
    first = normalize_catalog(document)
    assert len(first["spec_hash"]) == 64
    assert first["spec_hash"] == normalize_catalog(reordered)["spec_hash"]
    changed = deepcopy(document)
    changed["paths"]["/users/{id}"]["get"]["summary"] = "changed"
    other = normalize_catalog(changed)
    assert first["spec_hash"] != other["spec_hash"]
    assert first["operations"][0]["key"] == other["operations"][0]["key"]


def test_catalog_missing_and_conflicting_operation_ids_do_not_replace_keys():
    catalog = normalize_catalog(
        {
            "paths": {
                "/one": {"get": {"operationId": "same", "responses": {"200": {}}}},
                "/two": {"get": {"operationId": "same", "responses": {"200": {}}}},
                "/three": {"get": {"responses": {"200": {}}}},
            }
        },
        "svc",
    )
    assert [op["key"] for op in catalog["operations"]] == [
        "svc GET /one",
        "svc GET /two",
        "svc GET /three",
    ]
    assert catalog["operations"][-1]["operation_id"] is None
    assert {issue["code"] for issue in catalog["issues"]} == {
        "conflicting_operation_id",
        "missing_operation_id",
    }


def test_catalog_external_and_unresolved_refs_are_explicit_without_network(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("目录解析不能访问网络")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    catalog = normalize_catalog(
        {
            "paths": {
                "/x": {
                    "get": {
                        "operationId": "x",
                        "requestBody": {"$ref": "https://invalid.test/body.yaml"},
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/Missing"}
                                    }
                                }
                            },
                            "400": {"$ref": "other.yaml#/Bad"},
                        },
                    }
                }
            }
        }
    )
    assert {issue["code"] for issue in catalog["issues"]} >= {"unsupported_ref", "unresolved_ref"}
    operation = catalog["operations"][0]
    assert operation["request_schema"] is None
    assert operation["responses"]["200"]["schema"] == {"$ref": "#/components/schemas/Missing"}
    assert operation["responses"]["400"]["schema"] is None


def test_catalog_recursive_schema_terminates_and_keeps_unexpanded_ref():
    catalog = normalize_catalog(
        {
            "paths": {
                "/node": {
                    "get": {
                        "operationId": "node",
                        "responses": {"200": {"schema": {"$ref": "#/definitions/Node"}}},
                    }
                }
            },
            "definitions": {
                "Node": {"type": "object", "properties": {"child": {"$ref": "#/definitions/Node"}}}
            },
        }
    )
    schema = catalog["operations"][0]["responses"]["200"]["schema"]
    assert schema["properties"]["child"] == {"$ref": "#/definitions/Node"}
    assert any(issue["code"] == "cyclic_ref" for issue in catalog["issues"])


def test_catalog_pointer_escaping_and_path_item_ref():
    catalog = normalize_catalog(
        {
            "paths": {"/escaped": {"$ref": "#/x-path"}},
            "x-path": {
                "get": {
                    "operationId": "escaped",
                    "responses": {"200": {"schema": {"$ref": "#/definitions/a~1b~0c"}}},
                }
            },
            "definitions": {"a/b~c": {"type": "string"}},
        }
    )
    assert catalog["operations"][0]["responses"]["200"]["schema"] == {"type": "string"}
    assert catalog["issues"] == []


def test_catalog_unsupported_schema_content_and_structure_are_not_silent():
    catalog = normalize_catalog(
        {
            "paths": {
                "/bad": [],
                "/x": {
                    "get": {
                        "operationId": "x",
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "oneOf": [{"type": "string"}, {"type": "integer"}]
                                        }
                                    },
                                    "text/plain": {"schema": {"type": "string"}},
                                }
                            }
                        },
                    }
                },
            },
            "webhooks": {},
        }
    )
    assert {issue["code"] for issue in catalog["issues"]} >= {
        "unsupported_path_item",
        "unsupported_schema",
        "unsupported_media_type",
        "unsupported_structure",
    }
    assert all(issue["message"] and issue["location"] for issue in catalog["issues"])


def test_catalog_rejects_non_mapping_document_and_reports_invalid_paths(tmp_path):
    src = tmp_path / "list.yaml"
    src.write_text("- not-an-openapi-object\n", encoding="utf-8")
    with pytest.raises(ValueError, match="顶层"):
        load_catalog(src)
    assert normalize_catalog({"paths": []})["issues"][0]["code"] == "unsupported_paths"
