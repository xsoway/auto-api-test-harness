"""插件分类过滤（_case_filters / _filter_cases）的 hermetic 验证。

用轻量 config 桩驱动，不依赖真实 pytest 会话收集，聚焦过滤接线：
CLI/环境变量条件 → select_cases → 返回子集。
"""

from __future__ import annotations

from ah.core import plugin
from ah.harness.loader import Case, select_cases


class _Cfg:
    def __init__(self, **opts) -> None:
        self._opts = opts

    def getoption(self, name: str):
        return self._opts.get(name)


def test_case_filters_read_cli_when_env_absent(monkeypatch) -> None:
    cfg = _Cfg(
        **{"--ah-env": "test", "--ah-suite": "smoke",
           "--ah-priority": None, "--ah-tag": None}
    )
    for var in ("AH_ENV", "AH_SUITE", "AH_PRIORITY", "AH_TAG"):
        monkeypatch.delenv(var, raising=False)
    f = plugin._case_filters(cfg)
    assert f["env"] == "test" and f["suite"] == "smoke"


def test_case_filters_cli_overrides_env(monkeypatch) -> None:
    monkeypatch.setenv("AH_ENV", "prod")
    cfg = _Cfg(
        **{"--ah-env": "test", "--ah-suite": None,
           "--ah-priority": None, "--ah-tag": None}
    )
    f = plugin._case_filters(cfg)
    assert f["env"] == "test"  # 显式 CLI 优先于环境变量


def test_case_filters_tag_splits_comma(monkeypatch) -> None:
    monkeypatch.setenv("AH_TAG", "auth,payment")
    cfg = _Cfg(**{"--ah-env": None, "--ah-suite": None, "--ah-priority": None, "--ah-tag": None})
    f = plugin._case_filters(cfg)
    assert f["tags"] == ["auth", "payment"]


def _case(name: str, **kw) -> Case:
    base = {"request": {"method": "GET", "path": f"/{name}"}}
    base.update(kw)
    return Case(name=name, **base)


def test_filter_cases_no_filter_keeps_all(monkeypatch) -> None:
    monkeypatch.delenv("AH_ENV", raising=False)
    monkeypatch.delenv("AH_SUITE", raising=False)
    monkeypatch.delenv("AH_PRIORITY", raising=False)
    monkeypatch.delenv("AH_TAG", raising=False)
    cfg = _Cfg(**{"--ah-env": None, "--ah-suite": None, "--ah-priority": None, "--ah-tag": None})
    cases = [_case("a"), _case("b")]
    assert plugin._filter_cases(cases, cfg) == cases


def test_filter_cases_applies_suite(monkeypatch) -> None:
    monkeypatch.delenv("AH_SUITE", raising=False)
    cfg = _Cfg(**{"--ah-env": None, "--ah-suite": "smoke", "--ah-priority": None, "--ah-tag": None})
    cases = [_case("a", suite="smoke"), _case("b", suite="regression")]
    got = plugin._filter_cases(cases, cfg)
    assert [c.name for c in got] == ["a"]


def test_select_cases_and_smoke_regression_focus() -> None:
    cases = [
        _case("s1", env="test,prod", suite="smoke", priority="P0"),
        _case("s2", env="test", suite="smoke", priority="P1"),
        _case("r1", env="prod", suite="regression", priority="P0"),
    ]
    smoke_test = select_cases(cases, env="test", suite="smoke")
    assert [c.name for c in smoke_test] == ["s1", "s2"]
    prod_p0 = select_cases(cases, env="prod", suite="regression", priority="P0")
    assert [c.name for c in prod_p0] == ["r1"]


class _FakeTracer:
    """最小 fake：记录调用，供 sessionfinish flush 接线验证。"""

    enabled = True

    def __init__(self) -> None:
        self.flush_calls = 0
        self.failures = []

    def observe_failure(self, name: str, exc: BaseException) -> None:
        self.failures.append((name, exc))

    def flush(self) -> None:
        self.flush_calls += 1


class _Session:
    """session 桩：应答 plugin 读到的两个 option（其余返回 None）。"""

    def __init__(self, semantic_gate: bool = False) -> None:
        self._semantic_gate = semantic_gate
        self.exitstatus = 0
        self.config = _Cfg(
            **{"--ah-report-dir": None, "--ah-semantic-gate": semantic_gate}
        )

    def getoption(self, name: str):
        return self.config.getoption(name)


def test_sessionfinish_flushes_tracer_when_enabled(
    monkeypatch,
) -> None:
    """tracer enabled 且有用例记录时，sessionfinish 必须 flush（v4 落盘关键）。"""
    fake = _FakeTracer()
    monkeypatch.setattr("ah.core.plugin.get_tracer", lambda: fake)
    monkeypatch.setattr("ah.core.plugin.build_report", lambda *a, **k: None)
    monkeypatch.setattr("ah.core.plugin.LLMClient", lambda: None)
    monkeypatch.setattr("ah.core.plugin.llm_enabled", lambda: False)
    # 注入一条记录，使 sessionfinish 走到报告 + flush 分支。
    plugin._session_records.append(object())
    try:
        plugin.pytest_sessionfinish(_Session(), 0)
        assert fake.flush_calls == 1
        assert fake.failures == []
    finally:
        plugin._session_records.clear()


def test_sessionfinish_flush_omits_failure_on_success(monkeypatch) -> None:
    fake = _FakeTracer()
    monkeypatch.setattr("ah.core.plugin.get_tracer", lambda: fake)
    monkeypatch.setattr("ah.core.plugin.build_report", lambda *a, **k: None)
    monkeypatch.setattr("ah.core.plugin.LLMClient", lambda: None)
    monkeypatch.setattr("ah.core.plugin.llm_enabled", lambda: False)
    plugin._session_records.append(object())
    try:
        plugin.pytest_sessionfinish(_Session(), 0)
        assert fake.failures == []  # 成功退出不下发失败 span
    finally:
        plugin._session_records.clear()


def test_sessionfinish_reports_failure_when_exitstatus_nonzero(monkeypatch) -> None:
    fake = _FakeTracer()
    monkeypatch.setattr("ah.core.plugin.get_tracer", lambda: fake)
    monkeypatch.setattr("ah.core.plugin.build_report", lambda *a, **k: None)
    monkeypatch.setattr("ah.core.plugin.LLMClient", lambda: None)
    monkeypatch.setattr("ah.core.plugin.llm_enabled", lambda: False)
    plugin._session_records.append(object())
    try:
        plugin.pytest_sessionfinish(_Session(), 3)
        assert fake.flush_calls == 1
        assert fake.failures and fake.failures[0][0] == "session-finish"
    finally:
        plugin._session_records.clear()
