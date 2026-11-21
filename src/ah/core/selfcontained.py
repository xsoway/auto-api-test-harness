"""项目自包含执行入口的「逐用例日志 + 自动报告」辅助工具。

业务项目 ``projects/<业务>/`` 的 ``test_<业务>.py`` 用 ``run_single`` /
``run_flow`` 直接驱动用例时，不经 pytest 插件参数化节点，因此
``pytest_sessionfinish`` 不会自动落盘报告（见
``skills/auto-api-test-harness/references/reports.md``「项目自包含执行入口的报告」）。
在项目侧（``conftest.py`` + 执行入口）接入 :class:`AutoReporter` 补齐两件事：

1. **逐用例详细日志**：每条用例一行日志（时间、用例名、结果 PASS/FAIL/SKIP、
   状态码、耗时），追加写 ``<项目 report/>run.log`` 并在控制台同步输出。
2. **跑完自动生成报告**：会话结束时把本轮用例收集为 :class:`ResultRecord`
   列表，用 ``reporting.writer`` 的 ``build_report``（``report.md`` +
   ``report.json``）+ ``build_html_report``（自包含单文件 ``report.html``）
   写落到项目 ``report/``。

用法（项目 conftest.py 接入）
------------------------------
.. code-block:: python

    from ah.core.selfcontained import AutoReporter

    _REPORTER_KEY = "run_reporter"  # 放 config.stash，按会话隔离


    def pytest_sessionstart(session) -> None:
        session.config.stash[_REPORTER_KEY] = AutoReporter(
            Path(__file__).resolve().parent / "report"
        )


    def pytest_sessionfinish(session, exitstatus) -> None:
        rep = session.config.stash.get(_REPORTER_KEY)
        if rep is not None:
            rep.write_reports()   # 跑完自动生成报告


    @pytest.fixture
    def run_reporter(request) -> AutoReporter:
        return request.session.config.stash[_REPORTER_KEY]

执行入口每驱动一个用例后记录
------------------------------
.. code-block:: python

    def test_case(run_reporter, ...):
        result = run_single(case, ctx, client)
        run_reporter.record(result, start=start)   # 记入报告 + 逐用例日志

:class:`AutoReporter` 只做收集与落盘，不参与用例执行、不影响测试结果与退出码。
"""

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ah.llm.openai import LLMClient, llm_enabled
from ah.reporting.writer import (
    ResultRecord,
    build_html_report,
    build_report,
)

_DEFAULT_LOG_NAME = "ah.selfcontained"


def _laya_report_gate():
    """取进程级 LayaGate 供报告严重度分流；AH_LAYA=off/未设 → None（字节等价）。"""
    from ah.evidence.laya_gate import get_gate

    return get_gate()


class AutoReporter:
    """收集一次项目运行里的用例结果，会话结束自动写日志 + 报告。

    实例按项目会话隔离（存到 ``session.config.stash``），避免多项目同仓跑时
    记录串扰。日志与报告都落在 ``report_dir`` 下：

    - ``run.log``：逐用例详细日志（追加，跨多次运行累积）。
    - ``report.md`` / ``report.json``：确定性 markdown + 机器可读 json。
    - ``report.html``：自包含单文件 HTML 报告。

    默认固定写 ``report.*``（多次运行互相覆盖）。传 ``report_basename``（如
    ``"test-tcc-m-api"``）后，本轮报告写为
    ``{report_basename}-{YYYYmmdd_HHMMSS_mmm}.{md,json,html}``（时间戳带微秒，
    取运行启动时刻），**每次运行独立留档、互不覆盖**；此外把最新一轮镜像到固定
    ``report.{md,json,html}``（canonical 当前报告），保证 `report.*` 始终是最新结果。
    ``run.log`` 仍追加累积。
    """

    def __init__(
        self,
        report_dir: str | Path,
        *,
        log_name: str = _DEFAULT_LOG_NAME,
        console: bool = True,
        report_basename: Optional[str] = None,
    ) -> None:
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        # 本轮报告文件名主干：指定 report_basename 时带运行启动时间戳，避免覆盖。
        self._run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._report_basename = report_basename
        self.results: list[ResultRecord] = []
        # 逐用例日志：独立 logger（不进框架 ah.log），写 run.log + 可选控制台。
        self._log = logging.getLogger(log_name)
        self._log.setLevel(logging.INFO)
        self._log.propagate = False  # 避免冒泡到根 logging 造成重复输出
        _install_handlers(self._log, self.report_dir / "run.log", console=console)


    # ---- 记录 ---------------------------------------------------------------

    def record(self, step: Any, *, start: Optional[float] = None) -> ResultRecord:
        """收集一步结果并写一条逐用例日志；返回对应 :class:`ResultRecord`。

        ``step`` 是 ``ah.core.orchestrator.StepResult``（或具
        ``succeeded``/``name``/``response`` 属性的对象）；``start`` 为该步启动
        时的 ``time.perf_counter()``，用于计算墙钟耗时（缺省按 0.0）。
        """
        duration = (time.perf_counter() - start) if start is not None else 0.0
        record = ResultRecord.from_step(step, duration=duration)
        self.results.append(record)
        self.log_case(record)
        return record

    def consume_test_report(self, report: Any) -> None:
        """把一个 pytest 用例节点（``TestReport``）记入本轮日志 + 报告。

        供 conftest 的 ``pytest_runtest_logreport`` 钩子调用，用于**没有 step 级
        巡检驱动**的项目（如纯数据护栏 / 流程解析的 ``test_*.py``）：把每个
        ``when == "call"`` 的测试节点当作一条用例记录
        （PASS/FAIL/SKIP + 耗时），status_code 不可得时在日志显示 ``-``。

        与 :meth:`record` 互补：带 ``run_single``/``run_flow`` 巡检的项目应优先
        用 :meth:`record`（step 粒度、含真实状态码），本方法只在节点级作 fallback，
        两者重复记录时靠 ``name``（step 名 vs nodeid）区分。
        """
        if getattr(report, "when", None) != "call":
            return  # 只记执行主体一次，setup/teardown/setup_module 等不记
        if report.passed:
            result = "PASS"
        elif report.failed:
            result = "FAIL"
        elif report.skipped:
            result = "SKIP"
        else:
            result = "ERROR"
        failure = None
        if result != "PASS":
            failure = _failure_text(report)
        record = ResultRecord(
            name=report.nodeid,
            result=result,
            duration=float(getattr(report, "duration", 0.0) or 0.0),
            failure_msg=failure,
        )
        self.results.append(record)
        self.log_case(record)

    def log_case(self, record: ResultRecord) -> None:
        """手动追加一条逐用例日志（不收集进报告）。"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        status = _status_text(record)
        msg = (
            f"[{ts}] {record.result:<4} | {record.name} "
            f"| 状态码={status} | {record.duration * 1000:.0f}ms"
        )
        if record.failure_msg:
            msg += f" | {record.failure_msg[:200]}"
        self._log.info(msg)

    # ---- 落盘 ---------------------------------------------------------------

    def write_reports(
        self,
        *,
        trace_urls: Optional[dict[str, str]] = None,
        run_meta: Optional[dict[str, str]] = None,
    ) -> Path:
        """把本轮收集的用例写成报告，返回 md 路径；无记录则跳过。

        ``report_basename`` 指定时，本轮报告写为
        ``{report_basename}-{YYYYmmdd_HHMMSS}.{md,json,html}``（不覆盖历史）；
        未指定则沿用例写固定 ``report.*``。报告任何一步失败只打 warning、
        绝不抛异常（报告不能中断运行），与框架 ``pytest_sessionfinish`` 的约定
        一致。
        """
        if not self.results:
            self._log.info("无用例记录，跳过报告生成")
            return self.report_dir / "report.md"
        basename = (
            f"{self._report_basename}-{self._run_stamp}"
            if self._report_basename
            else "report"
        )
        report_dir = str(self.report_dir)
        # 启用 LLM 时注入实时分析（运行观察 / 预测分析建议）；关闭时确定性回退。
        llm = LLMClient() if llm_enabled() else None
        md = build_report(
            self.results,
            trace_urls=trace_urls,
            report_dir=report_dir,
            basename=basename,
            llm=llm,
        )
        try:
            build_html_report(
                self.results,
                report_dir=report_dir,
                run_meta=run_meta,
                basename=basename,
                llm=llm,
                gate=_laya_report_gate(),
            )
        except Exception as exc:  # HTML 可选项，失败不影响 md/json
            self._log.warning("HTML 报告生成失败: %s", exc)
        # 按时间戳留档时，同时镜像最新到固定 report.*（canonical「当前报告」），
        # 避免 README 习惯指向的 report.md/report.html 长期停留在旧留档、
        # 看不到新能力段（如框架能力矩阵）。
        if self._report_basename:
            for ext in ("md", "json", "html"):
                _src = self.report_dir / f"{basename}.{ext}"
                if _src.exists():
                    shutil.copyfile(_src, self.report_dir / f"report.{ext}")
        return Path(md)


def _status_text(record: ResultRecord) -> str:
    """从记录取状态码：优先 response 元数据，否则返回 '-'。"""
    if isinstance(record.response, dict):
        code = record.response.get("status_code")
        if code is not None:
            return str(code)
    return "-"


def _failure_text(report: Any) -> Optional[str]:
    """从 pytest TestReport 提取短失败摘要（供节点级记录的 failure_msg）。"""
    longrepr = getattr(report, "longrepr", None)
    if longrepr is None:
        return None
    text = str(longrepr).strip()
    if not text:
        text = getattr(report, "outcome", "FAIL")
    return text[:200] or None


def _install_handlers(logger: logging.Logger, path: Path, *, console: bool) -> None:
    """幂等地给 logger 挂文件 + 可选控制台 handler，避免重复添加。"""
    existing = {h.get_name() for h in logger.handlers}
    fmt = logging.Formatter("%(message)s")
    if "runfile" not in existing:
        fh = logging.FileHandler(path, encoding="utf-8", delay=True)
        fh.set_name("runfile")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    if console and "runconsole" not in existing:
        ch = logging.StreamHandler()
        ch.set_name("runconsole")
        ch.setFormatter(fmt)
        logger.addHandler(ch)
