"""Keep the tutorial's public command and its evidence runnable."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ai_loop_preserves_failure_and_reruns_adopted_case(tmp_path):
    run = subprocess.run(
        [sys.executable, "-m", "projects.demo.ai_loop", "--output-dir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    (batch,) = tmp_path.iterdir()
    report = batch / "report"

    def read_json(name):
        return json.loads((report / name).read_text(encoding="utf-8"))

    baseline = read_json("baseline.json")
    retry, defect = baseline["results"]
    assert (retry["result"], retry["attempts"]) == ("PASS", 2)
    assert retry["llm_notes"]["retried"]["attempts"] == 1
    assert (defect["result"], defect["response"]["status_code"]) == ("FAIL", 200)
    assert defect["llm_notes"]["failure_governance"]["action"] == "block"
    failed_check = next(item for item in defect["assertions"] if not item["passed"])
    assert (failed_check["expected"], failed_check["actual"]) == ("10", "9")
    assert read_json("baseline-receipt.json")["verdict"] == "FAIL"

    audit = json.loads((batch / "_evolution_log.jsonl").read_text(encoding="utf-8"))
    assert audit["failures"] == 1
    assert audit["accepted"] == ["products-limit-one"]
    assert [row["name"] for row in audit["rejected"]] == ["products-status-only"]
    assert "计划覆盖接口：/products" in (report / "baseline.md").read_text(encoding="utf-8")
    assert list((batch / "issues").glob("*.md"))
    assert "503" in (report / "ah.log").read_text(encoding="utf-8")

    regression = read_json("regression.json")
    assert [row["name"] for row in regression["results"]] == [
        "products-transient-retry", "products-limit-ten", "products-limit-one",
    ]
    assert regression["summary"]["passed"] == 3
    receipt = read_json("regression-receipt.json")
    assert receipt["verdict"] == "PASS"
    assert receipt["assertions"] == {"total": 6, "passed": 6, "failed": 0}
    for stage in ("baseline", "regression"):
        assert "LLM 运行观察" in (report / f"{stage}.html").read_text(encoding="utf-8")
