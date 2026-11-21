"""YAML 用例元数据（env/priority/suite/tags）与快速分类选择验证。"""

from __future__ import annotations

from ah.harness.loader import Case, _expand_env, load_cases, select_cases


def _case(
    name: str,
    *,
    env: str = "all",
    priority: str = "P2",
    suite: str = "regression",
    tags: list[str] | None = None,
) -> Case:
    return Case(
        name=name,
        request={"method": "GET", "path": f"/{name}"},
        env=env,
        priority=priority,
        suite=suite,
        tags=tags or [],
    )


def test_expand_env_comma_list() -> None:
    assert _expand_env("test, prod") == {"test", "prod"}
    assert _expand_env("") == set()
    assert _expand_env(None) == set()


def test_select_by_suite() -> None:
    cases = [_case("b", suite="smoke"), _case("c", suite="regression")]
    assert [c.name for c in select_cases(cases, suite="smoke")] == ["b"]


def test_case_rank() -> None:
    assert _case("a", priority="P0").rank == 0
    assert _case("b", priority="P1").rank == 1
    assert _case("c", priority="P2").rank == 2
    assert _case("d", priority="P5").rank == 2  # 未知按 P2


def test_select_by_env_all_matches_any() -> None:
    cases = [_case("all-kind", env="all"), _case("test-kind", env="test")]
    got = select_cases(cases, env="prod")
    assert [c.name for c in got] == ["all-kind"]  # all 恒匹配，test 不匹配 prod

def test_select_by_env_all_target_matches_any() -> None:
    # 目标 env='all'（通配）应匹配任意环境用例（含具体 env 的用例）。
    cases = [
        _case("all-kind", env="all"),
        _case("test-kind", env="test"),
        _case("prod-kind", env="prod"),
    ]
    got = select_cases(cases, env="all")
    assert [c.name for c in got] == ["all-kind", "test-kind", "prod-kind"]


def test_select_by_priority_and_tags_are_any_match() -> None:
    cases = [
        _case("p0b", priority="P0", tags=["auth", "payment"]),
        _case("p1b", priority="P1", tags=["read"]),
        _case("p0tag", priority="P0", tags=["payment"]),
    ]
    assert [c.name for c in select_cases(cases, priority="P0")] == ["p0b", "p0tag"]
    assert [c.name for c in select_cases(cases, tags=["payment"])] == [
        "p0b",
        "p0tag",
    ]


def test_select_multiple_dims_are_and() -> None:
    cases = [
        _case("ok", env="test", suite="smoke", priority="P0"),
        _case("bad-suite", env="test", suite="regression", priority="P0"),
        _case("bad-env", env="prod", suite="smoke", priority="P0"),
    ]
    got = select_cases(cases, env="test", suite="smoke", priority="P0")
    assert [c.name for c in got] == ["ok"]


def test_select_no_filter_returns_all() -> None:
    cases = [_case("a"), _case("b")]
    assert select_cases(cases) == cases


def test_load_cases_populates_metadata(tmp_path) -> None:
    p = tmp_path / "cases.yaml"
    p.write_text(
        "name: smoke-check\n"
        "request:\n  method: GET\n  path: /health\n"
        "env: test,prod\n"
        "priority: P0\n"
        "suite: smoke\n"
        "tags: [health, fast]\n",
        encoding="utf-8",
    )
    (case,) = load_cases(p)
    assert case.env == "test,prod"
    assert case.envs == {"test", "prod"}
    assert case.priority == "P0"
    assert case.suite == "smoke"
    assert case.tags == ["health", "fast"]


def test_load_cases_defaults() -> None:
    c = _case("d")
    assert c.env == "all" and c.suite == "regression" and c.priority == "P2"


def test_select_order_by_risk_sorts_high_risk_first(monkeypatch) -> None:
    import ah.predict.history as history_mod

    # 伪造历史库：beta 失败率高（高风险）、alpha 稳定通过（低风险）。
    fake_rows = [
        {"interface": "beta", "result": "FAIL", "failure_class": "schema"},
        {"interface": "beta", "result": "FAIL", "failure_class": "schema"},
        {"interface": "beta", "result": "FAIL", "failure_class": "status"},
        {"interface": "alpha", "result": "PASS"},
        {"interface": "alpha", "result": "PASS"},
        {"interface": "alpha", "result": "PASS"},
    ]
    monkeypatch.setattr(history_mod.HistoryStore, "load", lambda self: fake_rows)

    cases = [_case("alpha"), _case("beta"), _case("gamma")]
    got = select_cases(cases, order_by="risk")
    # beta 高风险在前；gamma 无历史记录保持原相对顺序靠后。
    assert [c.name for c in got] == ["beta", "alpha", "gamma"]
