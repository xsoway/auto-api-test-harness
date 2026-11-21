"""自动自愈（L1 确定性重试 / L2 LLM 反思）与报告体现的 hermetic 验证。

所有 HTTP 走 ``httpx.MockTransport``，零网络；LLM 全程关闭（LLMOFF）。
L1 自愈语义：失败时重跑完整单次管道（HTTP+提取+on_response+断言），只有
真瞬时失败才会自愈；数据不匹配类失败重试后仍失败，不会被误判治愈。
"""

from __future__ import annotations

from typing import Any

import httpx

from ah.core.orchestrator import LLMHooks, run_single
from ah.harness.http import HTTPClient
from ah.harness.loader import Case
from ah.reporting.writer import ResultRecord, build_html_report, build_report
from ah.utils.context import Context

HOME_URL = "https://web.example.test/home"


def _ok_body() -> str:
    return "<html><body>Example store</body></html>"


def _case():
    return Case(
        name="store-home",
        request={"method": "GET", "path": HOME_URL},
        assert_={"status_code": 200, "contains": ["Example store"]},
    )


def test_l1_retry_heals_transient_failure():
    """首调 500、重试 200：L1 重试自愈 -> succeeded 且带 healed 标记。"""
    calls = {"n": 0}
    body = _ok_body()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=body)

    client = HTTPClient("", client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = run_single(_case(), Context(), client)

    assert calls["n"] == 2
    assert result.succeeded
    assert result.error is None
    healed = result.llm_notes["healed"]
    # 自愈标记：level/mode 固定字段 + 触发条件/原因/修复方案（供报告展示）。
    assert healed["level"] == "L1"
    assert healed["mode"] == "retry"
    assert "expected 200, got 500" in healed["trigger"]  # 触发条件 = 首跑失败错误
    assert "瞬时/偶发失败" in healed["reason"]  # 原因：瞬时失败
    assert "确定性重试" in healed["fix"]  # 修复方案：L1 重试


def test_l1_retry_fails_when_persistent():
    """恒定失败：L1 重试两跑皆败 -> 不成功、不治愈，保留错误。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="no match here")

    client = HTTPClient("", client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = run_single(_case(), Context(), client)

    assert calls["n"] == 2
    assert not result.succeeded
    assert result.llm_notes["L1_retry"]["succeeded"] is False
    assert "healed" not in result.llm_notes


def test_l1_retry_does_not_falsely_heal_on_response_failure():
    """on_response 派生失败（数据不匹配）重试后仍失败：不被误判自愈。"""

    def always_fail_on_response(case, resp, ctx):  # noqa: ANN001
        raise KeyError("pagedItems 中无匹配项")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_ok_body())

    client = HTTPClient("", client=httpx.Client(transport=httpx.MockTransport(handler)))
    hooks = LLMHooks(on_response=always_fail_on_response)
    result = run_single(_case(), Context(), client, hooks=hooks)

    assert not result.succeeded
    assert result.llm_notes["L1_retry"]["succeeded"] is False
    assert "healed" not in result.llm_notes


def test_record_heal_emits_trace_node_on_l1_success():
    """L1 自愈成功后，tracer 收到一次 record_heal(level=1, succeeded=True)。"""
    calls = {"n": 0}
    body = _ok_body()

    class FakeTracer:
        def __init__(self):
            self.heals: list[dict[str, Any]] = []

        def record_heal(self, step_name, *, level, mode, succeeded, meta=None):  # noqa: ANN001
            self.heals.append(
                {"step": step_name, "level": level, "mode": mode, "succeeded": succeeded}
            )

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=body)

    client = HTTPClient("", client=httpx.Client(transport=httpx.MockTransport(handler)))
    tracer = FakeTracer()
    result = run_single(_case(), Context(), client, tracer=tracer)

    assert result.succeeded
    assert tracer.heals == [
        {"step": result.name, "level": "1", "mode": "retry", "succeeded": True}
    ]


def test_report_renders_heal_and_root_cause(tmp_path):
    """带 healed + triage 的记录，md 与 html 都渲染自愈与根因。"""
    records = [
        ResultRecord(
            name="store-home",
            result="PASS",
            llm_notes={"healed": {"level": "L2", "mode": "llm", "meta": {"order_id": "O9"}}},
        ),
        ResultRecord(
            name="orders.POST",
            result="FAIL",
            failure_msg="{status_code} expected <201>, got 500",
            llm_notes={
                "L1_retry": {"succeeded": False, "error": "500"},
                "triage": {"root_cause": "上游库存接口未就绪", "llm_off": True},
            },
        ),
    ]

    md_path = build_report(records, report_dir=str(tmp_path))
    html_path = build_html_report(records, report_dir=str(tmp_path))

    md = md_path.read_text()
    html = html_path.read_text()
    # md 表新增「Self-heal / 根因」列，含 L2 自愈与根因。
    assert "Self-heal / 根因" in md
    assert "L2 LLM 自愈" in md
    assert "L1 重试失败" in md
    assert "根因: 上游库存接口未就绪" in md
    # html 表头 + 单元格 + 详情块都体现自愈/根因。
    assert "Self-heal / 根因" in html
    assert "L2 LLM 自愈" in html
    assert "根因: 上游库存接口未就绪" in html
    # 根因文本 HTML 转义，避免注入。
    assert "&lt;201&gt;" in html


def test_report_renders_heal_trigger_reason_fix(tmp_path):
    """enriched healed（触发条件/原因/修复方案）在 HTML 详情里结构化渲染。"""
    record = ResultRecord(
        name="orders.POST",
        result="PASS",
        llm_notes={
            "healed": {
                "level": "L2",
                "mode": "llm",
                "trigger": "{status_code} expected 201, got 500",
                "reason": "上游库存接口未就绪",
                "fix": "修正断言 201；候选请求 {\"qty\": 1}",
                "meta": {"assert": {"status_code": 201}},
            }
        },
    )
    html = build_html_report([record], report_dir=str(tmp_path)).read_text()
    # 结构化字段逐项呈现（触发条件 / 原因 / 修复方案）。
    assert "<span class=\"lbl\">触发条件</span>" in html
    assert "expected 201, got 500" in html
    assert "<span class=\"lbl\">原因</span>" in html
    assert "上游库存接口未就绪" in html
    assert "<span class=\"lbl\">修复方案</span>" in html
    assert "修正断言 201" in html
    # md 摘要也带自愈原因。
    md = build_report([record], report_dir=str(tmp_path)).read_text()
    assert "原因: 上游库存接口未就绪" in md


def test_report_no_heal_column_empty_when_none(tmp_path):
    """无自愈/根因时，md 表该列留空且 html 不渲染自愈细节。"""
    record = ResultRecord(name="health.GET", result="PASS")
    md = build_report([record], report_dir=str(tmp_path)).read_text()
    html = build_html_report([record], report_dir=str(tmp_path)).read_text()
    assert "| health.GET | PASS |" in md
    # 无自愈/根因时：不在请求/响应详情里渲染「自愈」区块（框架能力概述
    # 仍会描述自愈特性，但本次运行每个用例都不带即席自愈详情块）。
    assert "<section><h3>自愈</h3>" not in html
