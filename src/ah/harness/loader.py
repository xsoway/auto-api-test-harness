"""YAML 用例加载。

``load_cases(path)`` 将 YAML 文件解析为 :class:`Case` 数据类。一个文件可包含
单个用例映射、``cases:`` 下的列表，或与用例并列的 ``on_failure`` 标志。
场景流（``scenario:`` 下的有序步骤列表）被展开为独立的 :class:`Case` 对象，
并标记共享的场景 id，由 runner 按顺序执行（第 7 步）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class Case:
    """一个可执行步骤：request + extract + assert + vars + hooks。"""

    name: str
    request: dict[str, Any]
    extract: dict[str, dict[str, Any]] = field(default_factory=dict)
    assert_: dict[str, Any] = field(default_factory=dict)
    vars_req: list[str] = field(default_factory=list)
    vars_output: list[str] = field(default_factory=list)
    auth: Optional[str] = None  # 传给令牌管理器的令牌名
    llm_gen: dict[str, Any] = field(default_factory=dict)
    on_failure: str = "stop"  # "stop" | "continue"
    scenario: Optional[str] = None  # 作为某个流程一部分时的共享 id
    order: int = 0  # 场景内步骤的先后顺序
    source_path: str = ""
    env: str = "all"  # 目标环境：all | test | prod（可逗号并列，见 _norm_env）
    priority: str = "P2"  # 重要级：P0 | P1 | P2
    suite: str = "regression"  # 用例集：smoke | regression | prod | all
    tags: list[str] = field(default_factory=list)  # 自由标签，供选择器匹配

    # 可配置运行重试：失败后（仅瞬时错误：5xx/超时/连接）最多再尝试 retries 次，
    # 每次间隔 retry_backoff 秒。区别于 L1 自愈重试（固定一次、语义绑定 self-heal）。
    retries: int = 0  # 额外重试次数（0 = 关闭可配置重试）
    retry_backoff: float = 0.0  # 重试之间的退避秒数
    # 用例级超时（秒）：覆盖 HTTPClient 默认 timeout；缺省 None 用客户端默认。
    timeout: Optional[float] = None
    # 软断言：True 时断言失败只记录不判失败（用例仍算通过，供非阻断性检查）。
    soft_assert: bool = False
    reference_mode: str = "legacy"
    strict: bool = False
    depends_on: list[str] = field(default_factory=list)
    # 声明式数据夹具：``[{name, strategy|value, prefix?, length?, seed?}]``。
    # 场景级声明会挂到同场景每个 step；运行时在链执行前 materialize 进 Context。
    variables: list[dict[str, Any]] = field(default_factory=list)

    @property
    def envs(self) -> set[str]:
        """规范化后的目标环境集合（逗号分隔展开、去空白）。"""
        return _expand_env(self.env)

    @property
    def rank(self) -> int:
        """优先级排序键：P0=0, P1=1, P2=2；未知按 P2 处理。"""
        p = self.priority.upper().lstrip("P")
        if p in ("0", "1", "2"):
            return int(p)
        return 2

    @property
    def host(self) -> Optional[str]:
        """目标服务域（``request.host``）。缺省 None -> 默认 base_url。"""
        host = self.request.get("host")
        return str(host) if isinstance(host, str) and host else None

    @property
    def request_path(self) -> str:
        return str(self.request.get("path", ""))

def _merge_variables(data: dict[str, Any], top_vars: list[Any]) -> dict[str, Any]:
    """把顶层 ``variables`` 合并进单步数据；步骤自带 ``variables`` 优先。

    返回新 dict，不改动入参；顶层无声明时直接返回原 dict（避免无谓拷贝）。
    """
    if not top_vars or data.get("variables"):
        return data
    merged = dict(data)
    merged["variables"] = list(top_vars)
    return merged


def _from_mapping(data: dict[str, Any], *, source: str, scenario: Optional[str] = None,
                  order: int = 0, on_failure_default: str = "stop",
                  schema_version: int = 1) -> Case:
    req = data.get("request", {}) or {}
    if not isinstance(req, dict):
        raise ValueError(f"case {data.get('name', '<anon>')!r}: 'request' must be a mapping")
    reference_mode = str(data.get("reference_mode", "legacy"))
    if reference_mode not in {"legacy", "typed"}:
        raise ValueError(f"invalid reference_mode: {reference_mode!r}")
    return Case(
        name=str(data.get("name", req.get("path", f"case-{order}"))),
        request=req,
        extract=data.get("extract", {}) or {},
        assert_=data.get("assert", {}) or {},
        vars_req=list(data.get("vars_req", []) or []),
        vars_output=list(data.get("vars_output", []) or []),
        auth=data.get("auth"),
        llm_gen=data.get("llm_gen", {}) or {},
        on_failure=str(data.get("on_failure", on_failure_default)),
        scenario=scenario,
        order=order,
        source_path=source,
        env=_norm_str(data.get("env")) or "all",
        priority=_norm_str(data.get("priority")) or "P2",
        suite=_norm_str(data.get("suite")) or "regression",
        tags=_norm_list(data.get("tags")),
        retries=_norm_int(data.get("retries")),
        retry_backoff=_norm_float(data.get("retry_backoff")),
        timeout=_norm_opt_float(
            data.get("timeout") if data.get("timeout") is not None else req.get("timeout")
        ),
        soft_assert=_norm_bool(data.get("soft_assert", False)),
        reference_mode="typed" if schema_version == 2 else reference_mode,
        strict=schema_version == 2 or _norm_bool(data.get("strict", False)),
        depends_on=_norm_list(data.get("depends_on")),
        variables=list(data.get("variables", []) or []),
    )
def _norm_str(value: Any) -> str:
    """把标量/列表规范化为逗号分隔字符串；空值返回空串。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    text = str(value).strip()
    return text


def _norm_list(value: Any) -> list[str]:
    """把标量/列表规范化为非空字符串列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []
def _norm_int(value: Any) -> int:
    """规范化为非负整数；缺失/非法/负值返回 0（关闭）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _norm_float(value: Any) -> float:
    """规范化为非负秒数；缺失/非法返回 0.0。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return f if f > 0 else 0.0


def _norm_bool(value: Any) -> bool:
    """把 YAML 布尔/字符串规范化为 bool；缺失/非法返回 False。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

def _norm_opt_float(value: Any) -> Optional[float]:
    """规范化为可选正数（秒）；缺失/非法/非正返回 None（用客户端默认）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _expand_env(env: str) -> set[str]:
    """展开逗号分隔环境字段为小写去空白集合。"""
    return {e.strip().lower() for e in str(env or "").split(",") if e.strip()}


def select_cases(
    cases: list[Case],
    *,
    env: Optional[str] = None,
    suite: Optional[str] = None,
    priority: Optional[str] = None,
    tags: Optional[list[str]] = None,
    order_by: Optional[str] = None,
) -> list[Case]:
    """按环境/用例集/优先级/标签筛选用例。

    每个维度都视为 *必须具备*（AND）；缺省维度不参与过滤。``env`` 匹配用例
    ``Case.envs`` 与目标 ``env`` 的交集（用例标 ``all`` 恒匹配任何 env）；
    ``suite`` 匹配 ``Case.suite``（``all`` 恒匹配）；``priority`` 与
    ``tags`` 均为集合匹配（用例含任一目标即命中）。默认保持原顺序。

    ``order_by="risk"`` 时按风险降序重排输出（提案 §3.2）：有历史画像的接口
    高风险在前，无历史记录的接口按原相对顺序排后。纯本地算术（hermetic）。

    典型调用：冒烟 = ``select_cases(cases, suite="smoke", env="test")``；
    生产回归 P0 = ``select_cases(cases, env="prod", suite="regression",
    priority="P0")``。
    """
    out: list[Case] = []
    for case in cases:
        if not _match_env(case, env):
            continue
        if not _match_suite(case, suite):
            continue
        if priority and case.priority.upper() not in {
            p.upper() for p in _norm_list(priority)
        }:
            continue
        if tags and not set(case.tags).intersection(_norm_list(tags)):
            continue
        out.append(case)
    if order_by == "risk":
        return _order_by_risk(out)
    return out


def _match_env(case: Case, env: Optional[str]) -> bool:
    if env is None:
        return True
    targets = _expand_env(env)
    if not targets:
        return True
    # 目标为 ``all`` 时视为“任何环境都行”（与 _match_suite 的 all 通配一致），
    # 否则具体环境必须与用例 envs 有交集；用例自身标 ``all`` 恒匹配。
    if targets == {"all"}:
        return True
    case_envs = case.envs
    if "all" in case_envs:
        return True
    return bool(case_envs.intersection(targets))


def _match_suite(case: Case, suite: Optional[str]) -> bool:
    if suite is None:
        return True
    want = {s.strip().lower() for s in _norm_list(suite) if s.strip()}
    if not want:
        return True
    case_suite = {case.suite.strip().lower()}
    if "all" in case_suite:
        return True
    return bool(case_suite.intersection(want))


def _order_by_risk(cases: list[Case]) -> list[Case]:
    """按接口历史风险降序重排切片（hermetic，无历史时保持原顺序）。

    从 ``settings.report_dir/history.jsonl`` 读取历史记录 → 计算各接口风险画像；
    切片里命中画像的接口按 ``(risk, confidence, total)`` 降序排前，未命中的
    接口保持原相对顺序排后。纯本地算术，绝不触碰网络。
    """
    try:
        from ah.predict.history import HistoryStore
        from ah.predict.risk import RiskProfile, risk_profiles
    except Exception:
        return cases  # 依赖不可用 → 退化为原顺序

    try:
        rows = HistoryStore().load()
    except Exception:
        return cases
    if not rows:
        return cases
    profiles = {p.interface: p for p in risk_profiles(rows)}
    ranked: list[tuple[Case, RiskProfile]] = []
    rest: list[Case] = []
    for case in cases:
        prof = profiles.get(case.name) or profiles.get(str(case.request_path))
        (ranked.append((case, prof)) if prof else rest.append(case))
    ranked.sort(key=lambda cp: (cp[1].risk, cp[1].confidence, cp[1].total), reverse=True)
    return [case for case, _ in ranked] + rest

def load_cases(path: str | Path) -> list[Case]:
    """从 YAML 文件加载所有用例（单用例、列表或场景形式）。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"case file not found: {p}")
    raw = _read_yaml(p)
    source = str(p)
    version = raw.get("schema_version", 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError(f"unsupported schema_version: {version!r}")
    mode = str(raw.get("reference_mode", "legacy"))
    if mode not in {"legacy", "typed"}:
        raise ValueError(f"invalid reference_mode: {mode!r}")
    cases: list[Case] = []
    scenario_vars = raw.get("variables", []) or []

    if "scenario" in raw and isinstance(raw.get("scenario"), list):
        scenario_id = str(raw.get("name", p.stem))
        default_on_failure = str(raw.get("on_failure", "stop"))
        for idx, step in enumerate(raw["scenario"]):
            step = _merge_variables(step, scenario_vars)
            cases.append(
                _from_mapping(
                    step,
                    source=source,
                    scenario=scenario_id,
                    order=idx,
                    on_failure_default=default_on_failure,
                    schema_version=version,
                )
            )
        return cases

    if "cases" in raw and isinstance(raw.get("cases"), list):
        default_on_failure = str(raw.get("on_failure", "stop"))
        for idx, item in enumerate(raw["cases"]):
            item = _merge_variables(item, scenario_vars)
            cases.append(
                _from_mapping(
                    item, source=source, order=idx, on_failure_default=default_on_failure,
                    schema_version=version,
                )
            )
        return cases

    # 单用例文件。
    if "request" in raw or "name" in raw:
        cases.append(_from_mapping(raw, source=source, schema_version=version))
        return cases

    raise ValueError(f"unrecognized case file format: {p} (no request/cases/scenario)")


def _read_yaml(p: Path) -> dict[str, Any]:
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"case file {p}: top-level must be a mapping")
    return data


def discover_case_files(base_dir: str | Path, env_var: str = "AH_CASES") -> list[str]:
    """从 ``AH_CASES``（冒号/glob 分隔）或目录下所有 ``.yaml`` 返回用例 YAML 路径。

    对 ``AH_CASES`` 的每一项：若是目录（绝对或相对）则递归收集其下 ``*.yaml``；
    若是文件则直接采用；否则按 glob 通配匹配。这样 conftest 传入的绝对
    项目目录也能被正确解析。
    """
    override = os.getenv(env_var)
    if override:
        out: list[str] = []
        for part in override.split(":"):
            gp = Path(part)
            if gp.is_file():
                out.append(str(gp))
            elif gp.is_dir():
                out.extend(str(p) for p in gp.rglob("*.yaml") if p.is_file())
            else:
                out.extend(str(p) for p in Path(".").glob(part) if p.is_file())
        return sorted(set(out))
    base = Path(base_dir)
    return sorted(str(p) for p in base.rglob("*.yaml") if p.is_file())



