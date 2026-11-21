"""AI 扩展助手（ah.predict.assist）hermetic 验证。"""

from __future__ import annotations

import httpx

from ah.harness.http import HTTPClient
from ah.harness.loader import Case
from ah.llm.mock import MockLLM
from ah.predict.assist import (
    AgentCluster,
    AgentCoverage,
    AgentImpact,
    AgentNL2Case,
    AgentQuality,
    NL2CaseResult,
)


def _case(name: str, path: str = "/x") -> Case:
    return Case(name=name, request={"method": "GET", "path": path})


def _row(name: str, result: str, fclass: str = "unknown", interface: str = "") -> dict:
    return {
        "name": name,
        "result": result,
        "failure_class": fclass if result != "PASS" else None,
        "interface": interface or name,
        "failure_msg": f"{name} failed",
    }


# ── Hermetic（LLM 禁用 → 空/None）────────────────────────────────────────

def test_assist_all_agents_hermetic_llm_none() -> None:
    assert AgentImpact(None).analyze(cases=[_case("a")]) == []
    assert AgentNL2Case(None).to_case("make a case") is None
    assert AgentCluster(None).cluster([_row("a", "FAIL")]) == []
    assert (
        AgentCoverage(None, client=HTTPClient("")).propose([_case("a")]) == []
    )
    q = AgentQuality(None).score([_case("a")])
    assert q == []
    assert AgentQuality(None).score([]) == []


# ── AgentImpact ────────────────────────────────────────────────────────────

def test_impact_parses_interfaces_and_clamps_score() -> None:
    mock = MockLLM()
    mock.set_json_response(
        {
            "interfaces": [
                {
                    "interface": "/x",
                    "affected_assert_paths": ["status_code", "data.id"],
                    "impact_score": 1.7,
                    "rationale": "字段改名",
                },
                {"interface": "/y", "impact_score": "none"},  # 非数字 → 0
                {"interface": ""},  # 空 → 跳过
            ]
        }
    )
    out = AgentImpact(mock).analyze(cases=[_case("a", "/x")])
    assert len(out) == 2
    assert out[0]["interface"] == "/x"
    assert out[0]["affected_assert_paths"] == ["status_code", "data.id"]
    assert out[0]["impact_score"] == 1.0  # 1.7 clamp 到 1.0
    assert out[1]["impact_score"] == 0.0
    assert mock.call_log[0]["task"] == "impact"
    assert mock.call_log[0]["model"] == "strong"


def test_impact_non_list_response_returns_empty() -> None:
    mock = MockLLM()
    mock.set_json_response({"nope": 1})
    assert AgentImpact(mock).analyze() == []

def test_nl2case_returns_reviewable_draft_without_client() -> None:
    mock = MockLLM()
    mock.set_json_response(
        {
            "name": "find-by-id",
            "request": {"method": "GET", "path": "/x/1"},
            "assert": {"status_code": 200},
        }
    )
    res = AgentNL2Case(mock).to_case("查 id=1 的详情", validate=True)
    assert isinstance(res, NL2CaseResult)
    assert res.needs_human_review is True
    assert "human review" in res.review_reason
    assert res.generated.validated is False  # 无 client → 草案
    assert mock.call_log[0]["task"] == "nl2case"


def test_nl2case_validates_when_client_given() -> None:
    client = HTTPClient(
        "",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
        ),
    )
    mock = MockLLM()
    mock.set_json_response(
        {
            "name": "ok-case",
            "request": {"method": "GET", "path": "https://m.example.com/x"},
            "assert": {"status_code": 200},
        }
    )
    res = AgentNL2Case(mock, client=client).to_case("x 应返回 200", validate=True)
    assert isinstance(res, NL2CaseResult)
    assert res.generated.validated is True


def test_nl2case_marks_validation_error_on_failure() -> None:
    client = HTTPClient(
        "",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(500, text="err"))
        ),
    )
    mock = MockLLM()
    mock.set_json_response(
        {
            "name": "bad",
            "request": {"method": "GET", "path": "/x"},
            "assert": {"status_code": 200},
        }
    )
    res = AgentNL2Case(mock, client=client).to_case("x 应返回 200", validate=True)
    assert isinstance(res, NL2CaseResult)
    assert res.generated.validated is False
    assert res.generated.validation_error


def test_nl2case_rejects_missing_path() -> None:
    mock = MockLLM()
    mock.set_json_response({"name": "nopath", "request": {"method": "GET"}})
    assert AgentNL2Case(mock).to_case("x") is None


# ── AgentCluster ──────────────────────────────────────────────────────────

def test_cluster_parses_members_and_drops_empty() -> None:
    histories = [_row("a", "FAIL"), _row("b", "PASS"), _row("c", "FAIL")]
    mock = MockLLM()
    mock.set_json_response(
        {
            "clusters": [
                {"id": "c1", "title": "auth", "member_indices": [0, 1]},
                {"id": "c2", "title": "empty", "member_indices": []},  # 无成员→丢弃
                {"id": "c3", "title": "bad-idx", "member_indices": [99, "x"]},  # 无效→丢弃
            ]
        }
    )
    out = AgentCluster(mock).cluster(histories)
    assert len(out) == 1
    assert out[0]["title"] == "auth"
    assert [m["name"] for m in out[0]["members"]] == ["a", "c"]
    assert mock.call_log[0]["task"] == "cluster"

# ── AgentCoverage ──────────────────────────────────────────────────────────

def test_coverage_only_returns_validated_candidates() -> None:
    client = HTTPClient(
        "",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
        ),
    )
    mock = MockLLM()
    mock.set_json_response(
        {
            "cases": [
                {
                    "name": "valid",
                    "request": {"method": "GET", "path": "https://m.example.com/x"},
                },
                {
                    "name": "also-valid",
                    "request": {"method": "GET", "path": "https://m.example.com/y"},
                },
            ]
        }
    )
    accepted = AgentCoverage(mock, client=client).propose([_case("a", "/x")])
    assert len(accepted) == 2
    assert all(c.validated is True for c in accepted)
    assert mock.call_log[0]["task"] == "coverage"
    assert mock.call_log[0]["model"] == "strong"


def test_coverage_drops_case_without_path() -> None:
    client = HTTPClient(
        "",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(500, text="err"))
        ),
    )
    mock = MockLLM()
    mock.set_json_response(
        {
            "cases": [
                {"name": "nopath", "request": {"method": "GET"}},
                {"name": "fails", "request": {"method": "GET", "path": "/x"}},
            ]
        }
    )
    accepted = AgentCoverage(mock, client=client).propose([_case("a", "/x")])
    # nopath 被跳过；fails 验真失败 → 不进 accepted（但有 validation_error）
    assert len(accepted) == 0


def test_coverage_requires_client_and_existing_cases() -> None:
    mock = MockLLM()
    # 无 client → []
    assert AgentCoverage(mock).propose([_case("a")]) == []
    client = HTTPClient("")
    # 无现有用例 → []
    assert AgentCoverage(mock, client=client).propose([]) == []


# ── AgentQuality ───────────────────────────────────────────────────────────

def test_quality_parses_reviews_and_clamps() -> None:
    mock = MockLLM()
    mock.set_json_response(
        {
            "reviews": [
                {
                    "case": "a",
                    "score": 1.2,
                    "assertion_effectiveness": 0.9,
                    "param_coverage": 0.5,
                    "maintainability": 0.4,
                    "issues": ["assert=status_code only"],
                    "suggestion": "add data.id check",
                },
                {"score": 0.5},  # 缺 case → 跳过
                {
                    "case": "b",
                    "score": -1,
                    "assertion_effectiveness": 0.1,
                    "param_coverage": 0.1,
                    "maintainability": 0.1,
                    "issues": [],
                    "suggestion": "",
                },
            ]
        }
    )
    out = AgentQuality(mock).score([_case("a"), _case("b")])
    assert len(out) == 2
    assert out[0]["case"] == "a"
    assert out[0]["score"] == 1.0  # 1.2 clamp 到 1.0
    assert out[0]["assertion_effectiveness"] == 0.9
    assert out[0]["issues"] == ["assert=status_code only"]
    assert out[1]["score"] == 0.0  # -1 clamp 到 0
    assert mock.call_log[0]["task"] == "quality"
    assert mock.call_log[0]["model"] == "strong"


def test_quality_empty_cases_returns_empty() -> None:
    mock = MockLLM()
    assert AgentQuality(mock).score([]) == []
    assert mock.calls == 0


# ── AgentNL2Case.to_case + LayaGate design 埋点（hermetic fake gate） ──────

def _gen_stub(action="escalate", clear_enough=None, confidence=0.0):
    class _Stub:
        def __init__(self):
            self.calls = 0
        def decide(self, stage, state, questions):
            self.calls += 1
            return type(
                "Dec", (),
                {"action": type("A", (), {"value": action})(), "confidence": confidence,
                 "value": {"clear_enough": clear_enough}},
            )()
    return _Stub()


def test_to_case_default_gate_unchanged():
    """gate 默认 None → 原流程字节等价：llm None 仍返回 None。"""
    assert AgentNL2Case(None).to_case("make a case") is None


def test_to_case_gate_none_llm_disabled_is_none():
    """gate 提供但 llm None：设计环节不短路生成，仍返回 None（生成必须 LLM）。"""
    gate = _gen_stub(action="auto", clear_enough=0.99)
    assert AgentNL2Case(None).to_case("make a case", gate=gate) is None


def test_to_case_gate_auto_signal_recorded():
    """有 LLM + gate AUTO 高置信"清晰"→ review_reason 记层信号，人审护栏不变。"""
    from ah.llm.mock import MockLLM
    mock = MockLLM()
    mock.set_json_response(
        {"name": "n", "request": {"method": "GET", "path": "https://m.example.com/x"}}
    )
    gate = _gen_stub(action="auto", clear_enough=0.97)
    res = AgentNL2Case(mock).to_case("查详情应返回200", gate=gate)
    assert res is not None
    assert "laya design ok" in res.review_reason
    assert res.needs_human_review is True  # 人审护栏不因 Laya 关闭


def test_to_case_gate_escalate_keeps_original_reason():
    """低置信升级 → 走原 LLM + 原 review_reason（不改信号）。"""
    from ah.llm.mock import MockLLM
    mock = MockLLM()
    mock.set_json_response(
        {"name": "n", "request": {"method": "GET", "path": "https://m.example.com/x"}}
    )
    gate = _gen_stub(action="escalate", clear_enough=0.3)
    res = AgentNL2Case(mock).to_case("查详情应返回200", gate=gate)
    assert res is not None
    assert "laya design ok" not in res.review_reason


def test_to_case_gate_backend_error_falls_back():
    """gate 抛错 → 不破坏原流程（有 LLM 仍正常生成）。"""
    from ah.llm.mock import MockLLM
    mock = MockLLM()
    mock.set_json_response(
        {"name": "n", "request": {"method": "GET", "path": "https://m.example.com/x"}}
    )
    class _Boom:
        def decide(self, *a, **k):
            raise RuntimeError("boom")
    res = AgentNL2Case(mock).to_case("查详情应返回200", gate=_Boom())
    assert res is not None
    assert "laya design ok" not in res.review_reason
