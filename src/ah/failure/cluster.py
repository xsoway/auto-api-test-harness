#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15
# @Filename : cluster.py
# @Author   : Alan_Hsu
"""失败簇群：指纹聚合 + 状态机（对应文章2 步骤3）。

把"同一条用例、同一种现象的反复失败"归类到同一个簇群，用指纹聚合具有共性
的问题，避免不同接口上同样超时被当作孤立个案重复处理。

状态机：
    active → flaky_watch → quarantined → recovered → active
                                  ↘ resolved（人工确认终态）
- 首次失败进入 ``active``，簇开始计数
- 连续失败达到阈值进入 ``flaky_watch``
- 观察后仍未恢复进入 ``quarantined``（跳过相关脚本执行）
- 连续通过进入 ``recovered``；常规执行再通过回到 ``active``
- 人工确认已解决进入 ``resolved``

纯本地 JSON 落盘（hermetic）：``failure_clusters.json``。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from ah.utils.logging import get_logger

log = get_logger("failure_cluster")

# 状态
ACTIVE = "active"
FLAKY_WATCH = "flaky_watch"
QUARANTINED = "quarantined"
RECOVERED = "recovered"
RESOLVED = "resolved"

#: 默认状态流转阈值。
DEFAULT_FLAKY_THRESHOLD = 3      # 连续失败达到该次数 → flaky_watch
DEFAULT_WATCH_FAIL_THRESHOLD = 2 # flaky_watch 期间再失败 N 次 → quarantined
DEFAULT_RECOVER_PASSES = 2       # 连续通过 N 次 → recovered


def _fingerprint(*, method: str, path: str, failure_class: str, error_sig: str) -> str:
    """生成失败指纹：同接口 + 同类失败 + 同错误特征聚合为一个簇。"""
    return f"{method}|{path}|{failure_class}|{error_sig}"


def normalize_error_sig(error: Any) -> str:
    """从错误对象/文本提炼稳定的错误特征签名（小写 + 空格归一）。"""
    text = error if isinstance(error, str) else str(error)
    return " ".join(text.strip().lower().split())[:120]


class ClusterStore:
    """失败簇群的 JSON 落盘存储 + 状态机。

    Args:
        path: 落盘路径（``report_dir/failure_clusters.json``）。为 None 时纯内存
            （不落盘，测试/临时用）。
    """

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else None
        self._clusters: dict[str, dict[str, Any]] = {}
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._clusters = data
        except Exception as exc:  # noqa: BLE001 — 损坏/空文件不阻断
            log.warning("failure_clusters 读取失败（%s），以空簇开始", exc)
            self._clusters = {}

    def _flush(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", encoding="utf-8") as fh:
                json.dump(self._clusters, fh, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001 — 写失败仅告警
            log.warning("failure_clusters 写盘失败: %s", exc)

    # ── 查询 ─────────────────────────────────────────────────────────────
    def get(self, key: str) -> Optional[dict[str, Any]]:
        return self._clusters.get(key)

    def states(self) -> dict[str, int]:
        """按状态统计簇数量（供报告/遥测）。"""
        out: dict[str, int] = {}
        for c in self._clusters.values():
            state = c.get("state", ACTIVE)
            out[state] = out.get(state, 0) + 1
        return out

    def quarantined_keys(self) -> list[str]:
        """处于 quarantined 的簇指纹（供调度跳过执行）。"""
        return [
            k for k, c in self._clusters.items() if c.get("state") == QUARANTINED
        ]

    def recovered_or_resolved_keys(self) -> list[str]:
        """recovered / resolved 的簇指纹（供知识沉淀回调）。"""
        return [
            k for k, c in self._clusters.items()
            if c.get("state") in (RECOVERED, RESOLVED)
        ]

    # ── 写入 / 状态流转 ──────────────────────────────────────────────────
    def record(
        self,
        *,
        method: str = "",
        path: str = "",
        failure_class: str = "",
        error: Any = None,
        passed: bool,
        case_name: str = "",
        flaky_threshold: int = DEFAULT_FLAKY_THRESHOLD,
        watch_fail_threshold: int = DEFAULT_WATCH_FAIL_THRESHOLD,
        recover_passes: int = DEFAULT_RECOVER_PASSES,
        store: Optional[str] = None,
    ) -> dict[str, Any]:
        """记录一次用例结果到对应簇，触发状态机流转。

        返回该簇更新后的状态字典。``passed=True`` 计入"连续通过"，
        ``passed=False`` 计入"连续失败"。
        """
        sig = normalize_error_sig(error) if not passed else ""
        # 失败时用失败特征；通过时用空错误签名，但若已有同 (method,path,failure_class)
        # 的观察/隔离簇，则把本次通过记到该簇（验证"同簇恢复能力"，驱动状态回移）。
        key = _fingerprint(
            method=method, path=path, failure_class=failure_class, error_sig=sig
        )
        if passed and sig == "":
            # 宽松定位：优先观察/隔离中的簇（flaky_watch/quarantined/recovered），
            # 让连续通过能真正把隔离簇回移到 recovered。
            for k, c in self._clusters.items():
                if k == key:
                    break
                if c.get("method") == method and c.get("path") == path \
                        and c.get("failure_class") == failure_class:
                    if c.get("state") in (ACTIVE, FLAKY_WATCH, QUARANTINED, RECOVERED):
                        key = k
                        break
        cluster = self._clusters.setdefault(
            key,
            {
                "key": key,
                "method": method,
                "path": path,
                "failure_class": failure_class,
                "state": ACTIVE,
                "fail_count": 0,
                "pass_count": 0,
                "first_seen": time.time(),
                "last_seen": time.time(),
                "cases": [],
            },
        )
        cluster["last_seen"] = time.time()
        if cluster["state"] == RESOLVED:
            # resolved 是人工终态：新失败不清除，但记录事实供复核。
            cluster["resolved_recurrences"] = cluster.get("resolved_recurrences", 0) + (
                0 if passed else 1
            )
            self._flush()
            return dict(cluster)

        if passed:
            cluster["pass_count"] = cluster.get("pass_count", 0) + 1
            cluster["fail_count"] = 0
            if cluster["state"] in (FLAKY_WATCH, QUARANTINED):
                if cluster["pass_count"] >= recover_passes:
                    cluster["state"] = RECOVERED
                    log.info("簇 %s 连续通过 %d 次 → recovered", key, cluster["pass_count"])
        else:
            cluster["fail_count"] = cluster.get("fail_count", 0) + 1
            cluster["pass_count"] = 0
            state = cluster["state"]
            if state == ACTIVE and cluster["fail_count"] >= flaky_threshold:
                cluster["state"] = FLAKY_WATCH
                log.info("簇 %s 连续失败 %d 次 → flaky_watch", key, cluster["fail_count"])
            elif state == FLAKY_WATCH:
                if cluster["fail_count"] >= watch_fail_threshold:
                    cluster["state"] = QUARANTINED
                    log.info("簇 %s 观察期仍失败 → quarantined", key)
            # recovered 后再次失败 → 回到 flaky_watch（重新观察）
            elif state == RECOVERED:
                cluster["state"] = FLAKY_WATCH

        if case_name and case_name not in cluster["cases"]:
            cluster["cases"].append(case_name)
            cluster["cases"] = cluster["cases"][-50:]  # 防无限增长
        self._flush()
        return dict(cluster)

    def mark_resolved(self, key: str) -> bool:
        """人工确认已解决 → resolved 终态。"""
        cluster = self._clusters.get(key)
        if not cluster:
            return False
        cluster["state"] = RESOLVED
        cluster["resolved_at"] = time.time()
        self._flush()
        return True

    def rearm(self, key: str) -> bool:
        """把 resolved/quarantined 簇重新拉回 active（人工复核后启用）。"""
        cluster = self._clusters.get(key)
        if not cluster:
            return False
        cluster["state"] = ACTIVE
        cluster["fail_count"] = 0
        cluster["pass_count"] = 0
        self._flush()
        return True


def load_clusters(path: Optional[str | Path] = None) -> ClusterStore:
    """便捷构造（供调度/报告复用）。"""
    return ClusterStore(path)
