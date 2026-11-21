"""HTML 报告生成器 hermetic 验证（复用 ResultRecord 结果数据）。"""
from ah.reporting.writer import ResultRecord, build_html_report


def test_build_html_report_writes_self_contained_static_html(tmp_path):
    records = [
        ResultRecord(
            name="store-home",
            result="PASS",
            duration=0.123,
            request={"method": "GET", "path": "https://m.example.com/?page=1"},
            response={"status_code": 200, "text": "<h1>商品 &amp; 价格</h1>"},
        ),
        ResultRecord(
            name="orders.POST",
            result="FAIL",
            duration=0.5,
            failure_msg="{status_code} expected <201>, got 500 && error",
            request={"method": "POST", "path": "/api/orders", "json": {"sku": "A-1"}},
            response={"status_code": 500, "text": "server error"},
        ),
        ResultRecord(name="health.GET", result="PASS", duration=0.02),
    ]
    html = build_html_report(records, report_dir=str(tmp_path))

    text = html.read_text()
    # 汇总卡片 + 结果表。
    assert "Test Report" in text
    assert "2/3 passed, 1 failed" in text
    assert "store-home" in text
    assert "orders.POST" in text
    assert 'class="badge pass"' in text
    assert 'class="badge fail"' in text
    # 失败信息必须 HTML 转义，避免破坏 DOM / 注入标签。
    assert "<201>" not in text
    assert "&lt;201&gt;" in text
    assert "&amp;&amp;" in text
    # 单文件自包含：<head> 段不引用外部脚本/样式资源。
    head = text.split("<head>")[1].split("</head>")[0]
    assert "<link" not in head and "<script" not in head
    # 请求/响应详情块：method + path + status + body 片段。
    assert "详情（请求/响应/自愈）" in text
    assert "GET https://m.example.com/?page=1" in text
    assert "POST /api/orders" in text
    assert "status 200" in text and "status 500" in text
    # 响应正文片段同样转义（含 HTML 标记）。
    assert "&lt;h1&gt;商品 &amp;amp; 价格&lt;/h1&gt;" in text  # 响应正文所有 HTML 均已转义
    assert "<h1>商品" not in text  # 原文不应泄露原始标签


def test_from_step_carries_request_response_detail_into_html(tmp_path):
    """run_single -> from_step -> build_html_report 端到端：详情确实进入报告。"""
    import httpx

    from ah.core.orchestrator import run_single
    from ah.harness.http import HTTPClient
    from ah.harness.loader import Case
    from ah.utils.context import Context

    case = Case(
        name="store-home",
        request={"method": "GET", "path": "https://web.example.test/home"},
        assert_={"status_code": 200, "contains": ["Example store"]},
    )
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, text='<html lang="en"><body>Example store</body></html>')

    client = HTTPClient("", client=httpx.Client(transport=httpx.MockTransport(handler)))
    step = run_single(case, Context(), client)
    assert step.succeeded, step.error

    record = ResultRecord.from_step(step)
    assert record.request == {
        "method": "GET",
        "path": "https://web.example.test/home",
        "params": None,
        "json": None,
        "data": None,
    }
    assert record.response is not None and record.response["status_code"] == 200

    text = build_html_report([record], report_dir=str(tmp_path)).read_text()
    assert "GET https://web.example.test/home" in text
    assert "status 200" in text
    assert "Example store" in text
    assert captured["url"] == "https://web.example.test/home"


def test_build_html_report_uses_report_writer(tmp_path):
    from ah.reporting.writer import ReportWriter

    writer = ReportWriter(report_dir=str(tmp_path))
    path = writer.write_html([ResultRecord(name="x", result="PASS")])
    assert path.name == "report.html"
    assert "x" in path.read_text()


def test_html_semantic_review_badge_and_analysis(tmp_path):
    rec = ResultRecord(
        name="orders.POST",
        result="FAIL",
        duration=0.5,
        failure_msg="boom",
        request={"method": "POST", "path": "/api/orders"},
        response={"status_code": 500},
    )
    rec.flaky = True
    rec.llm_notes = {
        "assert_llm": [{"verdict": "UNVERIFIED", "flagged": True, "reason": "price not ascending"}]
    }
    text = build_html_report([rec], report_dir=str(tmp_path)).read_text()
    # flaky 高亮 + 语义需人工复核 badge + 运行分析 panel + hero 计数。
    assert 'class="badge flaky"' in text
    assert "语义需复核" in text
    assert "运行分析" in text
    assert "语义复核 1" in text
    # 嵌入 JSON 也携带语义判定聚合（CI 可消费）。
    assert '"semantic_verdicts"' in text


def test_html_surfaces_evolution_and_telemetry_cards(tmp_path):
    """HTML 报告指标网格 + md 下载体现自主演进与 LLM 成本新功能数据。"""
    import json

    from ah.reporting.writer import ResultRecord, build_html_report

    project_root = tmp_path
    report_dir = tmp_path / "report"
    evo = project_root / "_evolution_log.jsonl"
    evo.write_text(
        json.dumps(
            {
                "ts": "2026-10-09T10:00:00",
                "round": 1,
                "planned_suite": "smoke",
                "candidates": 3,
                "accepted": 2,
                "rejected": 1,
                "dedup_skipped": 0,
                "stop_reason": "converged",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ledger = report_dir / "llm_usage.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"model": "gpt-4o", "status": "success", "cost": 0.01}) + "\n",
        encoding="utf-8",
    )

    rec = ResultRecord(name="users.GET", result="PASS", duration=0.1)
    text = build_html_report([rec], report_dir=str(report_dir)).read_text()
    # 指标网格卡 + md 下载视图二者都体现新功能数据。
    assert "自主演进" in text
    assert "1 轮" in text
    assert "LLM 成本" in text
    assert "$0.0100" in text
    assert "1 调用" in text
    assert "## 自主演进" in text
    assert "## LLM 成本" in text
    # R3 新面板：接口健康 + LLM 用途分布（默认收起 details.panel）。
    assert 'id="interface-health"' in text
    assert 'id="llm-usage"' in text
    assert "接口健康画像" in text
    assert "LLM 用途分布 · 时延" in text
    # 已用功能点详情：按「触发条件/执行/产物」分小节呈现（HTML 内 split('；')）。
    assert "触发条件：" in text and "产物：" in text
