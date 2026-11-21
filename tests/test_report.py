"""Step 9 验证：报告生成器（隔离运行）。"""
import json

from ah.reporting.writer import ResultRecord, build_report


def test_build_report_writes_md_and_json(tmp_path):
    records = [
        ResultRecord(name="users.GET", result="PASS", duration=0.123,
                     assertions=[{"passed": True}]),
        ResultRecord(
            name="orders.POST",
            result="FAIL",
            duration=0.5,
            failure_msg="{status_code} expected 201, got 500",
            llm_notes={"llm_off": True},
            assertions=[{"passed": False}, {"passed": False}],
        ),
        ResultRecord(name="health.GET", result="PASS", duration=0.02),
    ]
    md = build_report(records, {"trace-1": "https://langfuse/x"}, report_dir=str(tmp_path))

    text = md.read_text()
    assert "## Results" in text
    assert "**Summary**: 2/3 passed, 1 failed" in text
    assert "| users.GET | PASS |" in text
    assert "expected 201, got 500" in text

    data = json.loads((tmp_path / "report.json").read_text())
    assert data["summary"] == {
        "total": 3,
        "passed": 2,
        "failed": 1,
        "assert_density": {
            "avg_per_case": 1.0,
            "total_asserts": 3,
            "high": 0,
            "medium": 2,
            "none": 1,
            "no_assert_cases": ["health.GET"],
        },
    }
    assert len(data["results"]) == 3
    assert data["results"][1]["result"] == "FAIL"
    assert data["trace_urls"] == {"trace-1": "https://langfuse/x"}


def test_trace_link_uses_sdk_get_trace_url(monkeypatch):
    """_trace_link 委托 get_tracer().trace_url，产出官方格式深度链接。"""
    from ah.reporting import writer

    class FakeTracer:
        def trace_url(self, trace_id):
            return f"https://cloud.langfuse.com/project/p1/traces/{trace_id}"

    monkeypatch.setattr(writer, "get_tracer", lambda: FakeTracer())
    assert (
        writer._trace_link("ab" * 16)
        == "https://cloud.langfuse.com/project/p1/traces/" + "ab" * 16
    )


def test_trace_link_none_when_tracer_lacks_method(monkeypatch):
    """tracer 没有 trace_url 方法时 _trace_link 返回 None（静默，不中断报告）。"""
    from ah.reporting import writer

    class NoMethodTracer:
        pass

    monkeypatch.setattr(writer, "get_tracer", lambda: NoMethodTracer())
    assert writer._trace_link("ab" * 16) is None



def test_run_trace_link_uses_run_trace_id(monkeypatch):
    """_run_trace_link 用 tracer.run_trace_id 生成官方 Langfuse 链接。"""
    from ah.reporting import writer

    class FakeTracer:
        enabled = True
        run_trace_id = "ab" * 16

        def trace_url(self, trace_id):
            return f"https://cloud.langfuse.com/project/p1/traces/{trace_id}"

    monkeypatch.setattr(writer, "get_tracer", lambda: FakeTracer())
    url = writer._run_trace_link()
    assert url == "https://cloud.langfuse.com/project/p1/traces/" + "ab" * 16


def test_run_trace_link_none_when_tracer_disabled(monkeypatch):
    """追踪禁用（NullTracer）时 _run_trace_link 返回 None。"""
    from ah.reporting import writer

    class Nullish:
        enabled = False

    monkeypatch.setattr(writer, "get_tracer", lambda: Nullish())
    assert writer._run_trace_link() is None

def test_mark_flaky_from_history_jitter(tmp_path):
    from ah.reporting.writer import _mark_flaky_from_history

    # 历史里该接口既有 PASS 又有 FAIL（≥3 观测）→ 判定 flaky。
    prior = [
        {"run_time": "t1", "cases": {"users.GET": "PASS"}},
        {"run_time": "t2", "cases": {"users.GET": "FAIL"}},
        {"run_time": "t3", "cases": {"users.GET": "PASS"}},
    ]
    rec = ResultRecord(name="users.GET", result="FAIL", request={"path": "/users"})
    _mark_flaky_from_history([rec], prior)
    assert rec.flaky is True


def test_mark_flaky_from_history_stable_fail_not_flaky(tmp_path):
    from ah.reporting.writer import _mark_flaky_from_history

    # 持续失败（从无 PASS）→ 持久性缺陷，不打 flaky。
    prior = [
        {"run_time": "t1", "cases": {"orders.POST": "FAIL"}},
        {"run_time": "t2", "cases": {"orders.POST": "FAIL"}},
        {"run_time": "t3", "cases": {"orders.POST": "FAIL"}},
    ]
    rec = ResultRecord(name="orders.POST", result="FAIL", request={"path": "/orders"})
    _mark_flaky_from_history([rec], prior)
    assert rec.flaky is False


def test_build_report_writes_analysis_section(tmp_path):
    # 确定性（hermetic）NL 摘要：即使无 LLM 也产出「运行分析」段 + report.json。
    rec = ResultRecord(name="users.GET", result="PASS", duration=0.1)
    md = build_report([rec], report_dir=str(tmp_path))
    text = md.read_text()
    assert "## 运行分析" in text
    assert "1/1 passed" in text
    data = json.loads((tmp_path / "report.json").read_text())
    assert "分析" in data["analysis"]
    assert data["semantic_verdicts"] == {
        "SKIP": 0,
        "PASS": 0,
        "UNVERIFIED": 0,
        "flagged": 0,
    }


def test_agent_report_summarize_records_unverified(tmp_path):
    from ah.llm.agents import AgentReport

    rec = ResultRecord(name="orders.POST", result="FAIL", duration=0.2)
    rec.llm_notes = {
        "assert_llm": [{"verdict": "UNVERIFIED", "flagged": True, "reason": "price not ascending"}]
    }
    out = AgentReport().summarize_records([rec])
    assert "语义判定 UNVERIFIED" in out
    assert "price not ascending" in out


def test_semantic_verdicts_agg_multiple(tmp_path):
    from ah.reporting.writer import _semantic_verdicts_agg

    passed = ResultRecord(name="a", result="PASS")
    passed.llm_notes = {"assert_llm": {"verdict": "PASS"}}
    unv = ResultRecord(name="b", result="FAIL")
    unv.llm_notes = {
        "assert_llm": [
            {"verdict": "UNVERIFIED", "flagged": True, "reason": "x"},
            {"verdict": "PASS"},
        ]
    }
    agg = _semantic_verdicts_agg([passed, unv])
    assert agg["PASS"] == 2
    assert agg["UNVERIFIED"] == 1
    assert agg["flagged"] == 1

def test_build_report_custom_basename(tmp_path):
    """basename 参数：md/json 写到自定义文件名，不覆盖默认 report.*。"""
    rec = ResultRecord(name="users.GET", result="PASS", duration=0.1)
    md = build_report([rec], report_dir=str(tmp_path), basename="test-tcc-m-api-20261008_153000")

    assert md.name == "test-tcc-m-api-20261008_153000.md"
    assert (tmp_path / "test-tcc-m-api-20261008_153000.json").exists()
    # 默认 report.* 不被写（basename 独立，不残留占位）。
    assert not (tmp_path / "report.md").exists()


def test_build_html_report_custom_basename(tmp_path):
    """build_html_report 的 basename 与 build_report 对齐，避免 md/json/html 错位。"""
    from ah.reporting.writer import build_html_report

    rec = ResultRecord(name="users.GET", result="PASS", duration=0.1)
    html = build_html_report(
        [rec], report_dir=str(tmp_path), basename="test-tcc-m-api-20261008_153000"
    )
    assert html.name == "test-tcc-m-api-20261008_153000.html"


def test_auto_reporter_timestamped_runs_do_not_overwrite(tmp_path):
    """report_basename 指定后：两次运行写两份带时间戳文件，历史不被覆盖。"""
    from ah.core.orchestrator import StepResult
    from ah.core.selfcontained import AutoReporter

    def _run() -> str:
        rep = AutoReporter(tmp_path, report_basename="test-tcc-m-api")
        rep.record(StepResult(name="users.GET", succeeded=True))
        md = rep.write_reports()
        assert md.exists()
        return md.name

    first = _run()
    second = _run()
    assert first != second  # 时间戳不同，互不覆盖
    assert (tmp_path / first).exists()
    assert (tmp_path / second).exists()
    # 时间戳留档时同步镜像到固定 report.*，让 canonical 当前报告始终最新。
    for ext in ("md", "json", "html"):
        assert (tmp_path / f"report.{ext}").exists()
    assert (tmp_path / "report.md").read_text() == (tmp_path / second).read_text()
    legacy = AutoReporter(tmp_path, log_name="ah.selfcontained.legacy")
    legacy.record(StepResult(name="a", succeeded=True))
    assert legacy.write_reports().name == "report.md"


def _write_evolution_log(project_root, rows):
    """在项目根写 `_evolution_log.jsonl`（自主演化审计产物）。"""
    evo = project_root / "_evolution_log.jsonl"
    evo.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def _write_ledger(report_dir, records):
    """写 `llm_usage.jsonl` 账本（telemetry 聚合输入）。"""
    ledger = report_dir / "llm_usage.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_project_insights_aggregates_evolution_and_telemetry(tmp_path):
    """_project_insights 正确聚合演进日志 + telemetry 账本（hermetic 文件输入）。"""
    from ah.reporting.writer import _project_insights

    project_root = tmp_path
    report_dir = tmp_path / "report"
    _write_evolution_log(
        project_root,
        [
            {
                "ts": "2026-10-09T10:00:00",
                "round": 1,
                "planned_suite": "smoke",
                "candidates": 3,
                "accepted": 2,
                "rejected": 1,
                "dedup_skipped": 0,
                "stop_reason": "converged",
            },
            {
                "ts": "2026-10-09T11:00:00",
                "round": 2,
                "planned_suite": "smoke",
                "candidates": 5,
                "accepted": 3,
                "rejected": 2,
                "dedup_skipped": 1,
                "stop_reason": None,
            },
        ],
    )
    _write_ledger(
        report_dir,
        [
            {"model": "gpt-4o", "status": "success", "cost": 0.01},
            {"model": "gpt-4o", "status": "error", "cost": 0.004},
            {"model": "gpt-4o-mini", "status": "success", "cost": 0.002},
        ],
    )

    insights = _project_insights(report_dir)
    evo = insights["evolution"]
    assert evo["rounds"] == 2
    assert evo["candidates"] == 8 and evo["accepted"] == 5
    assert evo["rejected"] == 3 and evo["dedup_skipped"] == 1
    assert evo["planned_suite"] == "smoke" and evo["stop_reason"] is None

    tel = insights["telemetry"]
    assert tel["requests"] == 3 and tel["success"] == 2 and tel["errors"] == 1
    assert tel["cost"] == 0.016
    assert tel["success_rate"] == round(2 / 3, 4)
    assert set(tel["by_model"]) == {"gpt-4o", "gpt-4o-mini"}


def test_project_insights_missing_sources_returns_none(tmp_path):
    """无演进日志/无账本 → 对应键为 None（报告隐藏该段，hermetic 不崩溃）。"""
    from ah.reporting.writer import _project_insights

    report_dir = tmp_path / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    insights = _project_insights(report_dir)
    assert insights["evolution"] is None
    assert insights["telemetry"] is None


def test_build_report_surfaces_evolution_and_telemetry_sections(tmp_path):
    """build_report 输出 md/json 体现自主演进 + LLM 成本新段。"""
    from ah.reporting.writer import ResultRecord, build_report

    project_root = tmp_path
    report_dir = tmp_path / "report"
    _write_evolution_log(
        project_root,
        [
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
        ],
    )
    _write_ledger(
        report_dir,
        [{"model": "gpt-4o", "status": "success", "cost": 0.01}],
    )
    rec = ResultRecord(name="users.GET", result="PASS", duration=0.1)
    md = build_report([rec], report_dir=str(report_dir))

    text = md.read_text()
    assert "## 自主演进" in text
    assert "## LLM 成本" in text
    assert "候选 / 采纳 / 拒绝 / 去重跳过**: 3/2/1/0" in text

    data = json.loads((report_dir / "report.json").read_text())
    assert data["evolution"]["rounds"] == 1
    assert data["telemetry"]["requests"] == 1
    assert data["telemetry"]["cost"] == 0.01


def test_build_report_omits_sections_when_no_data(tmp_path):
    """无演进/无账本 → md/json 不含新段（保持原报告最小化）。"""
    from ah.reporting.writer import ResultRecord, build_report

    report_dir = tmp_path / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    rec = ResultRecord(name="users.GET", result="PASS", duration=0.1)
    md = build_report([rec], report_dir=str(report_dir))
    text = md.read_text()
    assert "## 自主演进" not in text
    assert "## LLM 成本" not in text
    data = json.loads((report_dir / "report.json").read_text())
    assert "evolution" not in data
    assert "telemetry" not in data


def test_capability_matrix_marks_used_by_insights():
    """_capability_matrix 按 insights 动态打 used：演进+成本族 → 已用；其余可用。"""
    from ah.reporting.writer import _capability_matrix

    cap = _capability_matrix(
        {"rounds": 2, "candidates": 3},
        {"requests": 4, "success": 3, "errors": 1},
    )
    assert cap["total"] == 28
    used = {r["id"] for r in cap["rows"] if r["used"]}
    # 自主演进族 1/2/4/11/12 + LLM 成本 14 → 共 6 项已用。
    assert used == {1, 2, 4, 11, 12, 14}
    assert {3, 5, 6, 7, 8, 9, 10, 13, 15, 16, 17, 18, 19, 20, 21, 22}.isdisjoint(used)
    # 脚手架/scheduler/property/diff/趋势闸 → 可用未用。
    # 已用功能点带 desc + evidence；未用功能点 evidence 为 None。
    id1 = next(r for r in cap["rows"] if r["id"] == 1)
    assert id1["desc"] and id1["evidence"] and "2 轮" in id1["evidence"]
    id5 = next(r for r in cap["rows"] if r["id"] == 5)
    assert id5["desc"] and id5["evidence"] is None
    assert all("desc" in r for r in cap["rows"])


def test_capability_matrix_no_insights_all_available():
    """无任何 insights → 无已用项，全部标记可用（空运行也显示矩阵，表 honest）。"""
    from ah.reporting.writer import _capability_matrix

    cap = _capability_matrix(None, None)
    assert cap["used"] == 0
    assert all(not r["used"] for r in cap["rows"])


def test_build_report_surfaces_capability_matrix(tmp_path):
    """build_report 在自主演进+LLM 成本后追加「框架能力」矩阵段（md + json）。"""
    from ah.reporting.writer import ResultRecord, build_report

    project_root = tmp_path
    report_dir = tmp_path / "report"
    _write_evolution_log(
        project_root,
        [{"ts": "t1", "round": 1, "planned_suite": "smoke", "candidates": 1,
          "accepted": 1, "rejected": 0, "dedup_skipped": 0, "stop_reason": None},
         {"ts": "t2", "round": 2, "planned_suite": "smoke",
          "candidates": [{"name": "边界价格候选", "method": "GET", "path": "/cars"}],
          "accepted": ["边界价格候选"], "rejected": [],
          "dedup_skipped": 0, "stop_reason": None, "failures": 0}],
    )
    _write_ledger(report_dir, [
        {"model": "gpt-4o", "status": "success", "cost": 0.01,
         "prompt_tokens": 100, "completion_tokens": 50},

    ])
    rec = ResultRecord(
        name="users.GET", result="PASS",
        duration=0.1, response={"status_code": 200},
    )

    md = build_report([rec], report_dir=str(report_dir))

    text = md.read_text()
    assert "## 框架能力" in text
    assert "本次运行已用 6 / 28 项" in text
    assert "| 1 | 自主涌现闭环 | evolution |" in text
    assert "✅ 已用" in text and "· 可用" in text
    # 已用功能点详情：desc + 本次证据（演进轮次）。
    assert "### 已用功能点详情" in text
    assert "**[1] 自主涌现闭环**" in text
    assert "演进 2 轮" in text and "候选 2" in text
    # 名称聚合：跨轮候选/采纳名称真实呈现（非"无"）。
    assert "边界价格候选" in text
    # 每个已用功能点详情按「触发条件 / 执行 / 产物」分小节，且各功能点说明各不相同。
    assert "触发条件：" in text and "执行：" in text and "产物：" in text

    # LLM 成本段展示 token（语义聚合，不做假设金额）。
    assert "## LLM 成本" in text
    assert "Token" in text and "150" in text
    # 新指标段：接口健康 + LLM 用途分布 + 演进质量。
    assert "## 接口健康" in text
    assert "HTTP 状态分布" in text and "200:1" in text
    assert "### LLM 用途分布 · 时延" in text
    assert "采纳率" in text
    data = json.loads((report_dir / "report.json").read_text())
    assert data["capabilities"]["total"] == 28
    assert data["capabilities"]["used"] == 6
    assert data["capabilities"]["rows"][0]["used"] is True

    assert next(r for r in data["capabilities"]["rows"] if r["id"] == 5)["used"] is False
    # 新指标：接口健康（status + flaky + 耗时分位数）写入 json。
    assert data["interface_health"]["total"] == 1
    assert data["interface_health"]["flaky"] == 0
    assert data["interface_health"]["status_dist"] == {"200": 1}
    assert data["interface_health"]["duration_ms"]["avg"] >= 0

def test_capability_matrix_marks_run_driven_by_results():
    """results 中出现重试/超时/软断言信号 → 对应能力(15/16/21)打 used 并附 evidence。"""
    from ah.reporting.writer import ResultRecord, _capability_matrix

    retry_rec = ResultRecord(
        name="users.GET", result="PASS", duration=0.3,
        llm_notes={"retried": 1},
    )
    timeout_rec = ResultRecord(
        name="cars.GET", result="PASS", duration=0.2, timeout=3.5,
    )
    soft_rec = ResultRecord(
        name="orders.POST", result="PASS", duration=0.1,
        llm_notes={"soft_failures": 2},
    )
    cap = _capability_matrix(None, None, results=[retry_rec, timeout_rec, soft_rec])
    used = {r["id"] for r in cap["rows"] if r["used"]}
    assert {15, 16, 21} <= used
    # 三者的 evidence 均由运行记录构造。
    for rid in (15, 16, 21):
        row = next(r for r in cap["rows"] if r["id"] == rid)
        assert row["evidence"] and "执行" in row["evidence"]
    # 无运行信号的并行/profile/调试/占位符 → 可用未用。
    for rid in (18, 19, 20, 22):
        row = next(r for r in cap["rows"] if r["id"] == rid)
        assert not row["used"] and row["evidence"] is None


def test_capability_matrix_results_none_no_run_marks():
    """不传 results → 运行记录驱动的能力不虚标 used（兼容旧调用）。"""
    from ah.reporting.writer import _capability_matrix

    cap = _capability_matrix(None, None)
    assert all(not r["used"] for r in cap["rows"])


def test_resultrecord_timeout_and_soft_failures_roundtrip():
    """ResultRecord 的 timeout/soft_failures 在 from_dict→to_dict 往返一致。"""
    from ah.reporting.writer import ResultRecord

    rec = ResultRecord(
        name="orders.GET", result="PASS", duration=0.2, timeout=3.5,
        llm_notes={"soft_failures": 2},
    )
    restored = ResultRecord.from_dict(rec.to_dict())
    assert restored.timeout == 3.5
    assert restored.soft_failures == 2
    # 无配置时默认：timeout None、soft_failures 0。
    plain = ResultRecord(name="users.GET", result="PASS", duration=0.1)
    assert plain.timeout is None and plain.soft_failures == 0
    assert ResultRecord.from_dict(plain.to_dict()).timeout is None


def test_resultrecord_from_step_derives_timeout():
    """from_step 从 step.request_kwargs 的 timeout 派生字段。"""
    from types import SimpleNamespace

    from ah.reporting.writer import ResultRecord

    def _step(request_kwargs):
        return SimpleNamespace(
            name="users.GET",
            succeeded=True,
            llm_notes={},
            error=None,
            request_detail={},
            assertions=[],
            request_kwargs=request_kwargs,
            response=None,
        )

    rec = ResultRecord.from_step(_step({"timeout": 2.0}))
    assert rec.timeout == 2.0
    rec_none = ResultRecord.from_step(_step({}))
    assert rec_none.timeout is None

