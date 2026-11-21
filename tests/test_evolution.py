"""WS1 自主涌现闭环 hermetic 验证：持久化追加/去重/undo、门控拒绝低分、
多轮停车、趋势闸判升档、LLM off 下空转不崩。

全部测试在 ``LLMOFF=1`` 下确定性运行，绝不触碰网络：要么走纯本地 helper，
要么用 :class:`~ah.evolution.AutoEvolutionLoop` 注入的确定性候选工厂 + hermetic
客户端（MockTransport）。
"""

from __future__ import annotations

from pathlib import Path

from ah.evolution import (
    EVOLUTION_YAML,
    EVOLVED_SUITE,
    EVOLVED_TAG,
    AutoEvolutionLoop,
    escalate_suite,
    evaluate_trend,
    gate_candidates,
    gen_candidates_hermetic,
    heuristic_quality,
    interfaces_for_paths,
    make_issue_draft,
    persist_evolution,
    quality_score_case,
    undo_evolution,
    write_issues,
)
from ah.harness.loader import Case, load_cases
from ah.predict.generator import GeneratedCase


def _cand(
    name: str,
    path: str,
    *,
    method: str = "GET",
    params: dict | None = None,
    assert_act: dict | None = None,
) -> GeneratedCase:
    return GeneratedCase(
        name=name,
        request={"method": method, "path": path, "params": params or {"limit": 1}},
        assert_=dict(assert_act or {"status": 200}),
        validated=False,
    )


# ── 持久化（feature 2）：追加 / 去重 / undo ─────────────────────────

def test_persist_appends_with_markers_and_metadata(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    cand = _cand("evolved-a", "/users", params={"limit": 0})
    written, skipped = persist_evolution(data, [cand], round_no=1, env="test")
    assert written and not skipped

    evo = data / EVOLUTION_YAML
    text = evo.read_text(encoding="utf-8")
    assert "# --- ah-evolved round 1 ---" in text

    cases = load_cases(str(evo))
    assert len(cases) == 1
    case = cases[0]
    assert case.name == "evolved-a"
    assert case.request_path == "/users"
    # 元数据：tags / suite / env 都被写回并可被 loader 读回。
    assert EVOLVED_TAG in case.tags
    assert case.suite == EVOLVED_SUITE
    assert case.env == "test"


def test_persist_round_append_keeps_prior_rounds(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    persist_evolution(data, [_cand("r1-a", "/a")], round_no=1, env="all")
    persist_evolution(
        data, [_cand("r2-b", "/b"), _cand("r2-c", "/c")], round_no=2, env="all"
    )
    cases = load_cases(str(data / EVOLUTION_YAML))
    assert {c.name for c in cases} == {"r1-a", "r2-b", "r2-c"}
    text = (data / EVOLUTION_YAML).read_text(encoding="utf-8")
    assert "# --- ah-evolved round 1 ---" in text
    assert "# --- ah-evolved round 2 ---" in text


def test_persist_dedups_against_existing_cases(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    existing = [
        Case(name="existing-x", request={"method": "GET", "path": "/x"})
    ]
    # 与已加载既有用例 (method,path,name) 重合 → 不写。
    dup = _cand("existing-x", "/x", method="GET")
    written, skipped = persist_evolution(
        data, [dup], round_no=1, env="all", existing=existing
    )
    assert written == [] and len(skipped) == 1


def test_persist_dedups_within_same_round(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    same = _cand("same", "/s")
    written, _ = persist_evolution(data, [same, same], round_no=1, env="all")
    assert len(written) == 1  # 同轮内二次提交去重
    assert len(load_cases(str(data / EVOLUTION_YAML))) == 1


def test_undo_removes_persisted_evolution(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    persist_evolution(
        data, [_cand("keep", "/keep"), _cand("drop", "/drop")], round_no=1, env="all"
    )
    removed = undo_evolution(data, ["drop"])
    assert removed == 1
    cases = load_cases(str(data / EVOLUTION_YAML))
    assert [c.name for c in cases] == ["keep"]

def test_undo_empties_and_removes_evolution_file(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    persist_evolution(data, [_cand("only", "/only")], round_no=1, env="all")
    removed = undo_evolution(data, ["only"])
    assert removed == 1
    assert not (data / EVOLUTION_YAML).exists()  # 清空即删除


# ── 质量护栏（feature 4）─────────────────────────────────────────

def test_heuristic_quality_rejects_no_assert_case() -> None:
    case = Case(name="weak", request={"method": "GET", "path": "/w"})
    assert heuristic_quality(case) < 0.6


def test_heuristic_quality_accepts_assert_with_params() -> None:
    case = Case(
        name="strong",
        request={"method": "GET", "path": "/s", "params": {"limit": 10}},
        assert_={"status": 200, "jsonpath": "$.ok == true"},
    )
    assert heuristic_quality(case) >= 0.6


def test_gate_rejects_low_quality_and_records_reason() -> None:
    weak = GeneratedCase(name="bare", request={"method": "GET", "path": "/b"}, assert_={})
    strong = _cand("good", "/g", params={"limit": 1}, assert_act={"status": 200})
    judged, accepted = gate_candidates([weak, strong], min_quality=0.6, llm=None)
    assert [c.name for c in accepted] == ["good"]
    rejected = {g.candidate.name for g in judged if not g.accepted}
    assert rejected == {"bare"}
    rej = next(g for g in judged if not g.accepted)
    assert "quality_below" in rej.reason
    assert rej.score < 0.6


def test_gate_respects_custom_min_quality() -> None:
    weak = GeneratedCase(name="bare", request={"method": "GET", "path": "/b"}, assert_={})
    judged, accepted = gate_candidates([weak], min_quality=0.0, llm=None)
    assert accepted  # 阈值降到 0 → 不拒绝


def test_quality_score_case_hermetic_is_deterministic() -> None:
    cand = _cand("x", "/x", params={"limit": 1})
    s1 = quality_score_case(cand, llm=None)
    s2 = quality_score_case(cand, llm=None)
    assert s1 == s2 and 0.0 <= s1 <= 1.0


# ── LayaGate 质量分流埋点（P1）───────────────────────────────

class _FakeGate:
    """注入 ``gate.best`` 行为的假闸门（hermetic，零网络零真模型）。"""

    def __init__(self, action: str, value: float | None = None) -> None:
        self.calls = 0
        self._action = action
        self._value = value

    def best(self, stage: str, state: str, questions: dict) -> dict:
        self.calls += 1
        return {
            "action": self._action,
            "value": self._value,
            "confidence": 0.99,
            "advisory_only": False,
            "signal": {"quality": {"value": self._value}},
        }


def test_gate_none_skips_laya_and_is_byte_identical() -> None:
    """gate=None（默认/off）→ 完全不触碰 gate，行为与原先一致。"""
    fake = _FakeGate("auto", value=0.9)
    weak = GeneratedCase(name="bare", request={"method": "GET", "path": "/b"}, assert_={})
    strong = _cand("ok", "/o", params={"limit": 5})
    judged, accepted = gate_candidates([weak, strong], min_quality=0.6, llm=None)
    assert fake.calls == 0  # 埋点完全跳过
    assert [g.accepted for g in judged] == [False, True]
    assert [g.reason for g in judged] == [f"quality_below_{0.6}", "quality_ok"]
    assert [c.name for c in accepted] == ["ok"]
    assert all(g.signal == {} for g in judged)  # 未触发 → 无 Laya 信号


def test_gate_auto_accept_skips_original_scorer() -> None:
    """高置信 AUTO + 归一化 value>=min_quality → 采纳，reason 记 laya_quality_ok，Scorer 不调用。"""
    fake = _FakeGate("auto", value=2.0)  # 0-based 最高带（3 带 → /2 → 1.0）
    called: list[str] = []

    def scorer(cand) -> float:
        called.append(cand.name)
        return 0.5  # 若被调用本应拒绝，用于证明被跳过

    cand = _cand("laya_strong", "/s")
    judged, accepted = gate_candidates([cand], min_quality=0.6, llm=None, scorer=scorer, gate=fake)
    assert fake.calls == 1
    assert called == []  # 原评分未被调用
    assert accepted == [cand]
    assert judged[0].accepted is True
    assert judged[0].reason == "laya_quality_ok"
    assert judged[0].score == 1.0  # 2.0/2 归一化
    assert judged[0].signal == {"quality": {"value": 2.0}}


def test_gate_auto_reject_below_threshold() -> None:
    """高置信 AUTO + 归一化 value<min_quality → 拒绝，reason 记 laya_quality_below。"""
    fake = _FakeGate("auto", value=0.3)  # 0-based 低带（→ /2 → 0.15）
    cand = _cand("laya_weak", "/w")
    judged, accepted = gate_candidates([cand], min_quality=0.6, llm=None, gate=fake)
    assert fake.calls == 1
    assert accepted == []
    assert judged[0].accepted is False
    assert judged[0].reason == "laya_quality_below"
    assert judged[0].score == 0.15  # 0.3/2 归一化




def test_gate_low_confidence_falls_back_to_original() -> None:
    """低置信（未开闸/ESCAPE）→ 走原 quality_score_case 逻辑。"""
    fake = _FakeGate("escalate", value=None)
    called: list[str] = []

    def scorer(cand) -> float:
        called.append(cand.name)
        return 0.8

    cand = _cand("fallback", "/f")
    judged, accepted = gate_candidates([cand], min_quality=0.6, llm=None, scorer=scorer, gate=fake)
    assert fake.calls == 1
    assert called == ["fallback"]  # 原评分被调用
    assert judged[0].accepted is True
    assert judged[0].reason == "quality_ok"
    assert accepted == [cand]


def test_gate_auto_value_always_numeric() -> None:
    """auto 但 value 非数值（脏数据）→ 跳过 Laya 判决，回退原逻辑。"""
    fake = _FakeGate("auto", value="high")  # 非数值
    called: list[str] = []

    def scorer(cand) -> float:
        called.append(cand.name)
        return 0.7

    cand = _cand("dirty", "/d")
    judged, accepted = gate_candidates([cand], min_quality=0.6, llm=None, scorer=scorer, gate=fake)
    assert fake.calls == 1
    assert called == ["dirty"]
    assert judged[0].reason == "quality_ok"
    assert accepted == [cand]


def test_gate_exception_falls_back_and_does_not_raise() -> None:
    """gate.best 抛异常 → 捕获并回退原逻辑，绝不外抛。"""
    class _BoomGate:
        def best(self, stage, state, questions):
            raise RuntimeError("boom")

    cand = _cand("boom", "/b")
    judged, accepted = gate_candidates([cand], min_quality=0.6, llm=None, gate=_BoomGate())
    # 回退后走原启发式（status 200 → 质量 ≥ 0.6 → 采纳），异常被吞不外抛。
    assert accepted == [cand]
    assert judged[0].accepted is True
    assert judged[0].reason == "quality_ok"


# ── 多轮停车（feature 1）─────────────────────────────────────────

def _loop_dir(tmp_path: Path) -> tuple[Path, Path]:
    proj = tmp_path / "proj"
    data = proj / "data"
    data.mkdir(parents=True)
    return proj, data


def test_loop_stops_on_no_new_candidate(tmp_path: Path) -> None:
    proj, data = _loop_dir(tmp_path)
    queue = iter([[_cand("ev-a", "/a")], []])

    def factory(cases, history):
        return next(queue, [])

    loop = AutoEvolutionLoop(
        data, proj, rounds=10, budget=100, candidate_factory=factory, run_suite=False
    )
    report = loop.run()
    # round1 采纳 1 条，round2 无候选 → 停车。
    assert len(report.rounds) == 2
    assert report.stop_reason == "no_new_candidate_adopted"
    assert report.total_accepted == 1
    assert report.rounds[0].persisted == 1
    assert report.rounds[1].persisted == 0
    evo = data / EVOLUTION_YAML
    assert evo.is_file() and len(load_cases(str(evo))) == 1


def test_loop_stops_on_max_rounds(tmp_path: Path) -> None:
    proj, data = _loop_dir(tmp_path)
    counter = {"n": 0}

    def factory(cases, history):
        counter["n"] += 1
        return [_cand(f"a-{counter['n']}", "/a")]

    loop = AutoEvolutionLoop(
        data, proj, rounds=2, budget=1000, candidate_factory=factory, run_suite=False
    )
    report = loop.run()
    assert report.stop_reason.startswith("max_rounds")
    assert len(report.rounds) == 2


def test_loop_stops_on_budget_exhausted(tmp_path: Path) -> None:
    proj, data = _loop_dir(tmp_path)
    n = {"c": 0}

    def factory(cases, history):
        n["c"] += 1
        return [_cand(f"budget-{n['c']}", "/b")]

    loop = AutoEvolutionLoop(
        data, proj, rounds=10, budget=2, candidate_factory=factory, run_suite=False
    )
    report = loop.run()
    assert "budget" in report.stop_reason
    assert report.total_accepted == 2  # 预算 2 耗尽即停


def test_loop_hermetic_idles_gracefully(tmp_path: Path) -> None:
    """LLM off、无注入工厂时，空转不崩、日志完整、恰退一次。"""
    import json

    proj, data = _loop_dir(tmp_path)
    loop = AutoEvolutionLoop(data, proj, rounds=3, budget=10, llm=None, run_suite=True)
    report = loop.run()
    assert report.stop_reason == "no_new_candidate_adopted"
    assert report.total_accepted == 0
    # 审计日志已落盘，包含本轮候选/采纳/拒绝记录。
    log_path = proj / "_evolution_log.jsonl"
    assert log_path.is_file()
    entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["round"] == 1
    assert "accepted" in entry and "rejected" in entry


# ── 候选生成 / 变更驱动（feature 3 / hermetic fallback）──────────

def test_gen_candidates_hermetic_idle_without_affected() -> None:
    cases = [Case(name="a", request={"method": "GET", "path": "/a"})]
    assert gen_candidates_hermetic(cases, []) == []


def test_gen_candidates_hermetic_with_affected() -> None:
    cases = [Case(name="a", request={"method": "GET", "path": "/users"})]
    out = gen_candidates_hermetic(cases, [], affected=["/users"])
    assert out and out[0].request["path"] == "/users"
    assert out[0].request.get("params") == {"limit": 0}


def test_interfaces_for_paths_maps_changed_files() -> None:
    cases = [Case(name="users", request={"method": "GET", "path": "/users"})]
    affected = interfaces_for_paths(["src/users.py", "data/users.yaml"], cases)
    assert "/users" in affected


def test_interfaces_for_paths_empty_without_changes() -> None:
    cases = [Case(name="a", request={"method": "GET", "path": "/a"})]
    assert interfaces_for_paths([], cases) == []
    assert interfaces_for_paths(["unrelated.md"], cases) == []


# ── 趋势闸（feature 13）──────────────────────────────────────────

def test_trend_escalates_on_drop() -> None:
    # 基线全 PASS（前 k），近期全 FAIL（后 k）→ 下滑 100% ≥ 20。
    history = [{"result": "PASS"}] * 3 + [{"result": "FAIL"}] * 3
    decision = evaluate_trend(history, drop=20.0, k=3)
    assert decision.escalate is True
    assert decision.decline >= 20.0
    assert decision.recent_rate == 0.0
    assert decision.baseline_rate == 100.0


def test_trend_no_escalation_when_stable() -> None:
    history = [{"result": "PASS"}] * 6
    decision = evaluate_trend(history, drop=20.0, k=3)
    assert decision.escalate is False


def test_trend_insufficient_data_no_decision() -> None:
    history = [{"result": "PASS"}, {"result": "FAIL"}]
    decision = evaluate_trend(history, drop=20.0, k=5)
    assert decision.escalate is False
    assert decision.message == "insufficient_data"


def test_escalate_suite_chain() -> None:
    assert escalate_suite("smoke") == "regression"
    assert escalate_suite("regression") == "prod"
    assert escalate_suite("prod") == "prod"  # 到顶保持


# ── 失败自动动作（feature 11）────────────────────────────────────

def test_make_issue_draft_contains_repro() -> None:
    draft = make_issue_draft(
        {
            "interface": "/payments",
            "name": "pay",
            "result": "FAIL",
            "failure_class": "assertion",
            "failure_msg": "expected 201 got 500",
            "request": {"method": "POST", "path": "/payments", "json": {"amt": 1}},
        },
        index=1,
    )
    assert "/payments" in draft
    assert "201 got 500" in draft
    assert "复现步骤" in draft


def test_write_issues_creates_issue_files(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    paths = write_issues(
        proj, [{"interface": "/x", "name": "x", "result": "FAIL"}]
    )
    assert len(paths) == 1 and paths[0].exists()
    assert "issue-1-" in paths[0].name


def test_write_issues_empty_is_noop(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    assert write_issues(proj, []) == []
