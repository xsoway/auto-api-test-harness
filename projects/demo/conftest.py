"""demo 业务项目（框架演示）pytest 配置。

demo 是框架自带的**参考模板**，演示一种业务项目自包含的标准结构和三种用例形态
（单接口 / 场景流程 / 分类元数据），不指向真实被测服务——全部测试用桩传输隔离
驱动，避免污染真实环境。

本项目不注册 ``--run-live``（无真实服务目标）。会话结束经
:class:`ah.core.selfcontained.AutoReporter` 自动生成逐用例日志（``report/run.log``）
与报告（``report.md``/``report.json``/``report.html``），报告目录兜底指向本项目
``report/``，与其它项目/框架报告隔离。

新增业务项目时复制 ``projects/demo/`` 的结构（``data/`` + ``test_<业务>.py`` +
``conftest.py`` + ``report/`` + ``README``），替换数据并改执行入口即可。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pytest

from ah.core.selfcontained import AutoReporter

_PROJECT_ROOT = Path(__file__).resolve().parent
_REPORT_DIR = _PROJECT_ROOT / "report"
# 本项目目录前缀，用于过滤 pytest_runtest_logreport 的节点，避免同仓多项目
# 共跑时把其它项目的 test 误记进本项目报告。
_NODE_PREFIX = "projects/demo/"
# 会话内持有的 AutoReporter 实例（test 回调拿不到 session/config，用模块级全局）。
_REPORTER: Optional[AutoReporter] = None


def pytest_configure(config: pytest.Config) -> None:
    """报告目录兜底指向本项目 report/，避免与其它项目/框架报告混淆。"""
    os.environ.setdefault("AH_REPORT_DIR", str(_REPORT_DIR))
    del config  # 保持 pytest 8 兼容的显式签名（此处仅设置环境，不读配置）


def pytest_sessionstart(session: pytest.Session) -> None:
    """建本项目 AutoReporter：逐用例日志 + 会话结束自动写报告到 report/。"""
    global _REPORTER
    _REPORTER = AutoReporter(_REPORT_DIR, log_name="ah.selfcontained.demo")
    del session


def pytest_runtest_logreport(report) -> None:
    """把本项目每个跳过/失败/通过的用例节点记入 run.log + 报告。"""
    if _REPORTER is not None and report.nodeid.startswith(_NODE_PREFIX):
        _REPORTER.consume_test_report(report)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """跑完自动生成报告；无记录或失败都不中断。"""
    global _REPORTER
    if _REPORTER is not None:
        _REPORTER.write_reports()
        _REPORTER = None
    del session, exitstatus