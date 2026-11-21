#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15
# @Filename : test_failure_cluster.py
# @Author   : Alan_Hsu
"""失败簇群：指纹聚合 + 状态机 hermetic 单测。

对应《别让自愈把红灯洗绿》步骤3：失败簇群指纹 + 状态机落盘。
覆盖：
- 指纹聚合：同接口+同类+同错误特征聚为一个簇。
- 状态流转：active → flaky_watch → quarantined → recovered → rearm；
  recovered 后再失败回到 flaky_watch。
- resolved 终态不被失败清除（只记 recurrences）。
- JSON 落盘 / 读取 / 损坏容错。
- quarantined_keys / recovered_or_resolved_keys / states 查询。

全部纯内存（path=None）或 tmp_path 落盘，零网络、零真模型。
"""

from __future__ import annotations

from ah.failure.cluster import (
    ACTIVE,
    FLAKY_WATCH,
    QUARANTINED,
    RECOVERED,
    RESOLVED,
    ClusterStore,
)


def _record(store, *, passed=False, failure_class="environment", path="/api/users", n=1):
    """连续记录 n 次同一失败/通过，返回最后一次的簇状态。"""
    out = None
    for _ in range(n):
        out = store.record(
            method="GET", path=path, failure_class=failure_class,
            error="connection refused", passed=passed, case_name="u",
        )
    return out


def test_fingerprint_aggregates_same_failure_one_cluster():
    """同接口 + 同类 + 同错误特征 → 聚合为单个簇（同一 key）。"""
    s = ClusterStore(path=None)
    r1 = s.record(method="GET", path="/api/users", failure_class="environment",
                  error="connection refused", passed=False, case_name="u")
    r2 = s.record(method="GET", path="/api/users", failure_class="environment",
                  error="connection refused", passed=False, case_name="u")
    assert r1["key"] == r2["key"]
    assert len(s._clusters) == 1  # noqa: SLF001 — 断言聚合而非分裂


def test_state_flow_active_to_quarantined():
    """连续失败：active(3) → flaky_watch → (观察期再失败) → quarantined。"""
    s = ClusterStore(path=None)
    # 3 次失败 → flaky_watch（默认阈值 3）
    r = _record(s, n=3)
    assert r["state"] == FLAKY_WATCH
    # 观察期（watch_fail_threshold=2）内再失败 2 次 → quarantined
    r = _record(s, n=2)
    assert r["state"] == QUARANTINED


def test_quarantined_recovers_after_consecutive_passes():
    """quarantined 簇连续通过 recover_passes 次 → recovered。"""
    s = ClusterStore(path=None)
    _record(s, n=5)  # 到 quarantined
    assert s.quarantined_keys()
    r = _record(s, passed=True, n=2)  # 连续通过 2 次 → recovered
    assert r["state"] == RECOVERED
    assert s.quarantined_keys() == []


def test_recovered_re_fail_reenters_flaky_watch():
    """recovered 后再失败 → 回到 flaky_watch（重新观察）。"""
    s = ClusterStore(path=None)
    _record(s, n=5)          # quarantined
    _record(s, passed=True, n=2)  # recovered
    r = _record(s, n=1)      # 再失败
    assert r["state"] == FLAKY_WATCH


def test_resolved_is_terminal_not_cleared():
    """人工 resolved 终态：新失败不清除状态，只累计 recurrences。"""
    s = ClusterStore(path=None)
    key = _record(s, n=5)["key"]
    assert s.mark_resolved(key)
    assert s.get(key)["state"] == RESOLVED
    r = _record(s, n=2)  # resolved 后再失败
    assert r["state"] == RESOLVED
    assert r["resolved_recurrences"] >= 2


def test_rearm_pulls_back_to_active():
    """rearm 把 resolved/quarantined 簇拉回 active 并清零计数。"""
    s = ClusterStore(path=None)
    key = _record(s, n=5)["key"]
    assert s.rearm(key)
    cluster = s.get(key)
    assert cluster["state"] == ACTIVE
    assert cluster["fail_count"] == 0
    assert cluster["pass_count"] == 0


def test_states_and_key_queries():
    """states() 统计与 quarantined/recovered_or_resolved 查询。"""
    s = ClusterStore(path=None)
    _record(s, n=5, path="/a")                       # quarantined
    k2 = _record(s, n=3, path="/b")["key"]           # flaky_watch
    s.mark_resolved(k2)
    counts = s.states()
    assert counts[QUARANTINED] == 1
    assert len(s.quarantined_keys()) == 1
    assert k2 in s.recovered_or_resolved_keys()


def test_corrupt_json_tolerated(tmp_path):
    """损坏 JSON 不阻断，回到空簇。"""
    p = tmp_path / "failure_clusters.json"
    p.write_text("{ not valid json", encoding="utf-8")
    s = ClusterStore(path=p)
    assert s._clusters == {}  # noqa: SLF001 — 损坏容错回到空簇
