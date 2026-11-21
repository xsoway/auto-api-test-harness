"""Run the pagination AI loop against a simulated product catalog."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from tempfile import mkdtemp

import httpx
import yaml

from ah.core.config import settings
from ah.core.orchestrator import LLMHooks, StepResult, run_single
from ah.core.receipt import build_verification_receipt
from ah.evolution import AutoEvolutionLoop, heuristic_quality
from ah.failure.governor import FailureGovernor
from ah.harness.http import HTTPClient
from ah.harness.loader import load_cases
from ah.llm import LLMClient, LLMDisabledError, MockLLM
from ah.llm.agents import AgentTriage
from ah.predict.assist import AgentNL2Case
from ah.reporting.writer import ResultRecord, build_html_report, build_report
from ah.utils.context import Context
from ah.utils.logging import configure_logging

BASE_URL = "https://catalog.demo.test"
PATH = "/products"
REQUIREMENT = (
    "生成一条名为 products-limit-ten 的用例。请求 GET /products，"
    "通过 request.params 传数字 limit=10，断言 status_code=200，"
    "且 assert.jsonpath.returned.eq=10。契约：limit 取 1 到 20，"
    "模拟目录有 20 件商品，响应 returned 必须等于请求的 limit。"
)


def pagination_case(name: str, limit: int) -> dict:
    return {
        "name": name,
        "request": {"method": "GET", "path": PATH, "params": {"limit": limit}},
        "assert": {"status_code": 200, "jsonpath": {"returned": {"eq": limit}}},
    }


def stub_response(request: httpx.Request, *, fixed: bool, transient: bool) -> httpx.Response:
    if (
        request.method != "GET"
        or request.url.host != "catalog.demo.test"
        or request.url.path != PATH
    ):
        return httpx.Response(404, json={"error": "route_not_found"})
    limit = request.url.params.get("limit", "")
    if limit not in {str(value) for value in range(1, 21)}:
        return httpx.Response(400, json={"error": "limit_out_of_range"})
    if transient and limit == "2":
        return httpx.Response(503, json={"error": "temporarily_unavailable"})
    returned = 9 if limit == "10" and not fixed else int(limit)
    return httpx.Response(200, json={"returned": returned})


def write_yaml(path: Path, mapping: dict) -> None:
    path.write_text(yaml.safe_dump(mapping, allow_unicode=True, sort_keys=False), encoding="utf-8")


def write_evidence(
    report_dir: Path, stage: str, steps: list[StepResult], llm: LLMClient | MockLLM,
) -> dict:
    records = [ResultRecord.from_step(step) for step in steps]
    passed_count = sum(record.result == "PASS" for record in records)
    summary = {
        "verdict": f"{passed_count}/{len(records)} 条用例通过。",
        "recommendations": ["保留分页数量断言，并回归候选覆盖的边界。"],
    }
    if isinstance(llm, MockLLM):
        llm.set_json_response(summary)
    build_report(records, report_dir=str(report_dir), basename=stage, llm=llm)
    if isinstance(llm, MockLLM):
        llm.set_json_response(summary)
        llm.set_json_response({"strategy": "优先回归分页契约。"})
    build_html_report(
        records, report_dir=str(report_dir), basename=stage, llm=llm,
        run_meta={
            "env": "模拟商品目录",
            "base_url": BASE_URL,
            "模型来源": "MockLLM 回放" if isinstance(llm, MockLLM) else "已配置的真实模型",
            "command": "python -m projects.demo.ai_loop",
        },
    )
    receipt = build_verification_receipt(
        steps,
        execution_id=f"{report_dir.parent.name}-{stage}",
        environment="catalog-stub",
        evidence={
            "run_log": str(report_dir / "ah.log"),
            **{
                f"report_{ext}": str(report_dir / f"{stage}.{ext}")
                for ext in ("md", "json", "html")
            },
        },
    )
    (report_dir / f"{stage}-receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    passed = receipt["steps"]["passed"]
    total = receipt["steps"]["total"]
    print(f"{stage}: {receipt['verdict']} ({passed}/{total} 通过)", flush=True)
    return receipt


def run_demo(project: Path, llm: LLMClient | MockLLM) -> int:
    data = project / "data"
    data.mkdir()
    report_dir = project / "report"
    retry_path = data / "transient.yaml"
    baseline_path = data / "generated.yaml"

    expected = pagination_case("products-limit-ten", 10)
    if isinstance(llm, MockLLM):
        llm.set_json_response(expected)
    draft = AgentNL2Case(llm).to_case(REQUIREMENT, validate=False)
    if draft is None or draft.generated.to_yaml_mapping() != expected:
        print("生成草案与演示契约不一致，请核对请求和断言。")
        return 1
    write_yaml(baseline_path, {**draft.generated.to_yaml_mapping(), "suite": "smoke"})
    retry_case = pagination_case("products-transient-retry", 2)
    write_yaml(retry_path, {**retry_case, "suite": "retry-demo", "retries": 1, "retry_backoff": 0})
    print(f"生成 {baseline_path.name}，待审核={draft.needs_human_review}", flush=True)

    (retry,) = load_cases(retry_path)
    (baseline,) = load_cases(baseline_path)
    fixed = False
    seen_transient = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal seen_transient
        response = stub_response(request, fixed=fixed, transient=not seen_transient)
        if response.status_code == 503:
            seen_transient = True
        return response

    with httpx.Client(transport=httpx.MockTransport(handle)) as raw:
        client = HTTPClient(BASE_URL, client=raw)
        retry_result = run_single(retry, Context(), client)
        print(f"瞬时 503：请求 {retry_result.attempts} 次，判定 {retry_result.verdict}", flush=True)
        if isinstance(llm, MockLLM):
            llm.set_json_response({
                "category": "product_defect",
                "root_cause": "HTTP 200，但 limit=10 时 returned=9，违反分页契约。",
                "suggestion": "修复分页逻辑，保留 returned=10 断言。",
            })
        defect = run_single(
            baseline, Context(), client,
            hooks=LLMHooks(triage=AgentTriage(llm).explain_failure),
            governor=FailureGovernor(),
        )
        print(f"业务断言：{defect.error}", flush=True)

        if isinstance(llm, MockLLM):
            llm.set_json_response({"clusters": []})
            weak = pagination_case("products-status-only", 20)
            weak["assert"] = {"status_code": 200}
            llm.set_json_response({"cases": [pagination_case("products-limit-one", 1), weak]})
        # 演进回合重现 smoke 失败，用失败样本与已有用例生成和试跑候选。
        outcome = AutoEvolutionLoop(
            data, project, suite="smoke", client=client, llm=llm,
            min_quality=0.9, scorer=heuristic_quality,
        ).step(1)
        print(f"演进：{outcome.message}", flush=True)

        initial = write_evidence(report_dir, "baseline", [retry_result, defect], llm)
        if not outcome.persisted:
            print("本轮未写入候选，详见演进审计。")
            return 1

        # 切换模拟服务版本。原始 YAML 和 baseline 报告保留供对照。
        fixed = True
        evolved = load_cases(data / "evolution.yaml")
        recovered = [run_single(case, Context(), client) for case in [retry, baseline, *evolved]]
        final = write_evidence(report_dir, "regression", recovered, llm)

    expected_baseline = (
        retry_result.verdict == "passed" and retry_result.attempts == 2
        and defect.verdict == "failed" and initial["verdict"] == "FAIL"
    )
    return 0 if expected_baseline and final["verdict"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "report")
    parser.add_argument(
        "--live-model", action="store_true", help="使用已配置的模型，业务 HTTP 仍走桩",
    )
    args = parser.parse_args()

    # CLI 进程只测演示桩，报告和跟踪也使用演示环境。
    settings.base_url = BASE_URL
    settings.trace_enabled = False
    settings.local_obs_enabled = False
    if not args.live_model:
        os.environ["LLMOFF"] = "1"
    try:
        llm = LLMClient() if args.live_model else MockLLM()
    except LLMDisabledError as exc:
        parser.error(str(exc))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    project = Path(mkdtemp(prefix="ai-loop-", dir=args.output_dir.resolve()))
    os.environ["AH_REPORT_DIR"] = str(project / "report")
    configure_logging(log_dir=project / "report", console=False)
    # 演进引擎默认读取 AH_CASES，这里固定使用本次生成的用例。
    os.environ.pop("AH_CASES", None)
    print(f"模型：{'真实模型' if args.live_model else 'MockLLM 回放'} | HTTP：模拟 | {project}")
    return run_demo(project, llm)


if __name__ == "__main__":
    raise SystemExit(main())
