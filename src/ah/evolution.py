#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time: 2026-10-08
# @Filename: evolution.py
# @Author: Alan_Hsu
"""WS1 自主涌现闭环（功能点 1,2,3,4,11,12,13）。

把"跑→分析→生成→持久化→下一轮"的**自主涌现闭环**落地：

- :class:`AutoEvolutionLoop`（功能 1）：多轮跑本套件 → :class:`AgentCluster`
  聚类失败 → 质量找短板 → :class:`Predictor`/:class:`AgentCoverage` 生成候选
  （接 :class:`AgentImpact` 若给 diff）→ 门控过筛 → 持久化写回 data → 下一轮。
  停车条件：达到 ``max_rounds`` | ``budget`` 耗尽 | 无新候选采纳 | 全绿两轮无变化。
- 持久化（功能 2）：候选经 :meth:`GeneratedCase.to_yaml_mapping` 写回
  ``data/evolution.yaml``，以 ``# --- ah-evolved round N ---`` 注释段分隔、追加不
  覆盖；每条加 ``tags: [ah-generated]``、``suite: evolution``、``env``；与已加载
  :class:`Case` 的 ``(method, path, name)`` 去重不写；``evolve --undo`` 删除该轮
  新增段。
- 质量护栏（功能 4）：候选过 ``min_quality``（默认 0.6）门控，低分拒绝 + 记录
  ``rejected_reason``；hermetic 下走确定性启发式评分（断言类型/参数覆盖），不依赖
  LLM。
- 变更驱动（功能 3）：``--diff <ref>`` 用 :class:`AgentImpact` 只对受影响接口生成
  （git diff 受影响路径 → 接口）。
- 失败自动动作（功能 11）：triage 结论 → 生成 issue/bug 单草稿（写项目 ``issues/``
  目录或记入 `_evolution_log.jsonl`），带复现用例。
- 回归计划执行（功能 12）：消费 :func:`rank_regression` → :class:`RegressionPlan`，
  按计划反推 ``--suite``/``--priority`` 触发下一轮回归。
- 趋势闸（功能 13）：``trend-gate --drop P`` 近 K 次通过率下滑 ≥ P → 自动升套件
  （smoke→regression→prod）或追加候选生成一轮，stderr + 报告横幅告警。

LLMOFF=1 下所有子命令可完整运行：LLM 助手（:class:`Predictor`/:class:
`AgentCoverage`/:class:`AgentImpact`/:meth:`AgentQuality.score`）在 ``llm=None``
时返回空；质量护栏走确定性启发式评分；候选生成在无 LLM 时优雅空转（返回 ``[]``），
绝不触碰网络。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import yaml

from ah.core.config import settings
from ah.core.orchestrator import StepResult, run_flow, run_single, summarize_results
from ah.core.receipt import build_verification_receipt
from ah.core.trace import get_tracer
from ah.evidence.laya_gate import STAGE_QUALITY, STAGE_STOP
from ah.harness.http import HTTPClient
from ah.harness.loader import (
    Case,
    discover_case_files,
    load_cases,
    select_cases,
)
from ah.predict.assist import (
    AgentCluster,
    AgentCoverage,
    AgentImpact,
    AgentQuality,
)
from ah.predict.generator import GeneratedCase, Predictor
from ah.predict.risk import RegressionPlan, rank_regression
from ah.utils.context import Context
from ah.utils.logging import get_logger

log = get_logger("evolution")

#: 演进用例的持久化标签与用例集标记（写入 ``data/evolution.yaml``）。
EVOLVED_TAG = "ah-generated"
EVOLVED_SUITE = "evolution"
#: 演进写回的 YAML 文件名（属 ``data/*.yaml``，集中管理以便回滚）。
EVOLUTION_YAML = "evolution.yaml"
#: 演进审计日志文件名（候选/门控/采纳/拒绝/理由），落在项目根或报告目录。
EVOLUTION_LOG = "_evolution_log.jsonl"
#: 附加候选生成/提交段标记（每轮一条）。
ROUND_MARKER = "# --- ah-evolved round {n} ---"

#: 套件自动升档链（趋势闸用）：冒烟 → 回归 → 生产。
_SUITE_CHAIN = ("smoke", "regression", "prod")


# ───────────────────────── 基础 helpers ─────────────────────────

def _request_key(case: Any) -> tuple[str, str, str]:
    """提取去重主键 ``(method, path, name)``。

    兼容 :class:`Case`（有 ``request`` 字典）与 :class:`GeneratedCase`
    （有 ``request`` 字典与 ``name`` 字段）。
    """
    req = getattr(case, "request", {}) or {}
    return (
        str((req or {}).get("method", "GET")).upper(),
        str((req or {}).get("path", "")),
        str(getattr(case, "name", "") or ""),
    )


def load_project_cases(data_dir: str | Path) -> list[Case]:
    """加载项目 ``data/`` 下全部 YAML 用例；无文件或解析失败仅告警不抛。"""
    data = Path(data_dir)
    if not data.is_dir():
        return []
    cases: list[Case] = []
    for path in discover_case_files(data):
        if Path(path).name == EVOLUTION_YAML:
            continue  # 演进文件也归入去重源，但避免重复加载的循环问题（下同）
        try:
            cases.extend(load_cases(path))
        except Exception as exc:  # 单文件坏不影响其余
            log.warning("跳过坏用例文件 %s: %s", path, exc)
    # 加载演进文件本身（含历史轮次演进用例），保证去重能覆盖演进结果。
    evo = data / EVOLUTION_YAML
    if evo.is_file():
        try:
            cases.extend(load_cases(str(evo)))
        except Exception as exc:
            log.warning("跳过演进文件 %s: %s", evo, exc)
    return cases


def _hermetic_client(trace: Any = None) -> HTTPClient:
    """构造一个确定性的隔离 HTTP 客户端（绝不触碰网络）。

    对所有请求返回稳定的空 JSON（200），使 LLMOFF 下的套件运行可重复、无外部依赖。
    ``trace`` 可选：传入真实 Tracer 时，HTTP span 也写入本地过程链路
    （run→case→step→HTTP/LLM）；缺省 None 则无 HTTP 跨度（保持原行为）。
    """
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
    raw = httpx.Client(transport=transport)
    return HTTPClient(settings.base_url or "http://hermetic.local", client=raw, trace=trace)


def _llm_off() -> bool:
    """与 :func:`ah.llm.openai.llm_enabled` 判定一致的 LLM 关闭检测。"""
    return os_env_off()


def os_env_off() -> bool:
    """读取 ``LLMOFF`` 环境变量（结构化便于测试 monkeypatch）。"""
    return os_env_get("LLMOFF").strip().lower() in {"1", "true", "yes", "on"}


def os_env_get(name: str) -> str:
    """包装 ``os.environ.get``，便于测试注入。"""
    import os

    return os.environ.get(name, "")


def _process_gate() -> Any:
    """惰性取进程级 LayaGate（AH_LAYA 未设/off → None，字节等价零网络）。"""
    from ah.evidence.laya_gate import get_gate

    return get_gate()


def resolve_project_dir(project: str) -> Path:
    """把 ``<project>`` 解析为项目目录（含 ``data/``）。

    优先把参数当显式路径；其次在 ``projects/<project>`` 下查找。
    """
    direct = Path(project)
    if direct.is_dir() and (direct / "data").is_dir():
        return direct
    candidate = Path("projects") / project
    if candidate.is_dir() and (candidate / "data").is_dir():
        return candidate
    raise FileNotFoundError(
        f"未找到项目目录（需含 data/）：{project}（也尝试过 {candidate}）"
    )


def ensure_data_dir(project_dir: Path) -> Path:
    """确保项目 data 目录存在并返回。"""
    data = project_dir / "data"
    data.mkdir(parents=True, exist_ok=True)
    return data


# ───────────────────────── 质量护栏（feature 4）────────────────────────

def heuristic_quality(case: Case | GeneratedCase) -> float:
    """确定性启发式质量评分（0–1），用于 LLM 关闭时的门控回退。

    三部分加权：断言有效性（0.5）/ 参数覆盖（0.3）/ 维护成本（0.2）。
    逻辑：无任何断言 → 断言项为 0（低分拒绝）；只有 ``status`` 断言 → 弱；
    含响应体断言（jsonpath/schema/equals/contains）→ 强；带请求体参数靠前加分。
    """
    score = 0.0
    req = getattr(case, "request", {}) or {}
    name = str(getattr(case, "name", "") or "")
    # 断言有效性（0.5）：无断言最能捕获"用例腐化"（恒真即失效）。
    assert_ = getattr(case, "assert_", None) or {}
    if assert_:
        if _has_status_assert(assert_):
            score += 0.25
        if _has_body_assert(assert_):
            score += 0.25
        else:
            score += 0.10  # 仅有状态码断言：弱但非恒真
    # 参数覆盖（0.3）：带请求体/查询参数说明探索了参数空间；模板路径次之。
    if req.get("params") or req.get("json") or req.get("data"):
        score += 0.30
    elif "{" in str(req.get("path", "")):
        score += 0.15
    # 维护成本（0.2）：命名有意义且不与路径重复（区分同名退化）。
    if name and name != str(req.get("path", "")):
        score += 0.20
    else:
        score += 0.10
    return round(min(1.0, max(0.0, score)), 3)


def _has_status_assert(assert_: dict[str, Any]) -> bool:
    return any(k in assert_ for k in ("status", "status_code"))


def _has_body_assert(assert_: dict[str, Any]) -> bool:
    return any(k in assert_ for k in ("jsonpath", "schema", "equals", "contains", "body"))


def quality_score_case(case: Case | GeneratedCase, llm: Any = None) -> float:
    """对单条用例打分：LLM 启用时优先用 :class:`AgentQuality`，否则确定性回退。

    :class:`AgentQuality.score` 期望 ``list[Case]`` 且无 LLM 时返回空 —— 因此
    LLM 关闭/失败时始终走 :func:`heuristic_quality`，保证 hermetic 确定性。
    """
    if llm is not None:
        probe = AgentQuality(llm)
        reviews = probe.score([case] if isinstance(case, Case) else [case.to_case()])
        if reviews:
            return round(float(reviews[0].get("score") or 0.0), 3)
    return heuristic_quality(case)


@dataclass
class GateResult:
    """一条候选的门控判定。"""

    candidate: GeneratedCase
    score: float
    accepted: bool
    reason: str = ""
    signal: dict = field(default_factory=dict)

def gate_candidates(
    candidates: list[GeneratedCase],
    *,
    min_quality: float = 0.6,
    llm: Any = None,
    scorer: Callable[[GeneratedCase], float] | None = None,
    gate: Any = None,
) -> tuple[list[GateResult], list[GeneratedCase]]:
    """对候选过 ``min_quality`` 门控。

    返回 ``(所有判定, 采纳集合)``；低分候选 `accepted=False` 并在 ``reason`` 中给出
    拒绝理由（供 `_evolution_log.jsonl` 记录）。``scorer`` 可注入以覆盖评分逻辑
    （测试/真实 LLM 都走此入口）。

    传入 ``gate``（LayaGate 实例）时，在原有评分前先做一层质量分流：高置信 AUTO
    直接裁决（值 ≥ min_quality → 采纳，否则拒绝）并跳过原逻辑；未开闸/低置信/异常
    一律回退原 ``quality_score_case``，保持字节等价。``gate=None``（默认/off）完全
    跳过 Laya 埋点。
    """
    _questions: dict = {
        "quality": {
            "type": "score",
            "instructions": "给这条测试用例的质量评分（0-1，越高越好）",
            "criteria": [
                "无断言且参数覆盖差，易腐化",
                "有基础断言或参数覆盖，可维护",
                "断言充分且参数覆盖好，高价值",
            ],
        }
    }
    judged: list[GateResult] = []
    accepted: list[GeneratedCase] = []
    for cand in candidates:
        if gate is not None:
            try:
                req = cand.request or {}
                state = (
                    f"候选测试用例：{cand.name}; "
                    f"method={req.get('method')}; path={req.get('path')}; "
                    f"请求体/{req.get('params') or req.get('body') or '无'}"
                )
                dec = gate.best(STAGE_QUALITY, state, _questions)
                if dec.get("action") == "auto":
                    value = dec.get("value")
                    if isinstance(value, (int, float)):
                        # laya score 返回 0-based 期望分（区间 [0, n_bands-1]），
                        # 除以 (n_bands-1) 归一化到 [0,1] 再与 min_quality 比较。
                        n_bands = max(1, len(_questions["quality"]["criteria"]))
                        score = float(value) / (n_bands - 1) if n_bands > 1 else float(value)
                        signal = dict(dec.get("signal") or {})
                        if score >= min_quality:
                            judged.append(
                                GateResult(cand, score, True, "laya_quality_ok", signal)
                            )
                            accepted.append(cand)
                        else:
                            judged.append(
                                GateResult(cand, score, False, "laya_quality_below", signal)
                            )
                        continue  # Laya AUTO 直接裁决，跳过原逻辑
            except Exception as exc:  # Laya 故障绝不改变原流程
                log.warning("gate_candidates Laya 分流失败，回退原评分: %s", exc)
        score = scorer(cand) if scorer is not None else quality_score_case(cand, llm=llm)
        if score >= min_quality:
            judged.append(GateResult(cand, score, True, "quality_ok"))
            accepted.append(cand)
        else:
            judged.append(
                GateResult(cand, score, False, f"quality_below_{min_quality}")
            )
    return judged, accepted


# ───────────────────────── 持久化（feature 2）────────────────────────

def persist_evolution(
    data_dir: str | Path,
    candidates: list[GeneratedCase],
    *,
    round_no: int,
    env: str = "all",
    existing: Optional[list[Case]] = None,
) -> tuple[list[dict[str, Any]], list[GeneratedCase]]:
    """把候选追加写回 ``data/evolution.yaml``（含去重）。

    - 与 ``existing``（已加载 Case 的 ``(method, path, name)``）去重，重复不写。
    - 以 ``# --- ah-evolved round N ---`` 注释段分隔追加，不覆盖现有用例。
    - 每条 mapping = ``to_yaml_mapping()`` + ``tags/suite/env`` 扩展。
    返回 ``(写入的 mapping 列表, 因重复被跳过的候选列表)``。
    """
    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)
    evo = data / EVOLUTION_YAML
    existing_keys = {_request_key(c) for c in (existing or [])}
    written: list[dict[str, Any]] = []
    skipped: list[GeneratedCase] = []
    for cand in candidates:
        key = _request_key(cand)
        if key in existing_keys:
            skipped.append(cand)
            log.info("去重跳过已存在用例 %s", cand.name)
            continue
        mapping = cand.to_yaml_mapping()
        mapping["tags"] = [EVOLVED_TAG]
        mapping["suite"] = EVOLVED_SUITE
        mapping["env"] = env or "all"
        written.append(mapping)
        existing_keys.add(key)  # 同轮内互相去重
    if not written:
        return written, skipped
    with evo.open("a", encoding="utf-8") as fh:
        if evo.stat().st_size == 0:
            fh.write(f"# --- {EVOLVED_SUITE} managed (追加不覆盖)，undo 可回滚 ---\n")
            fh.write("cases:\n")
        fh.write(ROUND_MARKER.format(n=round_no) + "\n")
        blob = yaml.safe_dump(written, allow_unicode=True, sort_keys=False)
        for line in blob.splitlines():
            fh.write("  " + line + "\n")  # 缩进到 cases 列表项
    log.info("persist round %d: 写入 %d 条（跳过重复 %d 条）", round_no, len(written), len(skipped))
    return written, skipped


def undo_evolution(data_dir: str | Path, names: list[str]) -> int:
    """删除 ``data/evolution.yaml`` 中给定名称的演进用例（rollback）。

    从文件加载 ``cases`` 列表，移除 ``name ∈ names`` 的项，重写回文件；若移除后
    演进文件清空则直接删除。返回移除条数。
    """
    data = Path(data_dir)
    evo = data / EVOLUTION_YAML
    if not evo.is_file():
        log.info("undo: 无演进文件，跳过")
        return 0
    raw = yaml.safe_load(evo.read_text(encoding="utf-8")) or {}
    items = raw.get("cases") if isinstance(raw, dict) else None
    name_set = set(names)
    if not isinstance(items, list):
        log.info("undo: 演进文件无可撤销列表，跳过")
        return 0
    kept = [it for it in items if str(it.get("name", "")) not in name_set]
    removed = len(items) - len(kept)
    if removed and kept:
        payload: dict[str, Any] = {"cases": kept}
        header = f"# --- {EVOLVED_SUITE} managed (追加不覆盖)，undo 可回滚 ---\n"
        dump = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        evo.write_text(header + dump, encoding="utf-8")
    elif removed and not kept:
        evo.unlink()  # 演进文件只剩本轮内容 → 直接删除
    log.info("undo: 移除 %d 条演进用例", removed)
    return removed


def load_evolution_names(data_dir: str | Path) -> list[str]:
    """读出当前 ``data/evolution.yaml`` 中已落盘的演进用例名（供 undo 定位）。"""
    data = Path(data_dir)
    evo = data / EVOLUTION_YAML
    if not evo.is_file():
        return []
    raw = yaml.safe_load(evo.read_text(encoding="utf-8")) or {}
    items = raw.get("cases") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    return [str(it.get("name", "")) for it in items if isinstance(it, dict)]


# ───────────────────────── 变更驱动（feature 3）────────────────────────

def git_changed_paths(diff_ref: str, project_dir: Path) -> list[str]:
    """返回 ``git diff <ref>`` 涉及的变更文件路径（相对仓库根）。

    非 git 仓库或命令失败时返回空列表（不因 diff 破坏演进）。
    """
    if not diff_ref:
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_dir), "diff", "--name-only", diff_ref],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("git diff 失败（忽略变更驱动）: %s", exc)
        return []
    if proc.returncode != 0:
        log.info("git diff %s 无输出/失败：%s", diff_ref, proc.stderr.strip()[:200])
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def interfaces_for_paths(changed_paths: list[str], cases: list[Case]) -> list[str]:
    """把变更文件路径映射到受影响接口（基于用例定义路径的近似匹配）。

    规则：路径片段（去掉 ``.yaml``/``.py`` 后缀与 ``data/`` 前缀）若命中某个
    用例的 ``request.path`` 子串，则该接口受影响；hermetic 场景下未命中不虚构。
    """
    if not changed_paths:
        return []
    affected: list[str] = []
    for p in changed_paths:
        stem = Path(p).stem.lower()
        for case in cases:
            path = case.request_path.lower()
            if stem and (stem in path or path in stem):
                if case.request_path not in affected:
                    affected.append(case.request_path)
    return affected


# ───────────────────────── 回归计划（feature 12）────────────────────────

def regression_plan(
    history: list[dict[str, Any]], *, risk_threshold: float = 0.3, top_k: int = 5
) -> RegressionPlan:
    """消费历史记录生成回归计划（薄封装 :func:`rank_regression`）。"""
    return rank_regression(history, risk_threshold=risk_threshold, top_k=top_k)


def escalate_suite(suite: str) -> str:
    """趋势闸用：套件自动升档（smoke→regression→prod）；未知/到顶保持。"""
    if suite not in _SUITE_CHAIN:
        return suite
    idx = _SUITE_CHAIN.index(suite)
    if idx + 1 < len(_SUITE_CHAIN):
        return _SUITE_CHAIN[idx + 1]
    return suite


# ───────────────────────── 趋势闸（feature 13）────────────────────────

@dataclass
class TrendDecision:
    """趋势闸判定的结果。"""

    recent_rate: float = 0.0
    baseline_rate: float = 0.0
    decline: float = 0.0
    escalate: bool = False
    recommended_suite: str = ""
    message: str = ""


def _pass_rate(samples: list[str]) -> float:
    if not samples:
        return 0.0
    return round(100.0 * sum(1 for s in samples if s == "PASS") / len(samples), 1)


def evaluate_trend(
    history: list[dict[str, Any]], *, drop: float = 20.0, k: int = 5
) -> TrendDecision:
    """评估近 K 次通过率是否下滑 ≥ ``drop`` 百分点。

    取历史中最近 ``2k`` 个 PASS/FAIL 样本：后 ``k`` 为近期窗口、前 ``k`` 为基线
    窗口；``baseline_rate - recent_rate >= drop`` 即判升档告警。样本不足返回不做决策。
    """
    samples = [
        str(r.get("result", "")).upper()
        for r in history
        if str(r.get("result", "")).upper() in {"PASS", "FAIL"}
    ]
    if len(samples) < 2 * k:
        return TrendDecision(message="insufficient_data")
    base = samples[-(2 * k):-k]
    recent = samples[-k:]
    baseline_rate = _pass_rate(base)
    recent_rate = _pass_rate(recent)
    decline = round(baseline_rate - recent_rate, 1)
    if decline >= drop:
        return TrendDecision(
            recent_rate=recent_rate,
            baseline_rate=baseline_rate,
            decline=decline,
            escalate=True,
            message=f"通过率下滑 {decline}%≥{drop}%，近 K 次大幅走低",
        )
    return TrendDecision(
        recent_rate=recent_rate,
        baseline_rate=baseline_rate,
        decline=decline,
        escalate=False,
        message=f"通过率相对基线下滑 {decline}%（未达 {drop}% 阈值）",
    )


# ───────────────────────── 失败自动动作（feature 11）────────────────────────

def _issue_slug(name: str) -> str:
    """从用例名生成安全文件 slug。"""
    slug = re.sub(r"[^0-9A-Za-z_\-]+", "-", str(name)).strip("-")
    return slug or "issue"


def make_issue_draft(failure: dict[str, Any], *, index: int = 0) -> str:
    """为一条失败记录生成 issue/bug 单草稿（确定性，无需 LLM）。

    草稿含标题、失败事实、失败类别与建议复现（保留请求信息，供人工接手）。
    """
    interface = str(failure.get("interface") or failure.get("name") or "unknown")
    result = str(failure.get("result") or "FAIL")
    fcls = str(failure.get("failure_class") or "unknown")
    msg = str(failure.get("failure_msg") or "")
    req = failure.get("request") or {}
    title = f"[test-fail] {interface} ({result}/{fcls})"
    lines = [
        f"# {title}",
        "",
        f"- 失败接口: `{interface}`",
        f"- 失败类别: `{fcls}`",
        f"- 失败信息: {msg[:200] or '(无)'}",
        f"- 用例索引: {index}",
        "",
        "## 复现步骤",
        "",
        "1. 通过 `ah run` 执行上述接口用例集（或直接运行 `test_*.py`）。",
        f"2. 关注 `request`: `{json.dumps(req, ensure_ascii=False)[:300]}`",
        "",
        "## 期望",
        "",
        "- 接口行为稳定，命中既有断言；若为环境/数据问题请标注为 flaky 并隔离。",
        "",
    ]
    return "\n".join(lines)


def write_issues(project_dir: Path, failures: list[dict[str, Any]]) -> list[Path]:
    """把失败 triage 结果写成 issue 草稿（写项目 ``issues/`` 目录）。"""
    if not failures:
        return []
    issues_dir = project_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for idx, failure in enumerate(failures, start=1):
        slug = _issue_slug(failure.get("interface") or failure.get("name") or idx)
        path = issues_dir / f"issue-{idx}-{slug}.md"
        path.write_text(make_issue_draft(failure, index=idx), encoding="utf-8")
        written.append(path)
    return written


# ───────────────────────── 候选生成（hermetic fallback）────────────────────────

def gen_candidates_hermetic(
    cases: list[Case],
    history: list[dict[str, Any]],
    *,
    affected: Optional[list[str]] = None,
) -> list[GeneratedCase]:
    """LLM 关闭时的候选生成：与 :class:`Predictor` 一致返回空（优雅空转）。

    仅当显式提供受影响的接口（如测试注入确定性候选工厂）时才有产出；默认空转
    使 LLMOFF 下的演进不产生任何未经 LLM 提议的用例，绝不臆造请求体。
    """
    if not affected:
        return []
    out: list[GeneratedCase] = []
    seen = {_request_key(c) for c in cases}
    for path in affected:
        method = "GET"
        key = (method, path, f"evolved-{path.strip('/').replace('/', '-')}-lim0")
        if key in seen:
            continue
        out.append(
            GeneratedCase(
                name=f"evolved-{path.strip('/').replace('/', '-')}-lim0",
                request={"method": method, "path": path, "params": {"limit": 0}},
                assert_={"status": 200},
                validated=False,
            )
        )
    return out


# ───────────────────────── AutoEvolutionLoop（feature 1,5,11,12）────────────────────────

@dataclass
class RoundOutcome:
    """单轮循环的产出摘要。"""

    round_no: int
    results: list[StepResult] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[GeneratedCase] = field(default_factory=list)
    accepted: list[GeneratedCase] = field(default_factory=list)
    rejected: list[GateResult] = field(default_factory=list)
    dedup_skipped: int = 0
    persisted: int = 0
    plan: Optional[dict[str, Any]] = None
    message: str = ""


@dataclass
class LoopReport:
    """整个演进循环的最终报告。"""

    rounds: list[RoundOutcome] = field(default_factory=list)
    stop_reason: str = ""
    total_accepted: int = 0
    evolution_file: str = ""


class AutoEvolutionLoop:
    """自主涌现闭环的引擎（hermetic 确定，可注入候选工厂）。

    参数：
    - ``data_dir``/``project_dir``：数据与项目目录。
    - ``rounds``/``budget``：最大轮数与候选采纳预算上限。
    - ``min_quality``：门控阈值。
    - ``env``/``suite``/``priority``/``tags``：本轮要跑的用例切片。
    - ``diff``：git 变更引用，走 :class:`AgentImpact` 变更驱动。
    - ``candidate_factory``：可注入的候选生成器（测试/真实 LLM 都用）。
    - ``scorer``：可注入的质量评分器。
    - ``llm``：可选的 LLM 客户端（None = LLMOFF 走确定性回退）。
    - ``gate``：可选的 LayaGate（None/未开闸 → 门控走原逻辑）。
    - ``client``：可选的 HTTP 客户端；None 时用 hermetic 隔离客户端。
    """

    def __init__(
        self,
        data_dir: str | Path,
        project_dir: str | Path,
        *,
        rounds: int = 3,
        budget: int = 10,
        min_quality: float = 0.6,
        env: str = "all",
        suite: str = "smoke",
        priority: Optional[str] = None,
        tags: Optional[list[str]] = None,
        diff: Optional[str] = None,
        llm: Any = None,
        client: Optional[HTTPClient] = None,
        candidate_factory: Optional[
            Callable[[list[Case], list[dict[str, Any]]], list[GeneratedCase]]
        ] = None,
        scorer: Optional[Callable[[GeneratedCase], float]] = None,
        gate: Any = None,
        run_suite: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.project_dir = Path(project_dir)
        self.rounds = max(1, int(rounds))
        self.budget = max(1, int(budget))
        self.min_quality = float(min_quality)
        self.env = env
        self.suite = suite
        self.priority = priority
        self.tags = list(tags or [])
        self.diff = diff
        self.llm = llm
        self._gate = gate
        self._client = client or _hermetic_client()
        self._candidate_factory = candidate_factory or self._default_factory
        self._scorer = scorer
        self._run_suite = run_suite
        self._ctx = Context()
        self.report = LoopReport()
        self._total_adopted = 0
        self._last_green = 0  # 连续全绿无采纳轮数

    # ---- 内部工厂：LLM 开启走 Predictor/Coverage，关闭走确定性回退 ----

    def _default_factory(
        self, cases: list[Case], history: list[dict[str, Any]]
    ) -> list[GeneratedCase]:
        affected = self._affected_interfaces(cases)
        if self.llm is not None:
            try:
                pred = Predictor(
                    self.llm, client=self._client, ctx=self._ctx,
                    traits="prefer boundary and adversarial inputs on affected interfaces",
                )
                proposed = pred.propose(cases, history, limit=3)
                if proposed:
                    return proposed
                cov = AgentCoverage(self.llm, client=self._client, ctx=self._ctx)
                return cov.propose(cases, limit=3)
            except Exception as exc:  # LLM 故障 → 空转，不中断演进
                log.warning("LLM 候选生成失败: %s", exc)
                return []
        return gen_candidates_hermetic(cases, history, affected=affected)

    def _affected_interfaces(self, cases: list[Case]) -> list[str]:
        """变更驱动：--diff 提供的受影响接口（hermetic 下无 LLM 走路径近似）。"""
        if not self.diff:
            return []
        changed = git_changed_paths(self.diff, self.project_dir)
        if self.llm is not None:
            impact = AgentImpact(self.llm)
            # 把解析出的变更文件路径（真实 diff）作为变更描述喂给 LLM，
            # 而非原始 ref 字符串（"HEAD~1" 对 LLM 无意义 → 之前导致受影响接口恒空）。
            change_desc = "\n".join(changed) or self.diff
            analyzed = impact.analyze(cases=cases, history=[], changes=change_desc)
            from_llm = [i.get("interface") for i in analyzed if i.get("interface")]
            if from_llm:
                return from_llm
        return interfaces_for_paths(changed, cases)

    # ---- 单轮 ----

    def _run_suite_once(self) -> tuple[list[StepResult], list[dict[str, Any]]]:
        cases = load_project_cases(self.data_dir)
        subset = select_cases(
            cases,
            env=self.env,
            suite=self.suite,
            priority=self.priority,
            tags=self.tags,
        )
        if not self._run_suite or not subset:
            return [], []
        results: list[StepResult] = []
        for case in subset:
            result = run_single(case, self._ctx, self._client)
            results.append(result)
        failures = [
            {
                "interface": r.request_detail.get("path") or r.name,
                "name": r.name,
                "result": "PASS" if r.succeeded else "FAIL",
                "failure_class": "assertion" if not r.succeeded else "",
                "failure_msg": str(r.error) if not r.succeeded else "",
                "request": r.request_detail,
            }
            for r in results
            if not r.succeeded
        ]
        return results, failures

    def step(self, round_no: int) -> RoundOutcome:
        """执行一轮：跑套件 → 生成候选 → 门控 → 持久化 → 记录日志。"""
        outcome = RoundOutcome(round_no=round_no)
        cases = load_project_cases(self.data_dir)
        # 本轮历史：无持久化历史库时用空；真实可扩展接入 HistoryStore。
        history: list[dict[str, Any]] = []
        results, failures = self._run_suite_once()
        outcome.results = results
        outcome.failures = failures
        # 失败聚类（hermetic：AgentCluster 在 llm=None 时返回 []）。
        # 聚类结果用于 triage；供后续失败动作与回归上下文复用。
        history = history or AgentCluster(self.llm).cluster(failures)
        # 回归计划：消费 rank_regression（特征 12），反推下次 suite/priority。
        plan = regression_plan(failures)
        outcome.plan = plan.to_dict()
        planned_suite = self.suite
        if plan.focused and self.suite in _SUITE_CHAIN:
            planned_suite = escalate_suite(self.suite)
        # 候选生成：先以规划套件内的用例为上下文。
        context_cases = cases or []
        candidates = self._candidate_factory(context_cases, failures)
        outcome.candidates = candidates
        # 门控过筛（特征 4）。
        judged, accepted = gate_candidates(
            candidates,
            min_quality=self.min_quality,
            llm=self.llm,
            scorer=self._scorer,
            gate=self._gate,
        )
        outcome.rejected = [g for g in judged if not g.accepted]
        outcome.accepted = accepted
        # 持久化（特征 2，含去重）。
        existing = load_project_cases(self.data_dir)
        written, skipped = persist_evolution(
            self.data_dir, accepted, round_no=round_no, env=self.env, existing=existing
        )
        outcome.persisted = len(written)
        outcome.dedup_skipped = len(skipped)
        self._total_adopted += len(written)
        # 失败自动动作（特征 11）：写 issue 草稿。
        if failures:
            write_issues(self.project_dir, failures)
        # 记录审计日志（候选/门控/采纳/拒绝/理由）。
        self._log_round(outcome, planned_suite)
        # 统计全绿无变化轮数（停车条件之一）。
        if not failures and not written:
            self._last_green += 1
        else:
            self._last_green = 0
        outcome.message = (
            f"round {round_no}: 采纳 {len(written)}，拒绝 {len(outcome.rejected)}，"
            f"跳过重复 {outcome.dedup_skipped}"
        )
        return outcome

    def _log_round(self, outcome: RoundOutcome, planned_suite: str) -> None:
        """往项目根的 `_evolution_log.jsonl` 追加一轮审计记录。"""
        log_path = self.project_dir / EVOLUTION_LOG
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "round": outcome.round_no,
            "failures": len(outcome.failures),
            "planned_suite": planned_suite,
            "candidates": [
                {
                    "name": c.name,
                    "method": (c.request or {}).get("method"),
                    "path": (c.request or {}).get("path"),
                    "score": next(
                        (g.score for g in outcome.rejected if g.candidate is c), None
                    ),
                }
                for c in outcome.candidates
            ],
            "accepted": [c.name for c in outcome.accepted],
            "rejected": [
                {"name": g.candidate.name, "reason": g.reason, "score": g.score}
                for g in outcome.rejected
            ],
            "dedup_skipped": outcome.dedup_skipped,
            "plan": outcome.plan,
        }
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ---- 停车判定 ----

    def stop_reason(self, round_no: int) -> Optional[str]:
        """返回停车原因；未到停车条件返回 None（继续下一轮）。

        Laya 前置（STAGE_STOP，noul）：仅当原确定性判定为“继续”时，才咨询
        Laya 是否提前停；Laya 只能“提前停”，绝不能“延后停”。低置信/未开闸/
        异常一律回退原确定性判定（字节等价）。
        """
        reason = self._deterministic_stop_reason(round_no)
        if reason is not None:
            # 原判定已停 → Laya 不参与，绝不把“停”改成“继续”。
            return reason
        # 走到这里说明原确定性判定为“继续”：让 Laya 决定是否提前停（保守）。
        if self._gate is not None:
            try:
                state = {
                    "round": round_no,
                    "max_rounds": self.rounds,
                    "adopted": self._total_adopted,
                    "budget": self.budget,
                    "recent_failures": self._recent_failure_count(),
                    "green_rounds": self._last_green,
                }
                q = {
                    "keep_going": {
                        "type": "noul",
                        "instructions": (
                            "当前演进是否应该继续下一轮？"
                            "根据已采纳候选数、最近轮失败数与质量判断，回答是否继续"
                        ),
                    }
                }
                dec = self._gate.best(STAGE_STOP, state, q)
                if dec.get("action") == "auto":
                    value = dec.get("value")
                    if isinstance(value, bool):
                        stop = not value
                    elif isinstance(value, str) and value.strip().lower() in {"yes", "no"}:
                        stop = value.strip().lower() == "no"
                    else:
                        stop = None  # 非法/nul 值 → 回退（继续）
                    if stop:
                        # Laya 高置信 AUTO 判停且原判定继续 → 注入提前停（保守）。
                        return "laya_no_more_value"
            except Exception as exc:  # Laya 异常 → 回退原逻辑，绝不向上抛
                log.warning("Laya 演进停车预判异常，回退确定性判定: %s", exc)
        return reason  # 原确定性判定为 None（继续）→ 保持继续

    def _deterministic_stop_reason(self, round_no: int) -> Optional[str]:
        """纯确定性停车判定（与历史逻辑字节等价，供 Laya 前置在返回前复用）。"""
        if round_no >= self.rounds:
            return f"max_rounds({self.rounds}) reached"
        if self._total_adopted >= self.budget:
            return f"budget({self.budget}) exhausted"
        last = self.report.rounds[-1] if self.report.rounds else None
        if last is not None and last.persisted == 0 and last.dedup_skipped == 0:
            return "no_new_candidate_adopted"
        if self._last_green >= 2:
            return "all_green_two_rounds_unchanged"
        return None

    def _recent_failure_count(self) -> int:
        """最近一轮失败数（供 Laya 演进停车判定的 state 语义）。"""
        last = self.report.rounds[-1] if self.report.rounds else None
        return len(last.failures) if last is not None else 0

    def run(self) -> LoopReport:
        """顺序执行多轮，直到停车条件满足。"""
        round_no = 1
        while True:
            outcome = self.step(round_no)
            self.report.rounds.append(outcome)
            reason = self.stop_reason(round_no)
            if reason is not None:
                self.report.stop_reason = reason
                break
            round_no += 1
        self.report.total_accepted = sum(r.persisted for r in self.report.rounds)
        evo = self.data_dir / EVOLUTION_YAML
        self.report.evolution_file = str(evo) if evo.is_file() else ""
        log.info(
            "演进结束：%d 轮，采纳 %d 条，停车：%s",
            len(self.report.rounds), self.report.total_accepted, self.report.stop_reason,
        )
        return self.report

    def as_dict(self) -> dict[str, Any]:
        """把最终报告序列化为可写盘/审计的字典。"""
        return {
            "rounds": [
                {
                    "round_no": r.round_no,
                    "failures": len(r.failures),
                    "candidates": len(r.candidates),
                    "accepted": len(r.accepted),
                    "rejected": len(r.rejected),
                    "persisted": r.persisted,
                    "dedup_skipped": r.dedup_skipped,
                    "message": r.message,
                }
                for r in self.report.rounds
            ],
            "stop_reason": self.report.stop_reason,
            "total_accepted": self.report.total_accepted,
            "evolution_file": self.report.evolution_file,
        }


# ───────────────────────── CLI register / run ─────────────────────────

def register(subparsers: Any) -> None:
    """把本模块的三个子命令挂到 argparse subparsers（由主代理 cli.py 调用）。"""
    # 公共参数：project 定位 + 切片 + 报告目录。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("project", nargs="?", default="demo", help="项目名或含 data/ 的目录")
    common.add_argument("--suite", default="smoke", choices=["smoke", "regression", "prod", "all"],
                        help="目标用例集（默认 smoke）")
    common.add_argument("--env", default="all", help="目标环境（如 test/prod）")
    common.add_argument("--priority", default=None, help="优先级过滤（如 P0,P1）")
    common.add_argument("--tags", default=None, help="标签过滤（逗号分隔）")
    common.add_argument("--diff", default=None, help="git 变更引用，触发变更驱动生成")
    common.add_argument("--report-dir", default=None, help="报告目录覆盖")

    p_run = subparsers.add_parser("run", help="跑一套用例并出报告", parents=[common])
    p_run.add_argument(
        "--flow", default=None,
        help="运行组装业务流程（projects/<project>/flows/<name>.yaml）",
    )
    p_run.add_argument(
        "--live", action="store_true",
        help="真实网络执行（flow 路径；默认桩传输，--live 发真实请求并启用 domains 域路由）",
    )
    p_run.set_defaults(handler=_cmd_run)

    p_evolve = subparsers.add_parser("evolve", help="自主涌现循环", parents=[common])
    p_evolve.add_argument("--rounds", type=int, default=3, help="最大轮数（默认 3）")
    p_evolve.add_argument("--budget", type=int, default=10, help="最大采纳候选预算（默认 10）")
    p_evolve.add_argument(
        "--min-quality", type=float, default=0.6, help="质量门控阈值（默认 0.6）"
    )
    p_evolve.add_argument(
        "--dry-run", action="store_true", help="不跑套件，仅生成与门控（LLM off 下）"
    )
    p_evolve.add_argument("--undo", action="store_true", help="删除最近一轮新增的演进用例")
    p_evolve.set_defaults(handler=_cmd_evolve)

    p_trend = subparsers.add_parser(
        "trend-gate", help="趋势闸：近 K 次通过率下滑告警", parents=[common]
    )
    p_trend.add_argument("--drop", type=float, default=20.0, help="下滑百分点阈值（默认 20）")
    p_trend.add_argument("--k", type=int, default=5, help="近 K 次窗口（默认 5）")
    p_trend.set_defaults(handler=_cmd_trend)

    # 顶层默认：无子命令时给出友好提示并返回非零（避免空跑静默）。
    subparsers.add_parser(
        "evolution-help", help="展示 evolution 用法"
    ).set_defaults(handler=_cmd_help)


def _cmd_help(args: argparse.Namespace) -> int:
    print("evolution 子命令：run / evolve / trend-gate。使用 --help 查看参数。", file=sys.stderr)
    return 2


def run(args: argparse.Namespace) -> int:
    """模块统一执行入口：按 register 挂载的 ``handler`` 分发。"""
    handler = getattr(args, "handler", None)
    if handler is None:
        return _cmd_unknown(args)
    return int(handler(args) or 0)


def _cmd_unknown(args: argparse.Namespace) -> int:
    print("未知 evolution 子命令；可选 run / evolve / trend-gate。", file=sys.stderr)
    return 2


def _split_tags(raw: Optional[str]) -> Optional[list[str]]:
    return [t.strip() for t in str(raw).split(",") if t.strip()] if raw else None


def _cmd_run(args: argparse.Namespace) -> int:
    """``ah run <project>``：跑一套用例并出报告。"""
    project_dir = resolve_project_dir(args.project)
    data_dir = ensure_data_dir(project_dir)
    # 本地过程链路：默认开启（AH_LOCAL_OBS=0 可关）。取共享 Tracer，注入 HTTP
    # client 与 run_single/run_flow，使 ah run 也产出 run→case→step→HTTP 链路。
    tracer = get_tracer()
    # 默认桩传输（LLMOFF CLI：稳定可复现）；--live 走真实网络并启用 domains 域路由
    # （skill 步 request.host 命中 settings.domains[host].base_url）。
    if getattr(args, "live", False):
        client = HTTPClient(
            settings.base_url or "http://hermetic.local",
            domains={name: d.base_url for name, d in settings.domains.items()},
            trace=tracer,
        )
    else:
        client = _hermetic_client(trace=tracer)
    # 组装业务流程（--flow）：装配 skill 注册表与 flow 条目，交 execute_flow 执行，
    # 与常规用例路径互斥——只跑该流程，不加载 data/ 下普通用例。
    if getattr(args, "flow", None):
        from ah.skill.executor import execute_flow
        from ah.skill.flow import build_project_flow, flow_meta, flow_path

        _flow_name, registry, entries = build_project_flow(project_dir, args.flow)
        # 顶层编排元数据（§7.3 dataset / §10.2 resources）。
        meta = flow_meta(flow_path(project_dir, args.flow))
        dataset = meta.get("dataset")
        resources = meta.get("resources")
        results: list = []
        if dataset:
            # 数据驱动：每行喂一轮 execute_flow，数据轮独立数据空间（不回写共享 ctx），
            # 汇总各轮 results；resources 在最后统一清理。
            rows = dataset if isinstance(dataset, list) else [dataset]
            for idx, row in enumerate(rows):
                row = dict(row) if isinstance(row, dict) else {}
                results.extend(
                    execute_flow(
                        entries, registry, client,
                        base_ctx=Context(), tracer=tracer,
                        dataset_row=row,
                        resources=resources if idx == len(rows) - 1 else None,
                    )
                )
        else:
            results = execute_flow(
                entries, registry, client,
                base_ctx=Context(), tracer=tracer, resources=resources,
            )
        counts = summarize_results(results)
        print(
            f"run {args.project} flow[{args.flow}]: 计划 {counts['planned']}，"
            f"执行 {counts['executed']}，验证通过 {counts['passed']}，"
            f"失败 {counts['failed']}，阻塞 {counts['blocked']}，跳过 {counts['skipped']}，"
            f"未验证 {counts['unverified']}"
        )
        failing = 0
        for res in results:
            if not res.succeeded:
                failing += 1
                log.warning("flow 步骤失败 %s: %s", res.name, res.error)
        # 流程执行与常规 run 一样，失败数非 0 时退出码 1。
        return 0 if failing == 0 else 1
    cases = load_project_cases(data_dir)
    ctx = Context()
    # 严格场景只继承运行输入快照，不继承其他场景的产出。
    seed_ctx = ctx.copy()
    subset = select_cases(
        cases, env=args.env, suite=args.suite,
        priority=args.priority, tags=_split_tags(args.tags),
    )
    # 场景流用例按 scenario 分组走 run_flow（共享 ctx，依赖上游 extract 变量）；
    # 独立用例（scenario 为空）逐个 run_single。统一收集 StepResult 供后续回归计划。
    standalone = [c for c in subset if not getattr(c, "scenario", None)]
    flow_groups: dict[str, list[Case]] = {}
    for c in subset:
        if getattr(c, "scenario", None):
            flow_groups.setdefault(c.scenario, []).append(c)

    results: list[StepResult] = []
    failing = 0
    for case in standalone:
        case_ctx = seed_ctx.copy() if case.strict else ctx
        res = run_single(case, case_ctx, client, tracer=tracer)
        results.append(res)
        if not res.succeeded:
            failing += 1
            log.warning("用例失败 %s: %s", case.name, res.error)
    for _flow_name, steps in flow_groups.items():
        # 流程内步骤并不保证都命中本次筛选（如单独筛某个接口）；此时退化为单步跑，
        # 但保留单步失败的可见性，不因流程分组把未选中的步骤强拉进来。
        runnable = [s for s in steps]
        flow_ctx = seed_ctx.copy() if any(step.strict for step in runnable) else ctx
        flow_results = run_flow(runnable, flow_ctx, client, tracer=tracer)
        for res in flow_results:
            results.append(res)
            if not res.succeeded:
                failing += 1
                log.warning("流程步骤失败 %s: %s", res.name, res.error)

    counts = summarize_results(results)
    print(
        f"run {args.project}: 计划 {counts['planned']}，执行 {counts['executed']}，"
        f"验证通过 {counts['passed']}，失败 {counts['failed']}，"
        f"阻塞 {counts['blocked']}，跳过 {counts['skipped']}，"
        f"未验证 {counts['unverified']}"
    )
# ── Agent 可消费的验证回执（对齐《让 Agent 自己跑回归》三字段契约）──
    # 只有显式打开 --json 才写报告 + 组装回执，避免改动既有 stdout/退出码语义。
    want_json = bool(getattr(args, "json", False))
    if want_json:
        from ah.reporting.writer import ResultRecord, build_report

        records = [ResultRecord.from_step(r) for r in results]
        out_dir = Path(args.report_dir) if getattr(args, "report_dir", None) else Path(settings.report_dir)  # noqa: E501
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        basename = getattr(args, "basename", None) or f"run-{args.suite}-{stamp}"
        md_path = build_report(records, report_dir=str(out_dir), basename=basename)
        started = min((r.started_at for r in records if r.started_at), default="")
        ended = max((r.ended_at for r in records if r.ended_at), default="")
        receipt = build_verification_receipt(
            results,
            environment=args.env,
            started_at=started,
            ended_at=ended,
            commit_sha=getattr(args, "commit", "") or "",
            evidence={
                "report_md": str(md_path),
                "report_json": str(md_path.with_suffix(".json")),
            },
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    # 回归计划（特征 12）：把失败记录交给 rank_regression 反推下次回归目标。
    if failing:
        rows = [
            {
                "interface": r.request_detail.get("path") or r.name,
                "result": (
                    "PASS" if r.verdict == "passed"
                    else "UNVERIFIED" if r.verdict == "unverified" else "FAIL"
                ),
                "failure_class": "assertion",
                "failure_msg": str(r.error) if not r.succeeded else "",
            }
            for r in results
        ]
        plan = regression_plan(rows)
        if plan.focused_interfaces:
            log.info(
                "下次回归建议（升档 %s）聚焦接口：%s",
                escalate_suite(args.suite), ",".join(plan.focused_interfaces),
            )
    # 盘子：冲刷本地过程链路（与 pytest_sessionfinish 的 flush 对齐），
    # 确保本次 ah run 的 run→case→step→HTTP 链路落库后再退出。
    if getattr(tracer, "flush", None):
        tracer.flush()
    return 0 if failing == 0 else 1


def _cmd_evolve(args: argparse.Namespace) -> int:
    """``ah evolve <project>``：自主涌现循环（多轮→门控→持久化→下一轮）。"""
    project_dir = resolve_project_dir(args.project)
    data_dir = ensure_data_dir(project_dir)
    if getattr(args, "undo", False):
        # 回滚：删除最近一轮新增（取演进文件中已被落盘的演进用例）。
        names = load_evolution_names(data_dir)
        if not names:
            print("无演进用例可回滚。", file=sys.stderr)
            return 1
        removed = undo_evolution(data_dir, names)
        if removed:
            print(f"已回滚 {removed} 条演进用例。")
        else:
            print("演进文件无匹配项，未删除。", file=sys.stderr)
        return 0
    loop = AutoEvolutionLoop(
        data_dir,
        project_dir,
        rounds=max(1, getattr(args, "rounds", 3)),
        budget=max(1, getattr(args, "budget", 10)),
        min_quality=float(getattr(args, "min_quality", 0.6)),
        env=args.env,
        suite=args.suite,
        priority=args.priority,
        tags=_split_tags(args.tags),
        diff=getattr(args, "diff", None),
        llm=None,  # CLI 统一 hermetic（LLMOFF 安全）；真实 LLM 由上层注入
        gate=_process_gate(),
        run_suite=not getattr(args, "dry_run", False),
    )
    report = loop.run()
    for r in report.rounds:
        log.info(r.message)
    print(json.dumps(loop.as_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_trend(args: argparse.Namespace) -> int:
    """``ah trend-gate <project> --drop P``：检测近 K 次通过率下滑。"""
    project_dir = resolve_project_dir(args.project)
    data_dir = ensure_data_dir(project_dir)
    history = _load_history_rows(project_dir, data_dir)
    decision = evaluate_trend(history, drop=float(args.drop), k=int(args.k))
    if decision.escalate:
        banner = (
            f"[趋势闸告警] 通过率 {decision.baseline_rate}%→{decision.recent_rate}% "
            f"（下滑 {decision.decline}% ≥ {args.drop}%），建议 `ah scheduled-run "
            f"--suite {escalate_suite(args.suite)}` 升档回归。"
        )
        print(banner, file=sys.stderr)
        # 报告横幅：在项目目录写一个 sidecar 标识，供报告/CI 读取。
        _write_banner(project_dir, banner)
        return 1  # 判闸：触发升档/告警 → 非零退出
    log.info("趋势闸：%s", decision.message)
    # 全绿或不足以判：零退出。
    print(f"trend-gate {args.project}: {decision.message}")
    return 0


def _load_history_rows(project_dir: Path, data_dir: Path) -> list[dict[str, Any]]:
    """从项目报告历史或演进日志汇总 `result` 序列给趋势闸。

    优先项目根 `_evolution_log.jsonl` 的失败计数，其次报告历史；无则空。
    """
    log_path = project_dir / EVOLUTION_LOG
    if log_path.is_file():
        rows: list[dict[str, Any]] = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            fails = int(entry.get("failures") or 0)
            # 用失败数近似：全 PASS 为 success；有失败记为 FAIL。
            rows.append({"result": "FAIL" if fails else "PASS"})
        return rows
    return []


def _write_banner(project_dir: Path, banner: str) -> Path:
    """写趋势闸告警横幅到项目目录（供报告渲染/CI 读取）。"""
    path = project_dir / "trend-gate-banner.txt"
    path.write_text(banner + "\n", encoding="utf-8")
    return path
