#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2026-10-09
# @Filename: test_profile.py
# @Author  : Alan_Hsu
"""④ 多环境切换 profile 验证：AH_PROFILE env / 显式 profile 覆盖 base_url、domains。

覆盖：env 与显式 profile 选择、base_url 覆盖、domains 按 key 合并覆盖、
未知 profile 兜底、activate_profile 全局生效。全部 hermetic（临时 YAML）。
"""

from __future__ import annotations

from ah.core import config as cfg


def _yaml(tmp_path, body: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return str(p)


_A = "http://127.0.0.1:8000"  # 顶部默认 base_url
_PROD = "https://api.example.test"
_DEV = "http://127.0.0.1:9000"


def _base_yml() -> str:
    return f"""\
settings:
  base_url: {_A}
  domains:
    orders:
      base_url: https://api-test.example.test
      auth: xmd_token
    inventory:
      base_url: http://inventory.example.test
profiles:
  prod:
    base_url: {_PROD}
    domains:
      orders:
        base_url: https://api-prod.example.test
  dev:
    base_url: {_DEV}
"""


# ───────────────────────── 环境变量选择 ─────────────────────────


def test_env_profile_overrides_base_url(monkeypatch, tmp_path):
    """AH_PROFILE=prod → base_url 被 prod 覆盖。"""
    monkeypatch.setenv("AH_PROFILE", "prod")
    s = cfg.load_settings(_yaml(tmp_path, _base_yml()))
    assert s.base_url == _PROD


def test_env_profile_overrides_domains(monkeypatch, tmp_path):
    """AH_PROFILE=prod → domains 按 key 覆盖（orders 换地址，inventory 保留）。"""
    monkeypatch.setenv("AH_PROFILE", "prod")
    s = cfg.load_settings(_yaml(tmp_path, _base_yml()))
    assert s.domains["orders"].base_url == "https://api-prod.example.test"
    assert s.domains["inventory"].base_url == "http://inventory.example.test"


def test_no_profile_uses_top_level(monkeypatch, tmp_path):
    """未设 AH_PROFILE → 用顶部 base_url 与 domains。"""
    monkeypatch.delenv("AH_PROFILE", raising=False)
    s = cfg.load_settings(_yaml(tmp_path, _base_yml()))
    assert s.base_url == _A
    assert s.domains["orders"].base_url == "https://api-test.example.test"


# ───────────────────────── 显式 profile 参数 ─────────────────────────


def test_explicit_profile_beats_env(monkeypatch, tmp_path):
    """显式 profile 参数优先于 AH_PROFILE env。"""
    monkeypatch.setenv("AH_PROFILE", "prod")
    s = cfg.load_settings(_yaml(tmp_path, _base_yml()), profile="dev")
    assert s.base_url == _DEV


def test_unknown_profile_falls_back(monkeypatch, tmp_path):
    """未知 profile → 兜底顶部配置（不报错）。"""
    monkeypatch.setenv("AH_PROFILE", "nope")
    s = cfg.load_settings(_yaml(tmp_path, _base_yml()))
    assert s.base_url == _A


def test_profile_without_base_url_keeps_top(monkeypatch, tmp_path):
    """profile 未给 base_url → 保留顶部 base_url，仅覆盖列出的 domains。"""
    yml = f"""\
settings:
  base_url: {_A}
  domains:
    orders:
      base_url: https://api-test.example.test
profiles:
  prod:
    domains:
      orders:
        base_url: https://api-prod.example.test
"""
    monkeypatch.setenv("AH_PROFILE", "prod")
    s = cfg.load_settings(_yaml(tmp_path, yml))
    assert s.base_url == _A
    assert s.domains["orders"].base_url == "https://api-prod.example.test"


# ───────────────────────── activate_profile 全局 ─────────────────────────


def test_activate_profile_mutates_global_singleton(monkeypatch, tmp_path):
    """activate_profile 原位更新模块级 settings（供 CLI --profile 运行时切换）。"""
    monkeypatch.delenv("AH_PROFILE", raising=False)
    monkeypatch.setattr(cfg, "CONFIG_PATH", _yaml(tmp_path, _base_yml()))
    cfg.load_settings.cache_clear()
    try:
        cfg.activate_profile("dev")
        assert cfg.settings.base_url == _DEV
        # 还原到顶部配置：activate_profile(None) 表示清除 profile。
        cfg.activate_profile(None)
        assert cfg.settings.base_url == _A
    finally:
        cfg.load_settings.cache_clear()


def test_cli_parser_accepts_profile_flag(tmp_path):
    """CLI ``scheduled-run --profile <name>`` 解析出 profile 并传给执行器。"""
    from ah.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["scheduled-run", str(tmp_path), "--profile", "dev"]
    )
    assert args.profile == "dev"
