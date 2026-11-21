#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15
# @Filename : knowledge.py
# @Author   : Alan_Hsu
"""失败知识沉淀：resolved / known_issue 检索闭环（对应文章2 步骤4）。

把失败簇群走到终态后的经验沉淀成可复用知识：

- ``resolved``：问题被修复并验证通过，记录根因和解法。
- ``known_issue``：确认是已知缺陷或外部不可控问题，生成规避方案。

流程：
1. 知识先以「草稿」保存（status=draft），经人工确认后发布（status=published）。
2. 处理新失败时按指纹检索已发布知识：
   - 命中 known_issue → 直接改变问题分类和动作（见 :func:`apply`）。
   - 命中 resolved → 根因/解法作为提示词上下文提供给分类/根因分析。
3. 记录每次命中后的使用结果（后续是否再次出现/自愈是否成功），
   反馈并评估知识质量（hit_count / effective_count / score）。

纯本地 JSON 落盘（hermetic）：``failure_knowledge.json``。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from ah.failure.cluster import normalize_error_sig
from ah.utils.logging import get_logger

log = get_logger("failure_knowledge")

DRAFT = "draft"
PUBLISHED = "published"

#: 知识类型
TYPE_RESOLVED = "resolved"
TYPE_KNOWN_ISSUE = "known_issue"


def _search_signature(
    *,
    method: str = "",
    path: str = "",
    failure_class: str = "",
    error: Any = None,
) -> str:
    """生成检索签名（与簇指纹同构：method|path|class|error_sig）。"""
    return f"{method}|{path}|{failure_class}|{normalize_error_sig(error)}"


class KnowledgeStore:
    """失败知识库（JSON 落盘 + 指纹检索 + 草稿/发布 + 使用反馈）。

    Args:
        path: 落盘路径（``report_dir/failure_knowledge.json``）。None → 纯内存。
    """

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else None
        self._items: dict[str, dict[str, Any]] = {}
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._items = data
        except Exception as exc:  # noqa: BLE001
            log.warning("failure_knowledge 读取失败（%s），以空库开始", exc)
            self._items = {}

    def _flush(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", encoding="utf-8") as fh:
                json.dump(self._items, fh, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            log.warning("failure_knowledge 写盘失败: %s", exc)

    # ── 写入 ─────────────────────────────────────────────────────────────
    def add_draft(
        self,
        *,
        method: str = "",
        path: str = "",
        failure_class: str = "",
        error: Any = None,
        kind: str = TYPE_RESOLVED,
        root_cause: str = "",
        solution: str = "",
        workaround: str = "",
    ) -> str:
        """新增一条草稿知识；返回知识 id（签名）。"""
        key = _search_signature(
            method=method, path=path, failure_class=failure_class, error=error
        )
        self._items[key] = {
            "id": key,
            "method": method,
            "path": path,
            "failure_class": failure_class,
            "error_sig": normalize_error_sig(error),
            "kind": kind,
            "status": DRAFT,
            "root_cause": root_cause,
            "solution": solution,
            "workaround": workaround,
            "created_at": time.time(),
            "published_at": None,
            "hit_count": 0,
            "effective_count": 0,
            "score": 0.0,
        }
        self._flush()
        return key

    def publish(self, key: str) -> bool:
        """人工确认发布（草稿 → published）。"""
        item = self._items.get(key)
        if not item:
            return False
        item["status"] = PUBLISHED
        item["published_at"] = time.time()
        self._flush()
        return True

    def drafts(self) -> list[dict[str, Any]]:
        return [dict(i) for i in self._items.values() if i.get("status") == DRAFT]

    def published(self) -> list[dict[str, Any]]:
        return [dict(i) for i in self._items.values() if i.get("status") == PUBLISHED]

    # ── 检索 / 应用 ──────────────────────────────────────────────────────
    def search(
        self,
        *,
        method: str = "",
        path: str = "",
        failure_class: str = "",
        error: Any = None,
        only_published: bool = True,
    ) -> Optional[dict[str, Any]]:
        """按签名精确/宽松检索知识；命中 published（默认）才返回。"""
        exact = _search_signature(
            method=method, path=path, failure_class=failure_class, error=error
        )
        item = self._items.get(exact)
        if item and (not only_published or item.get("status") == PUBLISHED):
            return dict(item)
        # 宽松：忽略 error_sig（同类失败多次根因一样）
        for key, it in self._items.items():
            if only_published and it.get("status") != PUBLISHED:
                continue
            if it.get("method") == method and it.get("path") == path \
                    and it.get("failure_class") == failure_class:
                return dict(it)
        return None

    def apply(
        self,
        *,
        method: str = "",
        path: str = "",
        failure_class: str = "",
        error: Any = None,
        default_category: str = "unknown",
        default_action: str = "observe",
        severe: bool = False,
    ) -> dict[str, Any]:
        """检索命中后把知识应用到当前失败：改变分类与动作。

        返回 ``{"hit": bool, "kind", "category", "action", "root_cause",
        "workaround", "knowledge_id", "evidence": [...], "score"}``。

        命中 known_issue → 分类强制为 known_issue，动作按严重性
        （observe/block）；命中 resolved → 沿用根因/解法但分类动作按默认逻辑。
        记录 hit_count + evidence。绝不抛异常。
        """
        hit = self.search(
            method=method, path=path, failure_class=failure_class, error=error
        )
        if not hit:
            return {
                "hit": False,
                "category": default_category,
                "action": default_action,
                "evidence": [],
            }
        kind = hit.get("kind")
        if kind == TYPE_KNOWN_ISSUE:
            category = "known_issue"
            action = "block" if severe else "observe"
            evidence = [hit.get("workaround") or hit.get("root_cause") or ""]
        else:  # resolved
            category = default_category
            action = default_action
            evidence = [
                hit.get("root_cause") or "",
                hit.get("solution") or "",
            ]
        # 记录使用（命中 +1）
        item = self._items.get(hit["id"])
        if item is not None:
            item["hit_count"] = item.get("hit_count", 0) + 1
            self._flush()
        return {
            "hit": True,
            "kind": kind,
            "category": category,
            "action": action,
            "root_cause": hit.get("root_cause", ""),
            "workaround": hit.get("workaround", ""),
            "knowledge_id": hit["id"],
            "evidence": [e for e in evidence if e],
            "score": hit.get("score", 0.0),
        }

    def record_effective(self, key: str, effective: bool) -> None:
        """反馈一次使用结果：后续是否证明有效（影响质量分数）。"""
        item = self._items.get(key)
        if not item:
            return
        if effective:
            item["effective_count"] = item.get("effective_count", 0) + 1
        hits = item.get("hit_count", 0)
        eff = item.get("effective_count", 0)
        item["score"] = (eff / hits) if hits else 0.0
        self._flush()


def load_knowledge(path: Optional[str | Path] = None) -> KnowledgeStore:
    """便捷构造（供报告/调度复用）。"""
    return KnowledgeStore(path)
