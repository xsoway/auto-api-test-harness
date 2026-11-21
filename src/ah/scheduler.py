#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2026-10-08
# @Filename: scheduler.py
# @Author  : Alan_Hsu
"""WS3 执行与调度：回归调度、变更触发、flaky 隔离仓。

本模块提供三个子命令（挂载点由主代理在 ``cli.py`` 统一编写）：
- ``scheduled-run <project> --suite smoke|regression|prod``：按用例集筛选用例，
  跑并出报告；非零退出码语义同 ``--ah-semantic-gate``（任一失败判闸）。
- ``ci-trigger <project> --diff <ref>``：对 git diff 受影响路径做影响分析，
  只跑受影响接口的用例；LLM 关闭时回退到确定性路径匹配。
- ``flaky-quarantine <project>``：历史里连续 flaky ≥ N 次的用例打
  ``tags:[quarantined]``，主回归排除、单独 ``--suite quarantine`` 可跑。

所有命令在 ``LLMOFF=1`` 下均可运行：LLM 能力走确定性回退，绝不产生网络依赖。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import httpx

from ah.core.config import activate_profile, settings
from ah.core.orchestrator import Context, HTTPClient, run_flow
from ah.failure.cluster import ClusterStore
from ah.harness.loader import Case, discover_case_files, load_cases, select_cases
from ah.predict.history import HistoryStore
from ah.predict.risk import rank_regression
from ah.reporting.writer import ResultRecord, build_report
from ah.utils.logging import get_logger

log = get_logger("scheduler")

#: flaky 隔离仓判定阈值（历史中连续 flaky 到达该次数即打标签）。
DEFAULT_FLAKY_MIN = 2
#: 隔离仓专用 suite 名（与 select_cases 的 suite 维度兼容）。
QUARANTINE_SUITE = "quarantine"
#: 隔离仓标签。
QUARANTINE_TAG = "quarantined"
#: 单次调度最多跑多少个接口（变更触发 0 时表示不设上限）。
_IMPACT_TOP_K = 64


# ───────────────────────── 项目 / 历史解析 ─────────────────────────

def resolve_project_dir(project: str) -> Path:
    """把项目名解析到 ``projects/<project>`` 或直接当作文件系统路径。

    优先级：显式存在的目录 > ``projects/<project>``。返回不含结果，则为 None。
    """
    raw = Path(project)
    if raw.is_dir():
        return raw
    candidate = Path("projects") / project
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError(
        f"项目 {project!r} 不存在：既不是路径也不是 projects/<project>。"
    )


def load_project_cases(project_dir: Path) -> list[Case]:
    """加载项目 ``data/``（或项目根）下全部 YAML 用例。"""
    data_dir = project_dir / "data"
    base = data_dir if data_dir.is_dir() else project_dir
    files = discover_case_files(base)
    if not files:
        log.warning("项目 %s 下未发现用例 YAML（%s）", project_dir, base)
    cases: list[Case] = []
    for f in files:
        cases.extend(load_cases(f))
    return cases


def report_dir_for(project_dir: Path) -> Path:
    """报告/历史目录：始终项目自带 ``report/``（自包含、隔离），不存在则创建。"""
    return project_dir / "report"


def _history_path(project_dir: Path) -> Path:
    """该项目的历史库路径（report_dir/history.jsonl）。"""
    return report_dir_for(project_dir) / "history.jsonl"


def load_project_history(project_dir: Path) -> list[dict[str, Any]]:
    """读回该项目历史记录；不存在返回空列表。"""
    return HistoryStore(_history_path(project_dir)).load()


def _append_to_history(project_dir: Path, records: list[ResultRecord]) -> None:
    """把本次调度产生的记录追加进该项目历史库。"""
    store = HistoryStore(_history_path(project_dir))
    n = store.append_all(records)
    log.info("history 追加 %d 条 -> %s", n, store.path)


# ───────────────────────── 传输层（hermetic 桩 / 实连） ─────────────────────────

def build_client(*, stub: bool = False, base_url: Optional[str] = None) -> HTTPClient:
    """构造用于调度的 HTTPClient。

    ``stub=True``（或环境变量 ``AH_SCHED_STUB=1``）时使用 httpx MockTransport，
    任何请求都返回 200 空响应，使 demo 等无真实服务的项目也能 hermetic 端到端
    跑通。默认执向 ``settings.base_url`` 真连。
    """
    use_stub = stub or _env_flag("AH_SCHED_STUB")
    url = base_url or settings.base_url
    if use_stub:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})
        raw = httpx.Client(transport=httpx.MockTransport(_handler))
        return HTTPClient(url, client=raw, timeout=5.0)
    return HTTPClient(url)


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _llm_enabled() -> bool:
    """是否允许发起真实 LLM 调用（LLMOFF 未设且存在 key）。
    失败时（依赖缺失）按关闭处理，保证 hermetic 回退可用。
    """
    try:
        from ah.llm.openai import llm_enabled
    except Exception:  # noqa: BLE001 - 依赖缺失按关闭处理。
        return False
    return bool(llm_enabled())


def _group_chains(cases: list[Case]) -> list[list[Case]]:
    """把用例按执行链分组：同场景相邻用例归一组（场景内顺序、共享 Context）。

    链 = 连续且 ``scenario`` 相同的用例（含单条 standalone/缺省场景）。
    链与链之间彼此独立（无共享 Context），可并行调度；输出仍按输入序汇总。
    """
    chains: list[list[Case]] = []
    cur: list[Case] = []
    cur_scen: Optional[tuple[str, str, bool]] = None
    for index, case in enumerate(cases):
        scen = (case.source_path, case.scenario or f"standalone:{index}", case.strict)
        if cur and cur_scen != scen:
            chains.append(cur)
            cur = []
            cur_scen = None
        cur.append(case)
        cur_scen = scen
    if cur:
        chains.append(cur)
    return chains


def run_cases(
    cases: list[Case],
    *,
    client: Optional[HTTPClient] = None,
    stub: bool = False,
    parallel: int = 1,
) -> list[ResultRecord]:
    """执行用例切片并转成 :class:`ResultRecord`。

    ``parallel=1`` 时 legacy 链之间保持共享上下文；严格链始终独立。
    ``parallel>1`` 并行调度独立链，各链上下文隔离。链内统一使用 run_flow 的
    stop/continue/依赖策略，输出始终包含每个计划步骤。
    """
    if not cases:
        return []
    own_client = client is None
    http = client or build_client(stub=stub)
    try:
        if parallel > 1:
            return _run_cases_parallel(cases, http, parallel)
        return _run_cases_sequential(cases, http)
    finally:
        if own_client:
            http.close()


def _run_chain_records(cases: list[Case], ctx: Context, http: HTTPClient) -> list[ResultRecord]:
    """复用编排策略并保留单步真实耗时（未执行步骤耗时为零）。"""
    return [
        ResultRecord.from_step(step, duration=step.duration)
        for step in run_flow(cases, ctx, http)
    ]


def _run_cases_sequential(cases: list[Case], http: HTTPClient) -> list[ResultRecord]:
    """严格场景隔离；旧串行用例仍共享变量。"""
    legacy_ctx = Context()
    records: list[ResultRecord] = []
    for chain in _group_chains(cases):
        context = Context() if any(case.strict for case in chain) else legacy_ctx
        records.extend(_run_chain_records(chain, context, http))
    return records


def _run_cases_parallel(cases: list[Case], http: HTTPClient, workers: int) -> list[ResultRecord]:
    """并行执行相互独立的用例链（同场景链内顺序、独立 Context）。

    用 ThreadPoolExecutor 提交各链；结果按输入顺序回填，保证记录顺序稳定。
    """
    chains = _group_chains(cases)
    indexed: list[tuple[list[Case], list[int]]] = []
    cursor = 0
    for chain in chains:
        group = list(range(cursor, cursor + len(chain)))
        cursor += len(chain)
        indexed.append((chain, group))
    records: list[Optional[ResultRecord]] = [None] * len(cases)

    def run_chain(chain: list[Case], group: list[int]) -> None:
        """单链：各自独立 Context、链内顺序推进；结果写入对应位置。"""
        ctx = Context()
        for orig_idx, record in zip(group, _run_chain_records(chain, ctx, http)):
            records[orig_idx] = record

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_chain, chain, group) for (chain, group) in indexed]
        for fut in futures:
            fut.result()  # 让异常（如有）冒泡，等待全部完成后统一返回
    return [r for r in records if r is not None]


def write_report(
    records: list[ResultRecord],
    *,
    project_dir: Path,
    suite: str,
    basename: Optional[str] = None,
) -> Path:
    """写报告 + 追加历史；返回 md 报告路径。"""
    out_dir = report_dir_for(project_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    name = basename or f"scheduled-{suite}-{stamp}"
    md_path = build_report(
        records,
        report_dir=str(out_dir),
        basename=name,
        history_rows=HistoryStore(_history_path(project_dir)).load(),
    )
    # 参与回归计划：scheduled-run 消费 rank_regression 计划做截图反推。
    history = load_project_history(project_dir)
    if history:
        plan = rank_regression(history)
        log.info("回归计划：%s", plan.to_dict() if hasattr(plan, "to_dict") else plan)
    return md_path


def _failed_count(records: list[ResultRecord]) -> int:
    return sum(1 for r in records if r.result != "PASS")


# ───────────────────────── 隔离仓 ─────────────────────────

def find_flaky_cases(
    history_rows: list[dict[str, Any]],
    *,
    min_consecutive: int = DEFAULT_FLAKY_MIN,
) -> list[str]:
    """从历史记录里挑出"连续 flaky ≥ N 次"的用例名。

    Hermetic、纯本地算术。按用例名分组后按写入顺序扫描，统计每个用例
    连续 ``flaky=True``（非首过即绿，见 :class:`ResultRecord`）的最长连击；
    达到 ``min_consecutive`` 即判为需隔离。返回用例名列表（保持首次命中顺序）。
    """
    if min_consecutive <= 0:
        min_consecutive = DEFAULT_FLAKY_MIN
    # 按用例名分组，保持首次出现顺序。
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in history_rows:
        name = str(row.get("name") or row.get("interface") or "")
        if not name:
            continue
        groups.setdefault(name, []).append(row)
    hits: list[str] = []
    for name, rows in groups.items():
        streak = 0
        for row in rows:
            if bool(row.get("flaky")):
                streak += 1
                if streak >= min_consecutive:
                    hits.append(name)
                    break
            else:
                streak = 0
    return hits


def _add_tag_to_case(case: Case, tag: str) -> bool:
    """为用例追加标签（幂等）；返回是否发生变化。"""
    if tag in case.tags:
        return False
    case.tags.append(tag)
    return True


def _write_quarantine_back(case: Case, tag: str) -> bool:
    """把标签写回该用例的源 YAML 文件（追加式，不重写其它字段）。

    仅支持"单用例映射"形态（顶层有 ``name``）；复杂场景/列表形态无法安全回写，
    返回 False 交由调用方在报告中标注。已有 ``tags`` 键时并入新标签（避免
    YAML 重复键覆盖），无则挂到 ``name`` 行之后。
    """
    if not case.source_path:
        return False
    path = Path(case.source_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("无法读取 %s 打标签：%s", path, exc)
        return False
    tag_token = f"- {tag}"
    if tag_token in text:
        return True  # 已打过该标签。
    lines = text.splitlines()
    name_line = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*name\s*:", line):
            name_line = i
            break
    if name_line is None:
        return False
    indent = re.match(r"^(\s*)name\s*:", lines[name_line]).group(1)
    # 1) 文件已有同层级 tags 键 → 并入（inline 或 block 列表）。
    for i, line in enumerate(lines):
        if not re.match(rf"^{indent}tags\s*:", line):
            continue
        if re.match(rf"^{indent}tags\s*:\s*\[", line):
            # inline 列表：tags: [a, b] -> 末尾追加。
            if "[" in line and line.rstrip().endswith("]"):
                lines[i] = line.rstrip()[:-1] + f", {tag}]"
                return _flush(path, lines)
        else:
            # block 列表：tags:\n  - a\n  - b -> 在其下追加一项。
            item_indent = indent + "  "
            last = i + 1
            while last < len(lines) and re.match(
                rf"^{item_indent}- ", lines[last]
            ):
                last += 1
            lines.insert(last, f"{item_indent}- {tag}")
            return _flush(path, lines)
    # 2) 无 tags 键 → 在 name 行之后插入 tags 行（保持合法 YAML）。
    lines.insert(name_line + 1, f"{indent}tags: [{tag}]")
    return _flush(path, lines)


def _flush(path: Path, lines: list[str]) -> bool:
    """把改写的行写回文件；失败返回 False。"""
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("无法写回 %s：%s", path, exc)
        return False
    return True


def quarantine_cases(
    cases: list[Case],
    flaky_names: list[str],
    *,
    write_back: bool = False,
) -> tuple[list[str], list[str]]:
    """为匹配 flaky 名单的用例打 ``quarantined`` 标签。

    ``write_back=True`` 时把标签写回源 YAML。返回 ``(quarantined, annotated)``：
    - ``quarantined``：成功打标签（含写回或仅在内存）的用例名；
    - ``annotated``：无法写回但已在报告/日志标注的用例名。
    """
    want = set(flaky_names)
    quarantined: list[str] = []
    annotated: list[str] = []
    for case in cases:
        if case.name not in want:
            continue
        changed = _add_tag_to_case(case, QUARANTINE_TAG)
        if write_back and changed:
            if _write_quarantine_back(case, QUARANTINE_TAG):
                quarantined.append(case.name)
                log.info("已隔离并写回：%s", case.name)
                continue
            annotated.append(case.name)
            log.warning("无法写回 %s，仅在报告中标注", case.name)
        else:
            quarantined.append(case.name)
    return quarantined, annotated


def _quarantined_names(cases: list[Case]) -> set[str]:
    return {c.name for c in cases if QUARANTINE_TAG in c.tags}


def load_project_clusters(project_dir: Path) -> Optional[ClusterStore]:
    """加载项目的失败簇群库；文件不存在/读失败返回 None（回退历史计数）。"""
    path = report_dir_for(project_dir) / "failure_clusters.json"
    cs = ClusterStore(path)
    if not cs.states():
        # 空簇（无文件或无任何记录）→ 视为未使用簇群，返回 None 让调用方回退。
        return None
    return cs


def find_cluster_quarantined(clusters: ClusterStore) -> list[str]:
    """返回簇群状态机判定为 quarantined 的用例名（去重、保持首次出现顺序）。

    对应文章2 状态机：簇群主动观察（flaky_watch）期仍失败升级为 quarantined，
    是隔离仓判定的事实来源；调度据此跳过执行，而非仅靠历史连击计数。
    """
    seen: set[str] = set()
    out: list[str] = []
    for cl in clusters.quarantined_keys():
        cl_data = clusters.get(cl) or {}
        for cname in cl_data.get("cases", []):
            if cname and cname not in seen:
                seen.add(cname)
                out.append(cname)
    return out


def _exclude_quarantined(
    cases: list[Case], project_dir: Path, *, log_prefix: str
) -> list[Case]:
    """剔除处于 quarantined 簇群的用例（簇群在手时用簇群状态，否则用标签）。

    返回值不包含被剔除的用例；剔除名单与来源写入日志（tag/cluster）。
    """
    excluded_tags = _quarantined_names(cases)
    clusters = load_project_clusters(project_dir)
    excluded_clusters = []
    if clusters is not None:
        excluded_clusters = find_cluster_quarantined(clusters)
    to_exclude = excluded_tags | set(excluded_clusters)
    if not to_exclude:
        return cases
    kept = [c for c in cases if c.name not in to_exclude]
    log.info(
        "%s剔除 %d 例隔离仓用例（%d 来自簇群状态）：%s",
        log_prefix, len(to_exclude),
        len({c.name for c in cases if c.name in excluded_clusters}),
        sorted(to_exclude),
    )
    return kept


# ───────────────────────── 变更触发（影响分析） ─────────────────────────

def _interface_token(path: str) -> str:
    """从请求路径提取接口主键：去掉 scheme/host/query，保留路径主体。"""
    path = path.split("?")[0]
    path = re.sub(r"^https?://[^/]+", "", path)
    return path.rstrip("/") or "/"


def _changed_interface(case: Case, changed_path: str) -> bool:
    """确定性路径匹配：变更文件路径是否命中该用例接口。

    规则（LLM 关闭时的回退）：
    1. 变更路径与用例 ``request.path`` 共享非空路径前缀；
    2. 变更路径是项目下用例 YAML（``*.yaml``），且用例来自该文件；
    3. 变更路径包含用例接口尾段（如 ``/users`` 命中 ``…/users``）。
    """
    req_path = _interface_token(case.request_path)
    if not req_path or req_path == "/":
        return False
    changed = changed_path.replace("\\", "/")
    if changed.endswith(".yaml"):
        # 用例 YAML 变更：命中出自该文件的用例。
        if case.source_path and Path(case.source_path).as_posix().endswith(
            changed.split("/")[-1]
        ):
            return True
    req_root = "/" + req_path.split("/", 1)[1] if req_path.startswith("/") else req_path
    # 前缀匹配：变更路径是接口路径的父/自路径，或接口是变更路径的子段。
    if changed.endswith(req_root) or req_root in changed:
        return True
    return False


def select_impacted_cases(
    cases: list[Case],
    changed_paths: list[str],
    *,
    llm: Any = None,
    changes_text: str = "",
    history_rows: Optional[list[dict[str, Any]]] = None,
) -> list[Case]:
    """按变更路径选取受影响接口用例。

    优先 ``AgentImpact``（LLM 开启时）；Hermetic（``llm is None``）回退到
    确定性路径匹配（:func:`_changed_interface`）——零网络依赖、可测试。
    返回按原顺序去重后的用例切片（受影响的顺序与输入一致）。
    """
    if not changed_paths:
        return []
    if llm is not None:
        try:
            impacted = llm.analyze(
                cases=cases,
                history=history_rows or [],
                changes=changes_text or " ".join(changed_paths),
                top_k=_IMPACT_TOP_K,
            )
            impacted_interfaces = [str(i.get("interface", "")) for i in impacted]
            impacted_interfaces = [i for i in impacted_interfaces if i]
        except Exception as exc:  # LLM 失败不阻断，回退确定性匹配。
            log.warning("AgentImpact 失败，回退确定性路径匹配：%s", exc)
            impacted_interfaces = []
        if impacted_interfaces:
            want = set()
            for iface in impacted_interfaces:
                token = _interface_token(iface)
                for c in cases:
                    if token and token.endswith(_interface_token(c.request_path)):
                        want.add(c.name)
            if want:
                selected = [c for c in cases if c.name in want]
                if selected:
                    return selected
    selected: list[Case] = []
    seen: set[str] = set()
    for changed in changed_paths:
        for c in cases:
            if c.name in seen:
                continue
            if _changed_interface(c, changed):
                seen.add(c.name)
                selected.append(c)
    return selected


def _git_diff_changed_paths(repo_root: Path, ref: str) -> list[str]:
    """返回 git diff（相对仓库）中被改动/新增的文件路径。"""
    base = ["git", "-C", str(repo_root), "diff", "--name-only"]
    # 依次尝试三种对比方向，拿到非空结果即返回。
    for arg in (ref, f"{ref}..HEAD", f"HEAD..{ref}"):
        try:
            out = subprocess.run(base + [arg], capture_output=True, text=True,
                                 timeout=30)
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("git diff 失败（%s）：%s", arg, exc)
            continue
        if out.returncode == 0:
            paths = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
            if paths:
                return paths
    return []


def _repo_root_for(project_dir: Path) -> Path:
    """向上找到包含 ``.git`` 的仓库根；找不到就用项目目录自身。"""
    cur: Path = project_dir.resolve()
    for _ in range(8):
        if (cur / ".git").exists():
            return cur
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return project_dir.resolve()


# ───────────────────────── 子命令入口 ─────────────────────────

def _add_suite_argument(sub: Any) -> None:
    sub.add_argument(
        "--suite",
        default=None,
        help=(
            f"用例集：smoke|regression|prod|all|{QUARANTINE_SUITE}；"
            "缺省 = regression。"
        ),
    )


def _require_project(sub: Any) -> None:
    sub.add_argument("project", help="业务项目名（projects/<project>）或目录路径。")


def register(subparsers: Any) -> None:
    """把本模块的 3 个子命令挂到 argparse subparsers。"""
    # scheduled-run：回归调度器
    p_run = subparsers.add_parser(
        "scheduled-run",
        help="按用例集跑回归并出报告（非零退出码 = 判闸失败）。",
    )
    _require_project(p_run)
    _add_suite_argument(p_run)
    p_run.add_argument("--priority", default=None, help="优先级过滤：P0|P1|P2。")
    p_run.add_argument("--env", default=None, help="环境过滤：all|test|prod。")
    p_run.add_argument("--tags", default=None, help="逗号分隔标签过滤。")
    p_run.add_argument("--basename", default=None, help="报告文件名主干。")
    p_run.add_argument(
        "--stub",
        action="store_true",
        help="用桩传输（MockTransport）hermetic 端到端跑，不连真实服务。",
    )
    p_run.add_argument(
        "--no-gate",
        action="store_true",
        help="关闭判闸（任一失败仍返回 0）。",
    )
    p_run.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="并行 worker 数（>1 时只并行相互独立的用例链，同场景仍按顺序）。",
    )
    p_run.add_argument(
        "--profile",
        default=None,
        help="多环境 profile 名（AH_PROFILE 之外显式指定，覆盖 base_url/domains）。",
    )
    p_run.set_defaults(func=run_scheduled_run)

    # ci-trigger：变更触发
    p_ci = subparsers.add_parser(
        "ci-trigger",
        help="按 git diff 受影响接口只跑相关用例。",
    )
    _require_project(p_ci)
    p_ci.add_argument(
        "--diff",
        default="HEAD",
        help="git 参考 ref（如 origin/main）；默认 HEAD（对比工作区改动）。",
    )
    p_ci.add_argument(
        "--changes",
        default=None,
        help="(测试/显式) 空格分隔的变更文件路径，优先于 --diff 的 git 解析。",
    )
    p_ci.add_argument(
        "--stub",
        action="store_true",
        help="用桩传输 hermetic 端到端跑。",
    )
    p_ci.add_argument(
        "--no-gate",
        action="store_true",
        help="关闭判闸（任一失败仍返回 0）。",
    )
    p_ci.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="并行 worker 数（>1 时只并行相互独立的用例链）。",
    )
    p_ci.add_argument(
        "--profile",
        default=None,
        help="多环境 profile 名（AH_PROFILE 之外显式指定，覆盖 base_url/domains）。",
    )
    p_ci.set_defaults(func=run_ci_trigger)

    # flaky-quarantine：隔离仓
    p_q = subparsers.add_parser(
        "flaky-quarantine",
        help="把历史中连续 flaky ≥ N 次的用例打 quarantined 标签并写回 YAML。",
    )
    _require_project(p_q)
    p_q.add_argument(
        "--min",
        type=int,
        default=DEFAULT_FLAKY_MIN,
        help=f"连续 flaky 次数阈值（默认 {DEFAULT_FLAKY_MIN}）。",
    )
    p_q.add_argument(
        "--dry-run",
        action="store_true",
        help="只列出待隔离用例，不写回 YAML。",
    )
    p_q.set_defaults(func=run_flaky_quarantine)


def _filter_suite(cases: list[Case], suite: Optional[str], priority: Optional[str],
                  env: Optional[str], tags: Optional[str]) -> list[Case]:
    """组合筛选（select_cases AND 语义）。suite 缺省 = regression。

    特殊：suite == ``quarantine`` 时按隔离仓标签（quarantined）挑选，忽略
    ``case.suite`` 维度——使被隔离用例可单独跑（设计 §8-f3）。
    """
    want = (suite or "regression").strip()
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    if want == QUARANTINE_SUITE:
        # 隔离仓：命中 quarantined 标签（可再叠加环境/优先级/用户标签）。
        select_tags = [QUARANTINE_TAG] + (tag_list or [])
        selected = select_cases(
            cases,
            env=env,
            suite=None,
            priority=priority,
            tags=select_tags,
            order_by="risk",
        )
        return selected
    return select_cases(
        cases,
        env=env,
        suite=want,
        priority=priority,
        tags=tag_list,
        order_by="risk",
    )


def _activate_profile_from_args(args: Any) -> None:
    """应用 CLI ``--profile``（若有）：原位切换模块级 settings 的多环境 profile。"""
    profile = getattr(args, "profile", None)
    if profile:
        activate_profile(str(profile))
        log.info("已切换到 profile=%s base_url=%s", profile, settings.base_url)


def run_scheduled_run(args: Any) -> int:
    """``ah scheduled-run <project> --suite <suite> [...]``。"""
    _activate_profile_from_args(args)
    project_dir = resolve_project_dir(args.project)
    cases = load_project_cases(project_dir)
    suite = (args.suite or "regression").strip()
    log.info(
        "scheduled-run %s suite=%s（共加载 %d 例）",
        project_dir, suite, len(cases),
    )
    # 隔离仓用例：只有显式 --suite quarantine 才纳入跑；主回归默认剔除。
    # 剔除来源 = quarantined 标签 ∪ 簇群状态机判定的 quarantined 簇。
    if suite != QUARANTINE_SUITE:
        cases = _exclude_quarantined(cases, project_dir, log_prefix="scheduled-run")
    selected = _filter_suite(cases, suite, args.priority, args.env, args.tags)
    if not selected:
        log.warning("suite=%s 未命中任何用例；视为通过（0）。", suite)
        return 0
    log.info("命中 %d 例待跑 %s", len(selected), suite)
    records = run_cases(selected, stub=bool(getattr(args, "stub", False)),
                        parallel=int(getattr(args, "parallel", 1)))
    md = write_report(records, project_dir=project_dir, suite=suite,
                      basename=getattr(args, "basename", None))
    _append_to_history(project_dir, records)
    failed = _failed_count(records)
    log.info(
        "scheduled-run 完成：%d/%d 通过，报告=%s",
        len(records) - failed, len(records), md,
    )
    if failed and not getattr(args, "no_gate", False):
        log.error("存在失败判闸：%d 例未通过（退出码非 0）。", failed)
        return 1
    return 0


def run_ci_trigger(args: Any) -> int:
    """``ah ci-trigger <project> --diff <ref> [--changes ...]``。"""
    _activate_profile_from_args(args)
    project_dir = resolve_project_dir(args.project)
    cases = load_project_cases(project_dir)
    # 变更路径来源：显式 --changes 优先（可测试、无需 git 仓库），否则 git diff。
    if getattr(args, "changes", None):
        changed_paths = [p.strip() for p in args.changes.split() if p.strip()]
    else:
        repo_root = _repo_root_for(project_dir)
        changed_paths = _git_diff_changed_paths(repo_root, args.diff)
    if not changed_paths:
        log.warning("未检测到任何变更路径；跳过（0）。")
        return 0
    changed_paths = [p for p in changed_paths if not _is_ignored_path(p)]
    if not changed_paths:
        log.warning("变更均为无关文件；跳过（0）。")
        return 0
    log.info("变更文件 %d 个：%s", len(changed_paths), changed_paths[:20])
    # LLM 影响分析（AgentImpact）：开启时交给 LLM 判定，关闭时走确定性路径匹配。
    llm = None
    if _llm_enabled():
        try:
            from ah.llm.openai import LLMClient
            from ah.predict.assist import AgentImpact
        except Exception:  # noqa: BLE001 - LLM 依赖缺失静默回退。
            llm = None
        else:
            llm = AgentImpact(LLMClient())
    changed_text = " ".join(changed_paths)
    impacted = select_impacted_cases(
        cases,
        changed_paths,
        llm=llm,
        changes_text=changed_text,
        history_rows=load_project_history(project_dir),
    )
    if not impacted:
        log.warning("变更未命中任何用例接口；跳过（0）。")
        return 0
    log.info("命中受影响用例 %d 例：%s", len(impacted), [c.name for c in impacted])
    # 隔离仓用例不纳入 CI 触发主跑（除非显式在变更文件里）。
    records = run_cases(impacted, stub=bool(getattr(args, "stub", False)),
                        parallel=int(getattr(args, "parallel", 1)))
    md = write_report(records, project_dir=project_dir, suite="ci",
                      basename=f"ci-{args.diff.replace('/', '-')}")
    _append_to_history(project_dir, records)
    failed = _failed_count(records)
    log.info("ci-trigger 完成：%d/%d 通过，报告=%s", len(records) - failed,
             len(records), md)
    if failed and not getattr(args, "no_gate", False):
        log.error("受影响用例存在失败判闸（%d 例）。", failed)
        return 1
    return 0


def _is_ignored_path(path: str) -> bool:
    """跳过与接口无关的变更文件（文档/构建产物/锁文件等）。"""
    p = path.lower()
    if p.startswith((".git/", "docs/", "artifacts/", "node_modules/", "dist/")):
        return True
    if p.endswith((".md", ".txt", ".lock", ".png", ".jpg", ".svg", ".ico")):
        return True
    return False


def run_flaky_quarantine(args: Any) -> int:
    """``ah flaky-quarantine <project> [--min N] [--dry-run]``。"""
    project_dir = resolve_project_dir(args.project)
    cases = load_project_cases(project_dir)
    history = load_project_history(project_dir)
    flaky_names = find_flaky_cases(
        history, min_consecutive=int(getattr(args, "min", DEFAULT_FLAKY_MIN))
    ) if history else []
    # 簇群状态机补充：状态为 quarantined 的簇对应用例也纳入隔离决策。
    clusters = load_project_clusters(project_dir)
    cluster_names = find_cluster_quarantined(clusters) if clusters else []
    merged = list(dict.fromkeys(flaky_names + cluster_names))
    if not merged:
        log.info(
            "连续 flaky ≥ %d 的用例：无；簇群 quarantined 用例：%d（%s）",
            getattr(args, "min", DEFAULT_FLAKY_MIN),
            len(cluster_names), cluster_names,
        )
        return 0
    if cluster_names:
        log.info(
            "簇群状态命中 quarantined %d 例：%s（历史计数命中 %d 例）",
            len(cluster_names), cluster_names, len(flaky_names),
        )
    present = [n for n in merged if any(c.name == n for c in cases)]
    if not present:
        log.warning("隔离名单命中 %d 个用例但不在当前用例集；跳过（0）。", len(merged))
        return 0
    quarantined, annotated = quarantine_cases(
        cases, present, write_back=not bool(getattr(args, "dry_run", False))
    )
    log.info(
        "隔离结果：打标 %d 例 %s，标注 %d 例（无法写回）%s",
        len(quarantined), quarantined,
        len(annotated), annotated,
    )
    if getattr(args, "dry_run", False):
        log.info("dry-run：仅预览，未写回 YAML。")
    return 0
