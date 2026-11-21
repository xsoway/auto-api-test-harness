#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/16 17:50
# @Filename : conftest.py
# @Author   : Alan_Hsu
"""框架自测套件（tests/）的全局隔离夹具。

背景：local_obs（本地过程链路 Tracer 双写 SQLite）默认开启（config 里
local_obs_enabled 缺省 True）。为保持「框架自测零副作用」的隔离铁律，本文件用
autouse session 夹具把模块级 settings 的 local_obs_enabled 钉为 False——否则任何
经 get_tracer() 构造真实 Tracer 的测试都会写到项目根的 local-obs.sqlite3。

需要真实本地链路的测试不走 get_tracer()：显式构造 `ObserveStore(tmp_path/...)` +
`Tracer(None, trace_id, name, store=store)`（见 test_observe_live_tree.py）。
用户侧实际运行（projects/<业务> 的 pytest、或 ah run CLI）不受本文件约束，仍默认
写过程链路。
"""

from __future__ import annotations

import pytest

import ah.core.trace as trace_mod


@pytest.fixture(autouse=True)
def _pin_local_obs_off():
    """把 settings.local_obs_enabled 钉为 False，保证框架自测不写项目 SQLite。

    session 生效（session 级字段全局一致）；结束后还原，避免污染同进程后续测试。
    注意：monkeypatch 对该进程级单例只保留到本夹具结束，跨测试不持久。
    """
    original = trace_mod.settings.local_obs_enabled
    trace_mod.settings.local_obs_enabled = False
    yield
    trace_mod.settings.local_obs_enabled = original
