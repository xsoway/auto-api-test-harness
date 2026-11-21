"""预测分析层 hermetic 验证（含历史库 / 风险 / 回归推荐 / LLM 生成器）。"""

from __future__ import annotations

from pathlib import Path

from ah.harness.http import HTTPClient
from ah.harness.loader import Case
from ah.llm.mock import MockLLM
from ah.predict.generator import Predictor
from ah.predict.history import HistoryStore
from ah.predict.risk import rank_regression, risk_profiles
from ah.reporting.writer import ResultRecord

# ── HistoryStore ────────────────────────────────────────────────────────────

def test_history_append_and_load_roundtrip(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.jsonl")
    recs = [
        ResultRecord(name="a.GET", result="PASS", duration=0.1),
        ResultRecord(name="b.GET", result="FAIL", failure_msg="expected X got Y"),
    ]
    n = store.append_all(recs, now=1234.5)
    assert n == 2
    rows = store.load()
    assert len(rows) == 2
    assert rows[0]["name"] == "a.GET"
    assert rows[0]["result"] == "PASS"
    assert rows[0]["failure_class"] is None  # 通过记录无失败类别
    assert rows[1]["failure_class"] == "assertion"
    assert rows[1]["interface"] == "b.GET"
    assert rows[1]["ts"] == 1234.5


def test_history_append_is_accumulative(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.jsonl")
    store.append_all([ResultRecord(name="x", result="PASS")])
    store.append_all([ResultRecord(name="y", result="FAIL", failure_msg="status 500")])
    assert len(store.load()) == 2


def test_history_missing_file_returns_empty(tmp_path: Path) -> None:
    assert HistoryStore(tmp_path / "missing.jsonl").load() == []


def test_history_interface_key_falls_back_to_name(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.jsonl")
    store.append_all([ResultRecord(name="no-request", result="FAIL")])
    assert store.load()[0]["interface"] == "no-request"


# ── Risk ────────────────────────────────────────────────────────────────────

def _row(
    name: str,
    result: str,
    fclass: str | None = "unknown",
    interface: str = "",
) -> dict:
    return {
        "name": name,
        "result": result,
        "failure_class": fclass if result != "PASS" else None,
        "interface": interface or name,
    }


def test_risk_profiles_counts_and_orders(tmp_path: Path) -> None:
    rows = [
        _row("a", "FAIL", "assertion", "/a"),
        _row("a", "FAIL", "assertion", "/a"),
        _row("a", "PASS", None, "/a"),
        _row("b", "PASS", None, "/b"),
        _row("b", "PASS", None, "/b"),
    ]
    profiles = risk_profiles(rows)
    by_intf = {p.interface: p for p in profiles}
    a = by_intf["/a"]
    assert a.total == 3
    assert a.failures == 2
    assert abs(a.failure_rate - 2 / 3) < 1e-3  # risk.py 保留 4 位小数
    assert a.risk > 0
    assert "assertion" in a.top_failure_classes
    b = by_intf["/b"]
    assert b.failures == 0 and b.risk == 0.0
    # 风险降序
    assert profiles[0].risk >= profiles[-1].risk


def test_risk_confidence_scales_with_samples(tmp_path: Path) -> None:
    low = risk_profiles([_row("a", "FAIL", "assertion", "/a")])[0]
    high = risk_profiles([_row("a", "FAIL", "assertion", "/a") for _ in range(10)])[0]
    assert low.confidence < high.confidence
    assert high.confidence == 1.0


def test_rank_regression_focused_only_high_risk(tmp_path: Path) -> None:
    rows = [_row(f"c{i}", "FAIL", "assertion", f"/c{i}") for i in range(6)]
    plan = rank_regression(rows, risk_threshold=0.3, top_k=5)
    assert 1 <= len(plan.focused) <= 5
    assert all(p.risk >= 0.3 for p in plan.focused)
    # to_dict 可序列化
    d = plan.to_dict()
    assert "focused" in d and "all_profiles" in d

# ── Report integration ──────────────────────────────────────────────────────

def test_build_report_emits_prediction_section_when_history(tmp_path: Path) -> None:
    from ah.reporting.writer import build_html_report, build_report

    store = HistoryStore(tmp_path / "history.jsonl")
    store.append_all(
        [ResultRecord(name="b.GET", result="FAIL", failure_msg="expected X got Y")]
    )
    md = build_report(
        [ResultRecord(name="a.GET", result="PASS")], report_dir=str(tmp_path)
    )
    text = md.read_text()
    assert "## 预测分析" in text
    assert "风险预测" in text
    assert "--ah-suite" in text
    html = build_html_report(
        [ResultRecord(name="a.GET", result="PASS")], report_dir=str(tmp_path)
    ).read_text()
    assert "预测分析" in html

    import json

    data = json.loads((tmp_path / "report.json").read_text())
    assert "prediction" in data


def test_build_report_no_prediction_without_history(tmp_path: Path) -> None:
    from ah.reporting.writer import build_report

    md = build_report(
        [ResultRecord(name="a.GET", result="PASS")], report_dir=str(tmp_path)
    )
    assert "## 预测分析" not in md.read_text()


# ── Predictor（LLM 生成器）─────────────────────────────────────────────────

def _case(name: str, path: str = "/x") -> Case:
    return Case(name=name, request={"method": "GET", "path": path})


def test_predictor_llm_off_returns_empty() -> None:
    # LLM 禁用（None）且无论如何不应调用网络/构造客户端。
    p = Predictor(llm=None, client=HTTPClient(""), ctx=None)
    out = p.propose([_case("a")], [])
    assert out == []




def test_predictor_rejects_candidate_failing_rerun(tmp_path: Path) -> None:
    import httpx

    client = HTTPClient(
        "",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, text="different body")
            )
        ),
    )
    mock = MockLLM()
    mock.set_json_response(
        {
            "cases": [
                {
                    "name": "bad",
                    "request": {"method": "GET", "path": "https://m.example.test/bad"},
                    "assert": {"contains": "not-present"},
                }
            ]
        }
    )
    p = Predictor(llm=mock, client=client, ctx=None)
    accepted = p.propose([_case("a", "/x")], [], limit=3)
    assert accepted == []
    assert mock.calls == 1


def test_predictor_validates_candidate_by_rerun(tmp_path: Path) -> None:
    import httpx

    # mock 返回 body=good —— 候选断言「contains good」重跑通过即被接受。
    client = HTTPClient(
        "",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, text="the good response")
            )
        ),
    )
    mock = MockLLM()
    mock.set_json_response(
        {
            "cases": [
                {
                    "name": "new-case",
                    "request": {
                        "method": "GET",
                        "path": "https://m.example.test/new",
                    },
                    "assert": {"status_code": 200, "contains": "good response"},
                }
            ]
        }
    )
    p = Predictor(llm=mock, client=client, ctx=None)
    accepted = p.propose([_case("a", "/x")], [], limit=3)
    assert len(accepted) == 1
    assert accepted[0].name == "new-case"
    assert accepted[0].validated is True
    assert mock.calls == 1 and mock.call_log[0]["task"] == "predict"


def test_predictor_ignores_invalid_candidates(tmp_path: Path) -> None:
    client = HTTPClient("")
    mock = MockLLM()
    mock.set_json_response(
        {"cases": [{"name": "no-path"}, {"request": {"method": "GET"}}]}
    )
    p = Predictor(llm=mock, client=client, ctx=None)
    assert p.propose([_case("a", "/x")], [], limit=3) == []


def test_predictor_failure_input_uses_history(tmp_path: Path) -> None:
    import httpx

    client = HTTPClient(
        "",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
        ),
    )
    mock = MockLLM()
    mock.set_json_response({"cases": []})
    p = Predictor(llm=mock, client=client, ctx=None)
    history = [
        _row("a", "FAIL", "assertion", "/a") | {"failure_msg": "expected q got z"},
        {"interface": "/a", "result": "FAIL", "failure_msg": "boom", "llm_notes": {}},
    ]
    p.propose([_case("a", "/x")], history, limit=3)
    prompt = mock.call_log[0]["prompt"]
    assert "/a" in prompt  # 失败历史进入提示
    assert "assertion" in prompt
