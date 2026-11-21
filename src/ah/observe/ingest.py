#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15 19:35
# @Filename : ingest.py
# @Author   : Alan_Hsu
"""把现有本地数据摄入到 SQLite 可观测存储。

数据源（对齐 Langfuse 模型）：
- ``report_history.jsonl``：每行一个 run 聚合 → ``runs``。
- ``llm_usage.jsonl``：每个 LLM 调用聚合 → ``observations``(GENERATION)。
- ``llm_detail.jsonl``：LLM 明细（含 input/output）→ ``observations``(GENERATION)。
- ``report JSON``（``ResultRecord`` 列表）：用例级 → ``tests`` + ``observations``
  （SPAN/EVENT） + ``scores``。

所有读入函数具备坏行隔离：单个坏行/缺失字段不崩溃，跳过后继续。
本模块不依赖网络，LLMOFF/hermetic 下安全。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ah.observe.store import ObserveStore
from ah.telemetry import LEDGER_FILENAME, load_ledger
from ah.utils.logging import get_logger

log = get_logger("observe.ingest")

#: run 聚合文件名（相对 report_dir）。
HISTORY_FILENAME = "report_history.jsonl"
#: 报告 JSON（可为 test-tcc-m-api-<ts>.json / demo-gov-<ts>.json）。
REPORT_GLOB = "*.json"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 jsonl；坏行/非 dict 行跳过。缺失返回 []。"""
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("_read_jsonl: 跳过坏行 %s: %.80s", path.name, line)
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
                else:
                    log.warning("_read_jsonl: 跳过非对象行 %s: %.80s", path.name, line)
    except (OSError, IOError):
        return []
    return rows


def _run_id_from_row(row: dict[str, Any], index: int, name: str) -> str:
    """从 run 聚合行派生稳定 run id：优先显式 id，否则 ``name:run_time``。

    ``name:run_time`` 保证同一 report 目录里的 history 行与 report JSON 共享
    run id，使 tests/observations 能挂到同一 run（看板详情视图数据完整）。
    """
    rid = row.get("id") or row.get("run_id")
    if rid:
        return str(rid)
    run_time = str(row.get("run_time") or "")
    if not run_time:
        return f"{name}:{index}"
    return f"{name}:{run_time}"


def _duration_ms(value: Any) -> float:
    """report JSON 里 duration 常为秒 → 统一转毫秒。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    # 秒级值（< 1 的 duration 实际是秒）转 ms；已是 ms 的大值保留。
    return round(v * 1000.0, 3) if v < 1.0 else round(v, 3)


def ingest_report_history(
    store: ObserveStore,
    history: str | Path,
    *,
    name_prefix: str = "run",
    source_file: str = "",
) -> int:
    """把 report_history.jsonl 摄入为 runs。返回摄入条数。"""
    rows = _read_jsonl(Path(history))
    for i, row in enumerate(rows):
        rid = _run_id_from_row(row, i, name_prefix)
        store.upsert_run({
            "id": rid,
            "name": row.get("name") or f"{name_prefix} {row.get('run_time', '')}",
            "run_id": rid,
            "start_time": row.get("run_time", ""),
            "total": row.get("total", 0),
            "passed": row.get("passed", 0),
            "failed": row.get("failed", 0),
            "flaky": row.get("flaky", 0),
            "pass_rate": row.get("pass_rate", 0.0),
            "result_dist": row.get("result_dist") or {},
            "status_dist": row.get("status_dist") or {},
            "avg_duration_ms": row.get("avg_duration_ms", 0.0),
            "p95_duration_ms": row.get("p95_duration_ms", 0.0),
            "total_duration_ms": row.get("total_duration_ms", 0.0),
            "per_case_dur_ms": row.get("per_case_dur_ms") or {},
            "total_cost": 0.0,  # 成本来自 llm_usage 摄入时累加
            "source_file": source_file or str(history),
            "source_type": "history",
        })
    return len(rows)


def ingest_llm_ledger(
    store: ObserveStore,
    ledger: str | Path,
    *,
    run_id: str,
    source: str = "llm_usage",
) -> int:
    """把 llm_usage.jsonl 摄入为 observations(GENERATION)。返回条数。"""
    rows = load_ledger(Path(ledger))
    for i, row in enumerate(rows):
        oid = f"{run_id}:llm:{source}:{i}"
        store.upsert_observation({
            "id": oid,
            "run_id": run_id,
            "name": f"LLM {row.get('task', '')}".strip(),
            "type": "GENERATION",
            "start_ms": float(i) * 0.0,  # 聚合账本无精确时间；用序号占位
            "latency_ms": row.get("latency_ms"),
            "level": "ERROR" if row.get("status") == "error" else "INFO",
            "status": str(row.get("status") or "success"),
            "input": None,
            "output": None,
            "model": row.get("model", ""),
            "task": row.get("task", ""),
            "prompt_tokens": row.get("prompt_tokens", 0),
            "completion_tokens": row.get("completion_tokens", 0),
            "cost": row.get("cost", 0.0),
            "metadata": {},
            "source": source,
        })
    return len(rows)


def ingest_llm_detail(
    store: ObserveStore,
    detail: str | Path,
    *,
    run_id: str,
) -> int:
    """把 llm_detail.jsonl（含 input/output）摄入为 observations(GENERATION)。返回条数。"""
    rows = _read_jsonl(Path(detail))
    for i, row in enumerate(rows):
        oid = f"{run_id}:llm-detail:{i}"
        store.upsert_observation({
            "id": oid,
            "run_id": run_id,
            "name": f"LLM {row.get('task', '')}".strip(),
            "type": "GENERATION",
            "start_ms": 0.0,
            "latency_ms": row.get("latency_ms"),
            "level": "ERROR" if row.get("status") == "error" else "INFO",
            "status": str(row.get("status") or "success"),
            "input": row.get("input"),
            "output": row.get("output"),
            "model": row.get("model", ""),
            "task": row.get("task", ""),
            "prompt_tokens": row.get("prompt_tokens", 0),
            "completion_tokens": row.get("completion_tokens", 0),
            "cost": row.get("cost", 0.0),
            "metadata": row.get("metadata") or {},
            "source": "llm_detail",
        })
    return len(rows)


def _derive_scores(run_id: str, result: str, llm_notes: dict[str, Any]) -> list[dict[str, Any]]:
    """从用例结果 + llm_notes 派生打分（severity / healed / recovered）。"""
    scores: list[dict[str, Any]] = []
    # 通过率定性分。
    if result == "PASS":
        scores.append({"name": "pass", "value": 1.0})
    elif result in ("FLKY",):
        scores.append({"name": "pass", "value": 0.5})
    else:
        scores.append({"name": "pass", "value": 0.0})
    # 失败治理分类。
    gov = llm_notes.get("failure_governance") or {}
    category = gov.get("category")
    if category:
        scores.append({"name": "failure_category", "value": float(len(category))})
    # 是否自愈恢复。
    healed = llm_notes.get("healed") or llm_notes.get("recovered")
    if isinstance(healed, dict) and healed.get("recovered"):
        scores.append({"name": "healed", "value": 1.0})
    elif result == "FAIL":
        scores.append({"name": "healed", "value": 0.0})
    return scores


def _case_observation(
    run_id: str, index: int, result: str, r: dict[str, Any]
) -> dict[str, Any]:
    """把一个用例结果展开为一次 SPAN observation（范围 = 该用例）。"""
    gov = (r.get("llm_notes") or {}).get("failure_governance") or {}
    level = "ERROR" if result in ("FAIL", "FLKY") else "INFO"
    status = "error" if result == "FAIL" else ("ok" if result == "PASS" else "skipped")
    return {
        "id": f"{run_id}:case:{index}",
        "run_id": run_id,
        "name": str(r.get("name") or f"case-{index}"),
        "type": "SPAN",
        "start_ms": 0.0,
        "latency_ms": _duration_ms(r.get("duration")),
        "level": level,
        "status": status,
        "input": None,
        "output": None,
        "model": "",
        "task": str(gov.get("action") or ""),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost": 0.0,
        "metadata": {
            "category": gov.get("category"),
            "cluster_state": gov.get("cluster_state"),
            "signal_error": gov.get("signal_error"),
            "request": r.get("request"),
            "response": r.get("response"),
            "assertions": r.get("assertions") or [],
        },
        "source": "case",
    }


def ingest_report_json(
    store: ObserveStore,
    report_path: str | Path,
    *,
    run_id: str,
) -> int:
    """把一个 report JSON（含 results: ResultRecord[]）摄入为 tests + observations + scores。"""
    p = Path(report_path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("ingest_report_json: 读取失败 %s: %s", p, exc)
        return 0
    results = data.get("results") or []
    # run 概要：仅当该 run 尚未存在（history 未摄入）时才写入。history 是权威
    # 概要源（含 pass_rate/时长/分布）；report JSON 的 summary 常只含 total/passed/
    # failed，若强行按同 run_id 覆盖，会把 history 的完整字段清成 0（回归 bug）。
    # 已存在时只补 tests/observations/scores，绝不改动 run 概要行。
    if store.get_run(run_id) is None:
        summary = data.get("summary") or {}
        # 从 results 派生时长/结果分布，避免硬编码 0。
        per_case = {
            str(r.get("name") or f"case-{i}"): _duration_ms(r.get("duration"))
            for i, r in enumerate(results)
            if r.get("duration") is not None
        }
        dist: dict[str, int] = {}
        for r in results:
            k = str(r.get("result") or "SKIP").upper()
            dist[k] = dist.get(k, 0) + 1
        n = len(results)
        passed = dist.get("PASS", 0)
        store.upsert_run({
            "id": run_id,
            "name": Path(report_path).stem or run_id,
            "run_id": run_id,
            "start_time": data.get("run_time", ""),
            "total": summary.get("total", n),
            "passed": summary.get("passed", passed),
            "failed": summary.get("failed", n - passed),
            "flaky": 0,
            "pass_rate": summary.get("pass_rate", round(passed * 100.0 / n, 2) if n else 0.0),
            "result_dist": dist,
            "status_dist": {},
            "avg_duration_ms": (
                round(sum(per_case.values()) / len(per_case), 3) if per_case else 0.0
            ),
            "p95_duration_ms": 0.0,
            "total_duration_ms": round(sum(per_case.values()), 3),
            "per_case_dur_ms": per_case,
            "total_cost": (data.get("telemetry") or {}).get("cost", 0.0),
            "source_file": str(report_path),
            "source_type": "report",
        })
    for i, r in enumerate(results):
        name = str(r.get("name") or f"case-{i}")
        result = str(r.get("result") or "SKIP").upper()
        llm_notes = r.get("llm_notes") or {}
        store.upsert_test({
            "id": f"{run_id}:{name}",
            "run_id": run_id,
            "name": name,
            "result": result,
            "duration_ms": _duration_ms(r.get("duration")),
            "failure": r.get("failure_msg") or "",
            "request": r.get("request") or {},
            "response": r.get("response") or {},
            "assertions": r.get("assertions") or [],
            "llm_notes": llm_notes,
        })
        store.upsert_observation(_case_observation(run_id, i, result, r))
        for s in _derive_scores(run_id, result, llm_notes):
            store.upsert_score({
                "run_id": run_id,
                "test_id": f"{run_id}:{name}",
                "name": s["name"],
                "value": s["value"],
                "comment": "",
                "source": "report",
            })
    return len(results)


def ingest_report_dir(
    store: ObserveStore,
    report_dir: str | Path,
    *,
    name_prefix: str = "run",
) -> dict[str, int]:
    """把整个 report 目录摄入：history r/a runs + ledger + detail + report JSON(s)。

    返回 {runs, observations(ledger), detail, tests} 计数摘要。
    """
    rd = Path(report_dir)
    counts = {"runs": 0, "ledger_obs": 0, "detail_obs": 0, "tests": 0}

    history = rd / HISTORY_FILENAME
    if history.exists():
        counts["runs"] += ingest_report_history(store, history, name_prefix=name_prefix)

    # ledger / detail 归到「最近一个 run」（若存在）；否则造一个 run。
    ledger = rd / LEDGER_FILENAME
    detail = rd / "llm_detail.jsonl"
    if ledger.exists() or detail.exists():
        last = store.list_runs(page=1, limit=1)[0]
        if last:
            run_id = last[0]["id"]
        else:
            run_id = f"{name_prefix}:ingest:{int(time.time() * 1000)}"
        if ledger.exists():
            counts["ledger_obs"] += ingest_llm_ledger(store, ledger, run_id=run_id)
        if detail.exists():
            counts["detail_obs"] += ingest_llm_detail(store, detail, run_id=run_id)

    # report JSON(s)：每个含 results 的都摄入为 tests + observations + scores。
    for rj in sorted(rd.glob(REPORT_GLOB)):
        if rj.name == HISTORY_FILENAME:
            continue
        try:
            data = json.loads(rj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not data.get("results"):
            continue
        run_id = _run_id_from_row(
            {"run_time": data.get("run_time", ""), "name": name_prefix}, 0, name_prefix
        )
        counts["tests"] += ingest_report_json(store, rj, run_id=run_id)
    return counts


def seed_demo(
    store: ObserveStore,
    *,
    runs: int = 3,
    llm_per_run: int = 4,
    cases_per_run: int = 5,
) -> dict[str, int]:
    """生成 hermetic 合成演示数据（零网络）。返回计数摘要。"""
    models = ["sglang-deepseek-v4-flash", "gpt-4o-mini"]
    task_pool = ["predict-advice:json", "report-summary:json", "triage:json"]
    case_names = ["首页", "列表", "商品详情", "价格查询", "查找商品"]
    for i in range(runs):
        rid = f"seed:run-{i + 1}"
        passed = cases_per_run - 1
        costs = 0.0
        for k in range(llm_per_run):
            pt, ct = 120 + k * 30, 40 + k * 10
            cost = (pt + ct) * 0.000001
            costs += cost
            store.upsert_observation({
                "id": f"{rid}:llm:{k}",
                "run_id": rid,
                "name": f"LLM {task_pool[k % len(task_pool)]}",
                "type": "GENERATION",
                "start_ms": float(k) * 100.0,
                "latency_ms": 500.0 + k * 120.0,
                "level": "INFO",
                "status": "success",
                "input": f"提示词：分析 run {i + 1} 用例 {k} 的失败风险",
                "output": (
                    f"建议回归：{case_names[k % len(case_names)]}"
                    f"（置信度 {0.7 + k * 0.05:.2f}）"
                ),
                "model": models[k % len(models)],
                "task": task_pool[k % len(task_pool)],
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "cost": cost,
                "metadata": {"seed": True},
                "source": "seed",
            })
        store.upsert_run({
            "id": rid,
            "name": f"seed 运行 {i + 1}",
            "start_time": f"2026-10-{10 + i:02d} {10 + i:02d}:00:00",
            "total": cases_per_run,
            "passed": passed,
            "failed": 1 if i % 2 else 0,
            "flaky": 1 if i % 2 else 0,
            "pass_rate": (passed / cases_per_run) * 100.0,
            "result_dist": {"PASS": passed, "FAIL": 1 if i % 2 else 0,
                            "FLKY": 1 if not i % 2 else 0},
            "status_dist": {"200": cases_per_run - 1, "404": 1},
            "avg_duration_ms": 300.0 + i * 20.0,
            "p95_duration_ms": 800.0 + i * 50.0,
            "total_duration_ms": 900.0 * cases_per_run,
            "per_case_dur_ms": {n: 200.0 + i * 30 for n in case_names},
            "total_cost": round(costs, 6),
            "source_file": "seed",
            "source_type": "seed",
        })
        for j, name in enumerate(case_names):
            result = "FAIL" if (j == 3 and i % 2) else ("FLKY" if j == 2 else "PASS")
            store.upsert_test({
                "id": f"{rid}:{name}",
                "run_id": rid,
                "name": name,
                "result": result,
                "duration_ms": 200.0 + j * 50.0,
                "failure": "" if result == "PASS" else "404 not found",
                "request": {"method": "GET", "path": f"/api/{name}"},
                "llm_notes": {"failure_governance": {"category": "unknown"}}
                if result != "PASS" else {},
            })
            store.upsert_score({
                "run_id": rid,
                "test_id": f"{rid}:{name}",
                "name": "pass",
                "value": 0.0 if result == "FAIL" else (0.5 if result == "FLKY" else 1.0),
                "comment": "",
                "source": "seed",
            })
    return store.counts()
