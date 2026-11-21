"""OpenAPI 契约挖掘 / NL→用例 hermetic 验证（predict.contract）。

对应 docs/ai-testing-proposal.md §2 设计维度的契约挖掘（AgentContractMiner）
与 NL→YAML 用例生成（AgentCaseGen）。全部为确定性/隔离逻辑，LLM 禁用时
不发任何网络。
"""

from __future__ import annotations

from ah.harness.loader import Case
from ah.predict.contract import ContractAdvice, ContractMiner, case_from_intent
from ah.predict.generator import GeneratedCase

OPENAPI2 = {
    "paths": {
        "/users/{id}": {
            "get": {
                "responses": {
                    "200": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer"},
                                "name": {"type": "string"},
                            },
                            "required": ["id", "name"],
                        }
                    }
                }
            }
        }
    }
}


# ── mine_schema（响应样本 → extract/assert）─────────────────────────────────

def test_miner_schema_extracts_ids_and_type_asserts() -> None:
    adv = ContractMiner().mine_schema({"id": 1, "name": "x", "meta": {"order_id": 9}})
    assert adv.assert_["status_code"] == 200
    # 常见 id 字段 → 提取
    assert adv.extract["id"]["extract_type"] == "jsonpath"
    assert adv.extract["order_id"]["path"] == "meta.order_id"
    # 其余 string 叶 → type 断言（不做值断言，避免幻觉）
    assert adv.assert_["jsonpath"]["name"]["type"] == "string"


def test_miner_merge_into_case_is_non_destructive() -> None:
    case = Case(name="x", request={"method": "GET", "path": "/x"}, assert_={"contains": "ok"})
    ContractMiner().mine_schema({"status": "ok"}).merge_into(case)
    # 补齐 status_code，但不覆盖已有断言。
    assert case.assert_["status_code"] == 200
    assert case.assert_["contains"] == "ok"
    assert case.extract == {}


def test_miner_schema_list_probes_first_element() -> None:
    adv = ContractMiner().mine_schema([{"user_id": 7, "kind": "a"}])
    assert adv.extract["user_id"]["path"] == "[0].user_id"
    assert any("list" in n for n in adv.notes)


def test_miner_schema_scalar_no_assert_guesses() -> None:
    adv = ContractMiner().mine_schema("just-a-string")
    assert adv.assert_["status_code"] == 200
    assert "jsonpath" not in adv.assert_
    assert adv.extract == {}


# ── mine_openapi（OpenAPI 2 spec → 断言）────────────────────────────────────

def test_miner_openapi_required_non_id_type_asserts() -> None:
    adv = ContractMiner().mine_openapi(OPENAPI2, path="/users/{id}", method="get")
    assert adv.assert_["status_code"] == 200
    jp = adv.assert_.get("jsonpath", {})
    # name 必填且非 id → type 断言；id 交给 extract，不做类型断言。
    assert jp["name"]["type"] == "string"
    assert "id" not in jp


def test_miner_openapi_missing_operation_returns_note() -> None:
    adv = ContractMiner().mine_openapi({"paths": {}}, path="/nope", method="get")
    assert adv.assert_ == {}
    assert any("not found" in n for n in adv.notes)


def test_miner_openapi_defaults_first_get_operation() -> None:
    spec = {
        "paths": {
            "/health": {"get": {"responses": {"204": {"description": "ok"}}}},
        }
    }
    adv = ContractMiner().mine_openapi(spec)
    assert adv.assert_["status_code"] == 204


def test_miner_openapi3_content_local_refs_asserts_required_nested_fields() -> None:
    """只断言必填祖先下的必填叶，不能把可选对象内字段提升为必填。"""
    spec = {
        "openapi": "3.0.3",
        "paths": {"/orders": {"get": {"operationId": "listOrders", "responses": {
            "200": {"$ref": "#/components/responses/Order"},
        }}}},
        "components": {
            "responses": {"Order": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/Order"},
            }}}},
            "schemas": {"Order": {
                "type": "object", "required": ["data"],
                "properties": {
                    "data": {"type": "object", "required": ["id", "state", "meta", "items"],
                             "properties": {
                                 "id": {"type": "string", "format": "uuid"},
                                 "state": {"type": "string"},
                                 "meta": {"type": "object", "required": ["count"],
                                          "properties": {"count": {"type": "integer"}}},
                                 "items": {"type": "array", "items": {
                                     "type": "object", "required": ["label"],
                                     "properties": {"label": {"type": "string"}}}},
                                 "optional": {"type": "string"},
                             }},
                    "optional_parent": {"type": "object", "required": ["child"],
                                        "properties": {"child": {"type": "string"}}},
                },
            }},
        },
    }
    advice = ContractMiner().mine_openapi(spec, path="/orders", method="GET")
    assert advice.assert_["status_code"] == 200
    assert advice.assert_["jsonpath"] == {
        "data": {"type": "object"}, "data.state": {"type": "string"},
        "data.meta": {"type": "object"}, "data.meta.count": {"type": "integer"},
        "data.items": {"type": "array"},
    }
    assert advice.extract["id"]["path"] == "data.id"


def test_miner_does_not_guess_success_for_error_only_contract() -> None:
    advice = ContractMiner().mine_openapi({"paths": {"/x": {"get": {
        "responses": {"400": {"schema": {"type": "object"}}},
    }}}})
    assert advice.assert_ == {}
    assert any("no explicit success" in note for note in advice.notes)


def test_miner_unsupported_refs_and_composition_are_reported_not_invented() -> None:
    advice = ContractMiner().mine_openapi({"paths": {"/x": {"get": {
        "operationId": "x", "responses": {"200": {"content": {"application/json": {
            "schema": {"oneOf": [{"$ref": "https://invalid.test/schema.json"}]},
        }}}},
    }}}})
    assert advice.assert_ == {"status_code": 200}
    assert any("unsupported_ref" in note for note in advice.notes)
    assert any("unsupported_schema" in note for note in advice.notes)

def test_mined_unicode_fields_execute_and_detect_wrong_type() -> None:
    """目录字段含中文时，生成的选择器既能通过合法响应也能检出错误类型。"""
    import httpx

    from ah.core.orchestrator import run_single
    from ah.harness.http import HTTPClient
    from ah.utils.context import Context

    advice = ContractMiner().mine_openapi({"openapi": "3.0.3", "paths": {"/orders": {
        "get": {"operationId": "orders", "responses": {"200": {"schema": {
            "type": "object", "required": ["data"], "properties": {"data": {
                "type": "object", "required": ["名称"], "properties": {
                    "名称": {"type": "string"}}}}}}}}}}})
    for value, expected in [("订单", True), (42, False)]:
        with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": {"名称": value}})
        )) as raw:
            result = run_single(
                Case(name="unicode", request={"path": "/orders"}, assert_=advice.assert_),
                Context(), HTTPClient("https://synthetic.invalid", client=raw),
            )
        assert result.succeeded is expected


def test_contract_advice_fields() -> None:
    adv = ContractAdvice(assert_={"status_code": 200}, extract={"id": {"path": "id"}})
    assert adv.assert_["status_code"] == 200
    assert adv.extract["id"]["path"] == "id"


# ── case_from_intent（NL→YAML，hermetic 返回 None）──────────────────────────

def test_case_from_intent_hermetic_returns_none() -> None:
    # LLM 禁用：绝不发明接口，返回 None（由上层回退）。
    assert case_from_intent("create order then list it", llm=None) is None


def test_case_from_intent_live_parses_and_normalizes() -> None:
    # 用 MockLLM（零网络）验证产物结构：name/request 规范化 + 默认 status 断言。
    from ah.llm.mock import MockLLM

    llm = MockLLM()
    llm.set_json_response(
        {"name": "create-order", "request": {"method": "POST", "path": "/orders"}}
    )
    gen = case_from_intent("create order", llm=llm)
    assert isinstance(gen, GeneratedCase)
    assert gen.request["method"] == "post"
    assert gen.request["path"] == "/orders"
    assert gen.assert_["status_code"] == 200


def test_case_from_intent_invalid_request_returns_none() -> None:
    from ah.llm.mock import MockLLM

    # 非 dict / 畸形 JSON → 拒绝；judge 抛错也回退 None，绝不产出畸形用例。
    llm = MockLLM()
    llm.set_response("not-valid-json")
    assert case_from_intent("whatever", llm=llm) is None
