#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/16 17:30
# @Filename : test_observe_live_tree.py
# @Author   : Alan_Hsu
"""本地过程链路（Tracer 双写 local-obs）分层树单测。

验证 run→case→step→HTTP/LLM 父子层级实时写入 SQLite，以及容器跨度 finish
弹栈恢复 parent、update_observation 补齐 latency/status/tokens。全部 hermetic：
用临时 SQLite 文件，零网络。
"""
from __future__ import annotations

import sqlite3

from ah.core.trace import Tracer
from ah.observe.store import ObserveStore


def _tree(db: ObserveStore) -> list[tuple[str, str, str, str, float]]:
    """返回 (name,parent_id,type,status,latency_ms) 按 start_ms 排序。

    用独立 sqlite3 连接读取，避免关闭 store 内部线程本地连接影响后续写入。
    """
    conn = sqlite3.connect(db.db_path)
    try:
        cur = conn.execute(
            "SELECT name,parent_id,type,status,latency_ms FROM observations "
            "ORDER BY start_ms"
        )
        return [tuple(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _ids(db: ObserveStore) -> dict[str, str]:
    """(name→id) 映射，同样用独立只读连接。"""
    conn = sqlite3.connect(db.db_path)
    try:
        return {
            r[1]: r[0]
            for r in conn.execute("SELECT id,name FROM observations")
        }
    finally:
        conn.close()

def test_case_step_http_llm_hierarchy(tmp_path):
    """分层父节点：case→step→HTTP/LLM 形成树，parent_id 正确。"""
    store = ObserveStore(tmp_path / "local-obs.sqlite3")
    # langfuse=None → 纯本地双写，不触网。
    t = Tracer(None, "a1b2c3d4e5f60718293a4b5c6d7e8f90", "run-x", store=store)

    case = t.start_case("test_login")
    # 内部 step 容器
    step = t.start_step("attempt")
    http = t.start_http("POST", "/api/login")
    http.finish(status=200, latency_ms=12.5)
    llm = t.start_llm("advice", "gpt-4o")
    llm.finish(
        prompt="p", response="r", latency_ms=40,
        usage={"prompt_tokens": 5, "completion_tokens": 3},
    )
    step.finish(latency_ms=60)
    case.finish(latency_ms=100)

    rows = _tree(store)
    by_name = {r[0]: r for r in rows}
    case_row = by_name["case:test_login"]
    step_row = by_name["step:attempt"]
    http_row = by_name["HTTP POST /api/login"]
    llm_row = by_name["LLM advice"]

    # case 是根（parent 为空）；step 挂到 case；HTTP/LLM 挂到 step。
    assert case_row[1] == ""
    ids = _ids(store)
    assert step_row[1] == ids["case:test_login"]
    assert http_row[1] == ids["step:attempt"]
    assert llm_row[1] == ids["step:attempt"]

    # 状态与延迟已由 finish 补齐。
    assert http_row[3] == "ok" and http_row[4] == 12.5
    assert llm_row[2] == "GENERATION" and llm_row[4] == 40
    assert llm_row[0]
    assert case_row[4] == 100


def test_container_finish_pops_parent_no_leak(tmp_path):
    """容器跨度 finish 弹栈：结束后新叶子不再受残留 parent 影响。"""
    store = ObserveStore(tmp_path / "local-obs.sqlite3")
    t = Tracer(None, "a1b2c3d4e5f60718293a4b5c6d7e8f91", "run-y", store=store)

    case = t.start_case("c1")
    step = t.start_step("s1")
    http_inner = t.start_http("GET", "/inner")
    http_inner.finish(status=200)
    step.finish(latency_ms=10)
    case.finish(latency_ms=20)

    # 结束后再开一个叶子，其父应回到根（空），而非残留 step/case。
    after = t.start_http("GET", "/after")
    after.finish(status=200)

    conn = sqlite3.connect(store.db_path)
    names = {r[0]: r[1] for r in conn.execute(
        "SELECT name,parent_id FROM observations"
    )}
    conn.close()
    assert names["HTTP GET /inner"] != ""
    # 跨容器结束时 context 已清空，叶子父为空字符串。
    assert names["HTTP GET /after"] == ""
