"""配置：settings 数据类 + YAML/环境变量覆盖。

``load_settings()`` 读取 ``config/config.yaml``（可回退到环境变量 ``AH_CONFIG``），
然后合并环境变量覆盖（``AH_BASE_URL``、``AH_TOKEN_STORE`` 等），返回进程级
``singleton``。Settings 是纯数据，可在任何模块中安全导入而不会触发副作用。

从 YAML 加载的值支持 ``${VAR}`` / ``${VAR:-default}`` 环境变量插值
（移植自 Eval-Anything 的 ``ConfigLoader._resolve_env_value``）：
没有默认值的未知变量会保留其占位符，从而使下游查找显式失败，
而不是静默替换为空字符串。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def _project_root() -> Path:
    """仓库根目录：本文件位于 ``src/ah/core/config.py``，上探到 ``src`` 的上级。"""
    return Path(__file__).resolve().parents[3]


def _default_config_path() -> str:
    """项目根 ``config/config.yaml`` 的绝对路径（任意 cwd 都能定位到仓库配置）。"""
    return str(_project_root() / "config" / "config.yaml")


# 配置路径：``AH_CONFIG`` 显式覆盖优先（保持相对 cwd 的原语义），
# 否则回退到按 ``__file__`` 定位的项目根 ``config/config.yaml``，
# 使从任意目录运行（如 projects/demo）都能读到仓库配置。
CONFIG_PATH = os.getenv("AH_CONFIG", _default_config_path())

# 当 YAML 缺失或键缺失时使用的默认值。
_DEFAULT_BASE_URL = "http://127.0.0.1:8000"
_DEFAULT_TOKEN_STORE = "config/tokens.json"
_DEFAULT_LOG_DIR = "artifacts/logs"
_DEFAULT_REPORT_DIR = "artifacts/reports"
_DEFAULT_TRACE_ENABLED = False
_OPENROUTER_DEFAULT = "https://openrouter.ai/api/v1/"

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _resolve_env_value(raw: Any) -> Any:
    """解析字符串中的 ``${VAR}`` 与 ``${VAR:-default}`` 引用。

    - 非字符串值（数字 / 布尔 / 列表）原样透传
    - ``${VAR}`` 读取环境变量；未设置且无默认值时保留占位符
    - ``${VAR:-default}`` 在未设置时回退到 ``default``
    一个字符串中的多个引用各自独立解析。
    """
    if not isinstance(raw, str) or "${" not in raw:
        return raw

    def _sub(m: "re.Match[str]") -> str:
        var = m.group(1)
        default = m.group(2)
        val = os.getenv(var)
        if val is not None and val != "":
            return val
        if default is not None:
            return default
        # 未设置且无默认值：保留占位符，使调用方的查找显式失败。
        return m.group(0)

    return _ENV_RE.sub(_sub, raw)



@dataclass
class DomainSettings:
    """多服务环境下单个域（服务）的连接配置。"""

    base_url: str = ""
    auth: str = ""  # 令牌名；空则不附加 Authorization


@dataclass
class LLMSettings:
    """LLM 辅助客户端的路由（轻量与强任务模型）。"""

    provider_base_url: str = ""
    api_key: str = ""
    model_cheap: str = ""
    model_strong: str = ""
    timeout: float = 120.0


@dataclass
class Settings:
    base_url: str = _DEFAULT_BASE_URL
    token_store_path: str = _DEFAULT_TOKEN_STORE
    log_dir: str = _DEFAULT_LOG_DIR
    report_dir: str = _DEFAULT_REPORT_DIR
    trace_enabled: bool = _DEFAULT_TRACE_ENABLED
    trace_host: str = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    trace_public_key: str = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    trace_secret_key: str = os.getenv("LANGFUSE_SECRET_KEY", "")
    trace_name: str = os.getenv("AH_TRACE_NAME", "auto-api-test-harness")
    # 本地过程链路（local-obs SQLite）独立开关；默认开启（env 未设→True），
    # 使任意 run（pytest / ah run / evolve）都实时写过程链路到 local-obs.sqlite3。
    # 显式 AH_LOCAL_OBS=0/false/off/no 可关闭；AH_LOCAL_OBS=1/true/yes/on 保持开启。
    # 再通过 AH_LOCAL_OBS_DB 指定库路径。写入失败仅告警，绝不中断运行。
    # 注意：字段默认值在类体创建时求值，故此处内联 env 解析，不引用下方 _env_bool。
    local_obs_enabled: bool = (
        os.getenv("AH_LOCAL_OBS", "").strip().lower()
        not in {"0", "false", "off", "no"}
    )
    local_obs_db: str = os.getenv("AH_LOCAL_OBS_DB", "local-obs.sqlite3")
    domains: dict[str, DomainSettings] = field(default_factory=dict)
    llm: LLMSettings = field(default_factory=LLMSettings)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_yaml(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def _s(value: Any, default: str) -> str:
    """将解析（可能经环境插值）后的 YAML 值强制转为 str，带兜底默认值。"""
    resolved = _resolve_env_value(value)
    return str(resolved) if resolved is not None else default


@lru_cache(maxsize=1)
def load_settings(config_path: str | None = None, profile: str | None = None) -> Settings:
    """加载并缓存 settings（每个进程仅做一次环境变量探测）。

    配置文件路径：显式参数优先，其次 ``AH_CONFIG``，最后 ``config/config.yaml``。
    多环境 profile：显式 ``profile`` 参数优先，其次 ``AH_PROFILE`` 环境变量；
    命中 ``profiles:<name>`` 段时，用其 ``base_url`` 覆盖顶部，并按 key 合并覆盖
    ``domains``（只覆盖列出项的 base_url/auth，其余保留）。
    """
    path = config_path or CONFIG_PATH
    raw = _load_yaml(path)
    top = raw.get("settings", raw) if isinstance(raw, dict) else {}

    # 解析活动 profile：显式参数 > AH_PROFILE；未知名 → None（兜底顶部）。
    active = profile or os.getenv("AH_PROFILE")
    profile_cfg: dict = {}
    if isinstance(raw, dict) and active:
        profiles_raw = raw.get("profiles", {})
        if isinstance(profiles_raw, dict) and isinstance(
            profiles_raw.get(active), dict
        ):
            profile_cfg = profiles_raw[active]
            # 仅覆盖 profile 显式列出的键；base_url 未列则保留顶部。
            if "base_url" in profile_cfg:
                top = {**top, "base_url": profile_cfg["base_url"]}
            if "domains" in profile_cfg and isinstance(profile_cfg["domains"], dict):
                base_domains = dict(top.get("domains", {}))
                merged = {**base_domains, **profile_cfg["domains"]}
                top = {**top, "domains": merged}

    llm_raw = raw.get("llm", {}) if isinstance(raw, dict) else {}
    if not isinstance(llm_raw, dict):
        llm_raw = {}

    trace_enabled = _env_bool("AH_TRACE_ENABLED", _DEFAULT_TRACE_ENABLED)
    if "trace_enabled" in top:
        trace_enabled = _env_bool("AH_TRACE_ENABLED", bool(top["trace_enabled"]))

    # 多服务域：{name: {base_url, auth}} -> {name: DomainSettings}。
    domains_raw = top.get("domains", {}) if isinstance(top, dict) else {}
    domains: dict[str, DomainSettings] = {}
    if isinstance(domains_raw, dict):
        for domain_name, cfg in domains_raw.items():
            if not isinstance(cfg, dict):
                continue
            domains[str(domain_name)] = DomainSettings(
                base_url=_s(cfg.get("base_url"), ""),
                auth=_s(cfg.get("auth"), ""),
            )

    s = Settings(
        domains=domains,
        base_url=os.getenv("AH_BASE_URL", _s(top.get("base_url"), _DEFAULT_BASE_URL)),
        token_store_path=os.getenv(
            "AH_TOKEN_STORE", _s(top.get("token_store_path"), _DEFAULT_TOKEN_STORE)
        ),
        log_dir=os.getenv("AH_LOG_DIR", _s(top.get("log_dir"), _DEFAULT_LOG_DIR)),
        report_dir=os.getenv(
            "AH_REPORT_DIR", _s(top.get("report_dir"), _DEFAULT_REPORT_DIR)
        ),
        trace_enabled=trace_enabled,
        # 本地过程链路开关：环境变量优先，其次 YAML 顶部的 local_obs_enabled/db；
        # 两者都未设置时默认开启（True），使任意 run 默认落到 local-obs.sqlite3。
        local_obs_enabled=_env_bool(
            "AH_LOCAL_OBS", bool(top.get("local_obs_enabled", True))
        ),
        local_obs_db=os.getenv(
            "AH_LOCAL_OBS_DB", _s(top.get("local_obs_db"), "local-obs.sqlite3")
        ),
        llm=LLMSettings(
            provider_base_url=os.getenv(
                "OPENROUTER_BASE_URL",
                _s(llm_raw.get("provider_base_url"), _OPENROUTER_DEFAULT),
            ),
            api_key=os.getenv("OPENROUTER_API_KEY", _s(llm_raw.get("api_key"), "")),
            model_cheap=os.getenv(
                "AH_LLM_MODEL_CHEAP", _s(llm_raw.get("model_cheap"), "openai/gpt-4o-mini")
            ),
            model_strong=os.getenv(
                "AH_LLM_MODEL_STRONG", _s(llm_raw.get("model_strong"), "openai/gpt-4o")
            ),
            timeout=float(_resolve_env_value(llm_raw.get("timeout", 120.0)) or 120.0),
        ),
    )
    return s


def activate_profile(name: str | None) -> None:
    """原位更新模块级 ``settings`` 到指定 profile（供 CLI ``--profile`` 运行时切换）。

    profile 只覆盖 ``base_url`` 与 ``domains``（多环境切换的核心），其余字段沿用
    顶部配置。``name=None`` 表示清除 profile、还原顶部配置。此函数直接改写全局
    ``settings`` 单例，使下游惰性读取（客户端构造/报告）即时生效。
    """
    fresh = load_settings(profile=name)
    settings.base_url = fresh.base_url
    settings.domains.clear()
    settings.domains.update(fresh.domains)


# 便捷访问器，对应计划中 ``settings`` 单例的用法。
settings = load_settings()
