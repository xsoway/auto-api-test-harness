#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2026/10/28 15:30
# @Filename: test_execution_contract.py
# @Author  : Alan_Hsu
"""A 阶段执行契约：所有入口的失败策略、类型和可信产出，全部离线。"""

from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest
import yaml

from ah.core import plugin
from ah.core.orchestrator import LLMHooks, _build_request, run_flow, run_single, summarize_results
from ah.harness.http import HTTPClient
from ah.harness.loader import Case, load_cases
from ah.harness.registry import DEFAULT_HOST, ServiceRegistry
from ah.metrics.extract import ExtractionError, extract_vars
from ah.reporting.writer import ResultRecord
from ah.scheduler import run_cases
from ah.utils.context import Context


def _case(name, **kwargs):
    return Case(
        name=name,
        request=kwargs.pop("request", {"method": "GET", "path": f"/{name}"}),
        scenario=kwargs.pop("scenario", "chain"),
        strict=kwargs.pop("strict", True),
        reference_mode=kwargs.pop("reference_mode", "typed"),
        assert_=kwargs.pop("assert_", {"status_code": 200}),
        **kwargs,
    )


def _client(handler):
    return HTTPClient(
        "http://api.test", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def _execute(entry, cases, client, monkeypatch):
    if entry == "flow":
        return run_flow(cases, Context(), client)
    if entry == "registry":
        registry = ServiceRegistry(clients={DEFAULT_HOST: client, "other": client})
        return registry.run_flow(cases, Context())
    if entry == "plugin":
        monkeypatch.setattr(plugin, "_session_records", [])
        try:
            plugin.test_scenario_flow(cases, Context(), client, LLMHooks())
        except AssertionError:
            pass
        return plugin._session_records
    return run_cases(cases, client=client, parallel=1 if entry == "serial" else 3)


@pytest.mark.parametrize("entry", ["flow", "registry", "plugin", "serial", "parallel"])
@pytest.mark.parametrize("policy", ["stop", "continue"])
def test_entrypoints_keep_planned_steps_and_agree_on_policy(entry, policy, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(500 if request.url.path == "/producer" else 200, json={"id": 91})

    cases = [
        _case("producer", extract={"id": {"path": "id"}}, on_failure=policy),
        _case(
            "consumer",
            depends_on=["producer"],
            request={
                "method": "GET",
                "path": "/consumer/${id}",
                "host": "other",
            },
        ),
        _case("independent"),
    ]
    client = _client(handler)
    try:
        results = _execute(entry, cases, client, monkeypatch)
    finally:
        client.close()
    assert [r.name for r in results] == [c.name for c in cases]
    assert [r.status for r in results] == [
        "executed",
        "blocked",
        "executed" if policy == "continue" else "skipped",
    ]
    assert results[0].verdict == "failed"
    assert results[1].verdict == "unverified"
    assert results[1].attempts == 0
    assert results[1].reason
    assert not any(path.startswith("/consumer") for path in seen)
    assert ("/independent" in seen) == (policy == "continue")
    if policy == "stop":
        assert results[2].attempts == 0
    else:
        assert results[2].verdict == "passed"
    counts = summarize_results(results)
    assert counts["planned"] == 3
    assert counts["blocked"] == 1
    assert counts["executed"] == (2 if policy == "continue" else 1)
    assert counts["failed"] == 1


@pytest.mark.parametrize("entry", ["flow", "registry", "plugin", "serial", "parallel"])
def test_zero_assertions_are_unverified_and_block_strict_consumers(entry, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"id": 91})

    cases = [
        _case("producer", assert_={}, extract={"id": {"path": "id"}}, on_failure="continue"),
        _case("consumer", depends_on=["producer"]),
        _case("independent"),
    ]
    client = _client(handler)
    try:
        results = _execute(entry, cases, client, monkeypatch)
    finally:
        client.close()
    assert [r.verdict for r in results] == ["unverified", "unverified", "passed"]
    assert seen == ["/producer", "/independent"]
    assert results[1].attempts == 0
    if isinstance(results[0], ResultRecord):
        assert results[0].result == "UNVERIFIED"
        assert results[1].result == "BLOCKED"


@pytest.mark.parametrize("failure", ["assertion", "partial_extract", "soft_assertion"])
def test_failed_strict_outputs_and_response_hook_mutation_do_not_leak(failure):
    ctx = Context({"seed": {"items": [1]}})
    spec = {"id": {"path": "id"}}
    if failure == "partial_extract":
        spec["missing"] = {"path": "missing"}
    case = _case(
        "producer",
        extract=spec,
        assert_={"status_code": 201},
        soft_assert=failure == "soft_assertion",
    )

    def on_response(case, response, attempt_ctx):
        attempt_ctx.get("seed")["items"].append(2)
        return {"derived": 10}

    client = _client(lambda request: httpx.Response(200, json={"id": 7}))
    try:
        result = run_single(case, ctx, client, hooks=LLMHooks(on_response=on_response))
    finally:
        client.close()
    assert result.verdict == "failed"
    assert result.extracted == {}
    assert ctx.as_dict() == {"seed": {"items": [1]}}
    assert not result.succeeded


@pytest.mark.parametrize("parallel", [1, 3])
def test_strict_scenarios_do_not_share_context_or_halt_independent_scenarios(parallel):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"id": 73})

    cases = [
        _case("producer", scenario="first", extract={"id": {"path": "id"}}),
        _case("undeclared", scenario="second", request={"path": "/leak/${id}"}),
        _case("independent", scenario="third"),
    ]
    client = _client(handler)
    try:
        results = run_cases(cases, client=client, parallel=parallel)
    finally:
        client.close()
    assert [r.verdict for r in results] == ["passed", "failed", "passed"]
    assert set(seen) == {"/producer", "/independent"}


def test_typed_json_and_assertions_preserve_all_json_types_without_mutating_spec():
    values = {
        "integer": 42,
        "boolean": True,
        "null": None,
        "list": [2, False],
        "object": {"nested": [3]},
    }
    references = {key: "${" + key + "}" for key in values}
    assertion = {
        "status_code": 200,
        "jsonpath": {key: {"eq": value} for key, value in references.items()},
    }
    case = _case(
        "typed",
        request={
            "method": "POST",
            "path": "/typed/${integer}",
            "headers": {"x-id": "${integer}"},
            "json": references,
        },
        assert_=assertion,
    )
    original = deepcopy(assertion)
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path, request.headers["x-id"], body))
        return httpx.Response(200, json=body)

    client = _client(handler)
    try:
        first = run_single(case, Context(values), client)
        second = run_single(case, Context({**values, "integer": 99}), client)
    finally:
        client.close()
    assert first.verdict == second.verdict == "passed"
    assert seen[0] == ("/typed/42", "42", values)
    assert seen[1][2]["integer"] == 99
    assert type(seen[0][2]["boolean"]) is bool
    assert case.assert_ == original
    assert len(first.assertions) == 6


def test_data_is_recursively_rendered_and_embedded_references_remain_strings():
    ctx = Context({"id": 17, "flag": False, "items": [1, 2], "object": {"a": 1}, "null": None})
    case = _case(
        "form",
        request={
            "method": "POST",
            "path": "/form",
            "data": {
                "id": "${id}",
                "flag": "${flag}",
                "nested": ["${items}", "${object}", "${null}"],
                "message": "id=${id}",
            },
        },
    )
    assert _build_request(case, ctx)["data"] == {
        "id": 17,
        "flag": False,
        "nested": [[1, 2], {"a": 1}, None],
        "message": "id=17",
    }
    case.request["data"] = "id=${id}&flag=${flag}"
    seen = []
    client = _client(lambda request: seen.append(request.content) or httpx.Response(200, json={}))
    try:
        result = run_single(case, ctx, client)
    finally:
        client.close()
    assert result.verdict == "passed"
    assert seen == [b"id=17&flag=False"]


def test_legacy_references_outputs_and_success_boolean_remain_compatible():
    ctx = Context({"id": 42})
    case = _case(
        "old",
        strict=False,
        reference_mode="legacy",
        assert_={},
        request={"method": "POST", "path": "/old", "json": {"id": "${id}"}},
        extract={"out": {"path": "id"}},
    )
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"id": 83})

    client = _client(handler)
    try:
        result = run_single(case, ctx, client)
    finally:
        client.close()
    assert result.succeeded
    assert result.verdict == "unverified"
    assert ctx.get("out") == 83
    assert seen == [{"id": "42"}]
    assert ResultRecord.from_step(result).result == "UNVERIFIED"


def test_null_extraction_is_typed_only_and_missing_still_fails():
    spec = {"value": {"path": "data.items[0].value"}}
    text = '{"data":{"items":[{"value":null}]}}'
    assert extract_vars(text, spec, Context(), allow_null=True) == {"value": None}
    with pytest.raises(ExtractionError):
        extract_vars(text, spec, Context())
    with pytest.raises(ExtractionError):
        extract_vars('{"data":{"items":[{}]}}', spec, Context(), allow_null=True)


@pytest.mark.parametrize("shape", ["scenario", "cases", "single"])
def test_v2_loader_propagates_strict_typed_defaults(tmp_path, shape):
    step = {
        "name": "first",
        "request": {"path": "/first"},
        "reference_mode": "legacy",
        "strict": False,
        "depends_on": [],
    }
    document = {"schema_version": 2, "name": "flow"}
    if shape == "single":
        document.update(step)
    else:
        document[shape] = [step]
    path = tmp_path / "typed.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    (case,) = load_cases(path)
    assert case.strict and case.reference_mode == "typed"
    assert case.depends_on == []


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 3, "request": {"path": "/"}},
        {"reference_mode": "typo", "request": {"path": "/"}},
    ],
)
def test_loader_rejects_unknown_execution_contract(tmp_path, document):
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(path)


@pytest.mark.parametrize("invalid", ["duplicate", "forward", "unknown"])
def test_invalid_strict_plan_is_blocked_before_any_requests(invalid):
    first = _case("first")
    second = _case("first" if invalid == "duplicate" else "second")
    if invalid != "duplicate":
        first.depends_on = ["second" if invalid == "forward" else "missing"]
    seen = []
    client = _client(lambda request: seen.append(request.url.path) or httpx.Response(200, json={}))
    try:
        results = run_flow([first, second], Context(), client)
    finally:
        client.close()
    assert [r.status for r in results] == ["blocked", "blocked"]
    assert all(r.attempts == 0 and r.reason for r in results)
    assert seen == []


def test_result_roundtrip_and_counts_separate_steps_from_evaluated_assertions():
    client = _client(lambda request: httpx.Response(500, json={}))
    try:
        results = run_flow([_case("bad"), _case("skipped")], Context(), client)
    finally:
        client.close()
    records = [ResultRecord.from_step(step) for step in results]
    restored = [ResultRecord.from_dict(record.to_dict()) for record in records]
    assert [(r.status, r.verdict, r.attempts) for r in restored] == [
        (r.status, r.verdict, r.attempts) for r in results
    ]
    counts = summarize_results(restored)
    assert counts == {
        "planned": 2,
        "executed": 1,
        "passed": 0,
        "failed": 1,
        "blocked": 0,
        "skipped": 1,
        "unverified": 1,
        "assertions": 1,
        "assertions_passed": 0,
        "assertions_failed": 1,
    }


@pytest.mark.parametrize("entry", ["flow", "registry"])
def test_multiple_strict_scenarios_have_isolated_seed_contexts(entry, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"id": 44})

    cases = [
        _case("producer", scenario="first", extract={"id": {"path": "id"}}),
        _case("consumer", scenario="second", request={"path": "/leak/${id}"}),
        _case("independent", scenario="third"),
    ]
    client = _client(handler)
    try:
        results = _execute(entry, cases, client, monkeypatch)
    finally:
        client.close()
    assert [result.verdict for result in results] == ["passed", "failed", "passed"]
    assert seen == ["/producer", "/independent"]


def test_verified_null_output_is_published_and_consumed_as_json_null():
    seen = []

    def handler(request):
        if request.url.path == "/producer":
            return httpx.Response(200, json={"nested": {"value": None}})
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True})

    cases = [
        _case("producer", extract={"nullable": {"path": "nested.value"}}),
        _case(
            "consumer",
            depends_on=["producer"],
            request={
                "method": "POST",
                "path": "/consumer",
                "json": {"value": "${nullable}"},
            },
        ),
    ]
    ctx = Context()
    client = _client(handler)
    try:
        results = run_flow(cases, ctx, client)
    finally:
        client.close()
    assert all(result.verdict == "passed" for result in results)
    assert ctx.has("nullable") and ctx.get("nullable") is None
    assert seen == [{"value": None}]


def test_legacy_failed_output_and_serial_context_sharing_remain_available():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(500 if request.url.path == "/producer" else 200, json={"id": 13})

    cases = [
        _case(
            "producer",
            strict=False,
            reference_mode="legacy",
            on_failure="continue",
            extract={"id": {"path": "id"}},
        ),
        _case(
            "consumer", strict=False, reference_mode="legacy", request={"path": "/consumer/${id}"}
        ),
    ]
    client = _client(handler)
    try:
        results = run_flow(cases, Context(), client)
    finally:
        client.close()
    assert [result.succeeded for result in results] == [False, True]
    assert seen[-1] == "/consumer/13"


def test_unknown_registry_route_is_a_zero_attempt_record_and_continue_still_works():
    seen = []
    client = _client(lambda request: seen.append(request.url.path) or httpx.Response(200, json={}))
    registry = ServiceRegistry(domains={}, clients={DEFAULT_HOST: client})
    cases = [
        _case("unknown", request={"path": "/", "host": "missing"}, on_failure="continue"),
        _case("independent"),
    ]
    try:
        results = registry.run_flow(cases, Context())
    finally:
        registry.close()
    assert [result.status for result in results] == ["blocked", "executed"]
    assert results[0].attempts == 0 and results[0].reason
    assert results[1].verdict == "passed"
    assert seen == ["/independent"]


def test_typed_assertions_do_not_coerce_number_strings_and_render_each_run():
    case = _case("typed", assert_={"jsonpath": {"value": {"eq": "${expected}"}}})
    client = _client(lambda request: httpx.Response(200, json={"value": 42}))
    try:
        wrong_type = run_single(case, Context({"expected": "42"}), client)
        correct_type = run_single(case, Context({"expected": 42}), client)
    finally:
        client.close()
    assert wrong_type.verdict == "failed"
    assert correct_type.verdict == "passed"
    assert case.assert_["jsonpath"]["value"]["eq"] == "${expected}"


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        (True, 1),
        (1, True),
        ("1", 1),
        (1, 1.0),
        ({"nested": [True]}, {"nested": [1]}),
        ([{"value": "1"}], [{"value": 1}]),
    ],
)
def test_typed_equality_distinguishes_nested_json_scalar_types(actual, expected):
    case = _case("typed", assert_={"jsonpath": {"value": {"eq": "${expected}"}}})
    client = _client(lambda request: httpx.Response(200, json={"value": actual}))
    try:
        result = run_single(case, Context({"expected": expected}), client)
    finally:
        client.close()
    assert result.verdict == "failed"


def test_report_exports_unverified_and_blocked_execution_counts(tmp_path, monkeypatch):
    from ah.reporting.writer import build_report

    monkeypatch.setenv("LLMOFF", "1")
    client = _client(lambda request: httpx.Response(200, json={}))
    try:
        results = run_flow(
            [
                _case("unverified", assert_={}),
                _case("dependent", depends_on=["unverified"]),
            ],
            Context(),
            client,
        )
    finally:
        client.close()
    records = [ResultRecord.from_step(result) for result in results]
    build_report(records, report_dir=str(tmp_path), history_rows=[])
    payload = json.loads((tmp_path / "report.json").read_text())
    assert payload["summary"] == {
        "total": 2,
        "passed": 0,
        "failed": 0,
        "assert_density": {
            "avg_per_case": 0.0,
            "total_asserts": 0,
            "high": 0,
            "medium": 0,
            "none": 2,
            "no_assert_cases": ["unverified", "dependent"],
        },
    }
    assert payload["execution_counts"]["blocked"] == 1
    assert payload["execution_counts"]["unverified"] == 2
    assert payload["execution_counts"]["assertions"] == 0
    assert [record["result"] for record in payload["results"]] == ["UNVERIFIED", "BLOCKED"]
    assert all(record["reason"] for record in payload["results"])


@pytest.mark.parametrize("dependency", ["missing", "single"])
def test_standalone_strict_dependency_never_dispatches_http(dependency):
    """独立入口不能绕过不存在/自引用的强依赖。"""
    seen = []
    client = _client(lambda request: seen.append(request.url.path) or httpx.Response(200, json={}))
    try:
        result = run_single(_case("single", depends_on=[dependency]), Context(), client)
    finally:
        client.close()
    assert result.status == "blocked"
    assert result.attempts == 0
    assert seen == []


def test_missing_historical_verdict_does_not_become_verified_pass():
    """旧记录缺验证证据时只能恢复为未验证，不能由执行布尔值补造 PASS。"""
    from ah.core.orchestrator import StepResult

    old = StepResult.from_dict({"name": "historical", "succeeded": True})
    record = ResultRecord.from_step(old)
    assert record.verdict == "unverified"
    assert record.result == "UNVERIFIED"
    assert summarize_results([old, record])["passed"] == 0


def test_cli_separate_strict_scenarios_cannot_consume_previous_outputs(tmp_path, monkeypatch):
    """CLI 分组调用不得把前一个严格场景输出当作下一个场景输入。"""
    import argparse

    from ah import evolution

    data = tmp_path / "data"
    data.mkdir()
    for filename, name, step in [
        (
            "a.yaml",
            "first",
            {
                "name": "producer",
                "request": {"path": "/producer"},
                "extract": {"id": {"path": "id"}},
                "assert": {"status_code": 200},
            },
        ),
        (
            "b.yaml",
            "second",
            {
                "name": "consumer",
                "request": {"path": "/leak/${id}"},
                "assert": {"status_code": 200},
            },
        ),
    ]:
        (data / filename).write_text(
            yaml.safe_dump(
                {
                    "schema_version": 2,
                    "name": name,
                    "scenario": [step],
                }
            ),
            encoding="utf-8",
        )
    seen = []
    client = _client(
        lambda request: (
            seen.append(request.url.path) or httpx.Response(200, json={"id": "first-only"})
        )
    )
    monkeypatch.setenv("AH_LOCAL_OBS", "0")
    monkeypatch.setattr(evolution, "_hermetic_client", lambda **kwargs: client)
    try:
        code = evolution._cmd_run(
            argparse.Namespace(
                project=str(tmp_path),
                env="all",
                suite="regression",
                priority=None,
                tags=None,
            )
        )
    finally:
        client.close()
    assert code == 1
    assert seen == ["/producer"]
