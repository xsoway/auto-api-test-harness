"""pytest 插件：将 YAML 用例参数化到测试节点中。

行为
----
- 单用例 / 用例列表文件为每个用例产出一个参数化的 ``test_single_case``。
- 场景文件（顶层 ``scenario:``）产出一个 ``test_scenario_flow`` 节点，
  该节点在有序步骤上执行 :func:`ah.core.orchestrator.run_flow`。

用例文件在配置的数据目录下发现。数据目录不再全局默认：仅在显式配置
``AH_CASES``（或 ``--ah-data-dir``）时才产生参数化节点；未指定时不产生任何
用例节点（框架单元测试独立运行）。业务项目自包含的主要执行路径是项目内
``test_<业务>.py`` 显式 ``load_cases(projects/<业务>/data/)`` 并逐用例驱动
（不经本插件节点）——插件参数化保留为可选能力，非默认入口。

插件暴露夹具 ``ctx``（每个测试一个全新的 Context）、``ah_client``
（由 settings 构建；测试可用桩覆盖）以及 ``ah_hooks``（默认为隔离）。

报告
----
每个已执行的用例/流程都会向会话收集器追加一条 :class:`ResultRecord`。
会话结束时，报告写入 ``settings.report_dir``（``report.md`` + ``report.json``）。
收集器遵循 ``--ah-report-dir``，当没有执行任何用例时会被跳过。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from ah.core.config import settings
from ah.core.orchestrator import LLMHooks, execute_flow, run_single
from ah.core.trace import get_tracer
from ah.harness.http import HTTPClient
from ah.harness.loader import Case, discover_case_files, load_cases, select_cases
from ah.llm.agents import AgentHeal, AgentTriage
from ah.llm.openai import LLMClient, llm_enabled
from ah.reporting.writer import ResultRecord, build_report
from ah.utils.context import Context
from ah.utils.logging import configure_logging, get_logger

log = get_logger("plugin")

DEFAULT_DATA_DIR = None

# 会话收集器：在 sessionstart 重置，每个执行的用例/流程都会追加，
# 在 sessionfinish 时排空并写入报告。
_session_records: list[ResultRecord] = []


def _data_dir(config: Any) -> str | None:
    return str(config.getoption("--ah-data-dir")) if config.getoption("--ah-data-dir") else (
        os.getenv("AH_CASES") or DEFAULT_DATA_DIR
    )


def _case_filters(config: Any) -> dict[str, str]:
    """从 CLI/环境读取分类过滤条件（env/suite/priority/tag）。"""
    return {
        "env": config.getoption("--ah-env") or os.getenv("AH_ENV"),
        "suite": config.getoption("--ah-suite") or os.getenv("AH_SUITE"),
        "priority": config.getoption("--ah-priority") or os.getenv("AH_PRIORITY"),
        "tags": (config.getoption("--ah-tag") or os.getenv("AH_TAG") or "").split(",")
        if (config.getoption("--ah-tag") or os.getenv("AH_TAG"))
        else None,
    }


def pytest_addoption(parser: Any) -> None:
    group = parser.getgroup("ah")
    group.addoption(
        "--ah-data-dir",
        action="store",
        default=None,
        help="Directory (or AH_CASES glob) containing case YAML files.",
    )
    group.addoption(
        "--ah-report-dir",
        action="store",
        default=None,
        help="Directory for report.md / report.json (default: settings.report_dir).",
    )
    group.addoption(
        "--ah-env",
        action="store",
        default=None,
        help="Only run cases targeting these envs (comma list; 'all' matches any).",
    )
    group.addoption(
        "--ah-suite",
        action="store",
        default=None,
        help="Only run cases in these suites (comma list; 'all' matches any).",
    )
    group.addoption(
        "--ah-priority",
        action="store",
        default=None,
        help="Only run cases with these priorities (comma list; P0/P1/P2).",
    )
    group.addoption(
        "--ah-tag",
        action="store",
        default=None,
        help="Only run cases carrying any of these tags (comma list, OR).",
    )
    group.addoption(
        "--ah-semantic-gate",
        action="store_true",
        default=False,
        help="CI 判闸（R3）：任一语义判定 flagged（需人工复核）时以非零退出码结束。"
        "默认关闭（LLMOFF=1 下语义判定全 SKIP，绝不误伤 hermetic 运行）。",
    )

def pytest_configure(config: Any) -> None:
    configure_logging()


def pytest_sessionstart(session: Any) -> None:
    _session_records.clear()


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    if not _session_records:
        return
    try:
        report_dir = os.getenv(
            "AH_REPORT_DIR", str(session.config.getoption("--ah-report-dir"))
        ) if session.config.getoption("--ah-report-dir") else None
        llm = LLMClient() if llm_enabled() else None
        build_report(_session_records, report_dir=report_dir, llm=llm)
        # 收集报错信息：把本次结果追加进历史库，供下次报告做预测分析。
        try:
            from ah.predict.history import HistoryStore

            base = Path(report_dir or settings.report_dir)
            HistoryStore(base / "history.jsonl").append_all(_session_records)
        except Exception as hexc:  # 历史累积失败不影响本轮报告
            log.warning("history append failed: %s", hexc)
        # R3 CI 判闸（opt-in）：--ah-semantic-gate 时任一语义判定 flagged 即非零退出。
        if session.config.getoption("--ah-semantic-gate"):
            from ah.reporting.writer import _semantic_verdicts_agg

            if _semantic_verdicts_agg(_session_records).get("flagged", 0):
                session.exitstatus = 3  # 语义复核未通过 → 非零出口码
        # Langfuse（v4）：有真实 tracer 用例（本路径保证 _session_records 非空）时，
        # 统一 flush 把缓冲的 spans 真正导出；失败仅告警，绝不中断运行。
        try:
            tracer = get_tracer()
            if getattr(tracer, "enabled", False):
                if exitstatus != 0:
                    tracer.observe_failure(
                        "session-finish",
                        RuntimeError(f"exitstatus={exitstatus}"),
                    )
                tracer.flush()
        except Exception as taut:  # 追踪绝不能打断报告
            log.warning("tracer flush failed: %s", taut)
    except Exception as exc:  # 报告绝不能中断运行
        log.warning("session report failed: %s", exc)


def _filter_cases(cases: list[Case], config: Any) -> list[Case]:
    """按 CLI/环境过滤条件筛选用例（空条件不过滤）。"""
    filt = _case_filters(config)
    if not any(filt.values()):
        return cases
    return select_cases(
        cases,
        env=filt["env"],
        suite=filt["suite"],
        priority=filt["priority"],
        tags=filt["tags"],
        order_by="risk" if _is_regression(filt["suite"]) else None,
    )


def _is_regression(suite: str | None) -> bool:
    """套件条件是否含 regression（'all' 视为含，用于风险优先）。"""
    if not suite:
        return False
    want = {s.strip().lower() for s in suite.split(",") if s.strip()}
    return bool(want.intersection({"regression", "all"}))


def pytest_generate_tests(metafunc: Any) -> None:
    data = _data_dir(metafunc.config)
    if not data:
        return  # 未指定数据目录：不产生业务用例节点（框架单元测试独立运行）
    files = discover_case_files(data)
    if "scenario_steps" in metafunc.fixturenames:
        scenarios = []
        for f in files:
            cases = _filter_cases(load_cases(f), metafunc.config)
            if cases and cases[0].scenario is not None:
                scenarios.append((cases[0].scenario, cases))
        metafunc.parametrize(
            "scenario_steps", [s for _, s in scenarios], ids=[s for s, _ in scenarios]
        )
        return
    if "case" in metafunc.fixturenames:
        single = []
        ids = []
        for f in files:
            for c in _filter_cases(load_cases(f), metafunc.config):
                if c.scenario is None:
                    single.append(c)
                    ids.append(f"{Path(f).stem}:{c.name}")
        if single:
            metafunc.parametrize("case", single, ids=ids)


def _load_prior_history() -> list[dict[str, Any]]:
    """读取 prior 逐运行历史（``report_history.jsonl``），供自愈护栏使用。

    文件不存在或不可读返回空列表（护栏退化为不过滤，保持向后兼容）。
    纯本地文件 IO，hermetic。行结构：``{run_time, cases: {name: result}}``。
    """
    hist_file = Path(settings.report_dir) / "report_history.jsonl"
    if not hist_file.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in hist_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    except (ValueError, OSError):
        log.warning("report history unreadable: %s", hist_file)
    return rows


@pytest.fixture
def ctx() -> Context:
    """每个测试一个全新的 Context；场景步骤通过 flow 共享同一实例。"""
    return Context()


@pytest.fixture
def ah_client() -> Any:
    """由 settings 配置的真实 HTTPClient。测试中可通过 monkeypatch 替换为桩。"""
    return HTTPClient(settings.base_url, trace=get_tracer())


@pytest.fixture
def ah_hooks(ctx, ah_client) -> LLMHooks:  # noqa: ANN001
    """默认启用自愈 hooks：L1 确定重试 + L2 LLM 反思（含失败根因分析）。

    当 LLM 可用（未设 LLMOFF 且有 key）时使用真实 LLM；否则退回确定性的
    canned 回退（零网络）——两者都让失败用例自动进入自愈与根因链。

    L2 自愈自带成功率护栏：读取 prior 逐运行历史（``report_history.jsonl``），
    对长期治不愈（历史样本足够且从未通过）的接口跳过反思，避免盲目重试。
    """
    history_rows = _load_prior_history()
    llm = LLMClient() if llm_enabled() else None
    heal = AgentHeal(
        client=ah_client,
        ctx=ctx,
        llm=llm,
        history_rows=history_rows,
    ).heal
    return LLMHooks(
        triage=AgentTriage(llm=llm).explain_failure,
        heal=heal,
        max_heal_attempts=2,
    )


def test_single_case(case, ctx, ah_client, ah_hooks):  # noqa: ANN001
    """参数化节点：运行单个独立用例并断言成功。"""
    start = time.perf_counter()
    tracer = get_tracer()
    # case 跨度：使本地链路形成 run→case→step→HTTP/LLM 层级；finish 放
    # finally 确保无论成败都弹栈，绝不泄漏 parent 上下文。
    case_span = tracer.start_case(case.name)
    try:
        result = run_single(case, ctx, ah_client, hooks=ah_hooks, tracer=tracer)
    finally:
        case_span.finish(latency_ms=(time.perf_counter() - start) * 1000.0)
    _session_records.append(_record_from_step(result, start))
    if not result.succeeded:
        raise AssertionError(f"case {case.name!r} failed: {result.error}")


def test_scenario_flow(scenario_steps, ctx, ah_client, ah_hooks):  # noqa: ANN001
    """参数化节点：运行完整场景流程，遵循每步的 on_failure。"""
    mark = len(_session_records)
    tracer = get_tracer()

    def execute(step: Case, context: Context) -> Any:
        start = time.perf_counter()
        # 场景里的每个步骤也作为独立的 case 跨度（run→case→step→HTTP/LLM）。
        case_span = tracer.start_case(step.name)
        try:
            result = run_single(step, context, ah_client, hooks=ah_hooks, tracer=tracer)
        finally:
            case_span.finish(latency_ms=(time.perf_counter() - start) * 1000.0)
        return result

    results = execute_flow(scenario_steps, ctx, execute, hooks=ah_hooks)
    _session_records.extend(
        ResultRecord.from_step(result, duration=result.duration) for result in results
    )
    flow_records = _session_records[mark:]
    failed = [result for result in results if not result.succeeded]
    if failed:
        first = failed[0]
        ok_count = sum(1 for result in results if result.succeeded)
        raise AssertionError(
            f"scenario step {first.name!r} failed: {first.reason or first.error} "
            f"({ok_count}/{len(flow_records)} steps ok)"
        )


def _record_from_step(step: Any, start: float) -> ResultRecord:
    """从 StepResult 构建 ResultRecord，附带墙钟耗时。"""
    return ResultRecord.from_step(step, duration=time.perf_counter() - start)
