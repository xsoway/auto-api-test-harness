"""跨 run 的结果历史库（JSONL 追加式）。

:class:`HistoryStore` 把每次运行产生的 :class:`ResultRecord` 序列化为一行
JSON 追加到 ``artifacts/history.jsonl``（目录可经 ``settings.report_dir`` 或
构造参数指定）。读写是纯本地文件 IO，绝不触碰网络，因此 hermetic 套件
可在 `LLMOFF=1` 下正常累积历史。

每条记录保留 :class:`ResultRecord` 的全部字段，并额外带上捕获时间戳与
从失败信息/LLM notes 归一化的 ``failure_class``（risk 层消费它做分类统计）。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Optional

from ah.reporting.writer import ResultRecord

#: 失败类别：从 LLM triage 或错误字符串归一化得到。
FAILURE_UNKNOWN = "unknown"


def _normalize_failure_class(rec: ResultRecord) -> str:
    """从一次记录的失败信息推导一个稳定的失败类别字符串。

    优先使用 LLM triage 给出的 ``category``（若有）；否则根据错误文本
    关键词回退到启发式分类。用于 risk 层的分类维度统计。
    """
    triage = rec.llm_notes.get("triage")
    if isinstance(triage, dict):
        cat = triage.get("category")
        if isinstance(cat, str) and cat.strip():
            return cat.strip()
    msg = (rec.failure_msg or "").lower()
    if "schema" in msg or "jsonpath" in msg:
        return "schema"
    if "contains" in msg or "expected" in msg:
        return "assertion"
    if "status_code" in msg or "status" in msg:
        return "status"
    if "extract" in msg or "extraction" in msg:
        return "extraction"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    return FAILURE_UNKNOWN


def _interface_key(rec: ResultRecord) -> str:
    """归一化接口主键：优先 request 里的 path，否则用用例名。"""
    req = rec.request or {}
    path = req.get("path")
    if isinstance(path, str) and path.strip():
        return path.strip()
    return str(rec.name)


class HistoryStore:
    """JSONL 累积结果历史库。

    ``append`` 写一条；``load`` 读回全部记录。存储路径默认落在
    ``settings.report_dir`` 下的 ``history.jsonl``。写入是追加模式，
    线程安全由调用方（pytest 会话收集器单线程 flush）保证。
    """

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._path = Path(path) if path is not None else None

    @property
    def path(self) -> Path:
        if self._path is None:
            from ah.core.config import settings

            base = Path(settings.report_dir)
            base.mkdir(parents=True, exist_ok=True)
            return base / "history.jsonl"
        return self._path

    def append_all(self, records: Iterable[ResultRecord], *, now: Optional[float] = None) -> int:
        """把一批记录追加到历史库；返回写入条数。"""
        ts = now if now is not None else time.time()
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with p.open("a", encoding="utf-8") as fh:
            for rec in records:
                row = asdict(rec)
                row["ts"] = ts
                row["interface"] = _interface_key(rec)
                row["failure_class"] = (
                    _normalize_failure_class(rec) if rec.result != "PASS" else None
                )
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
        return written

    def load(self) -> list[dict[str, Any]]:
        """读回全部历史记录（每行一条 dict），按写入顺序排列。"""
        p = self.path
        if not p.exists():
            return []
        rows: list[dict[str, Any]] = []
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows


def load_history(path: Optional[str | Path] = None) -> list[dict[str, Any]]:
    """便捷函数：返回给定存储的全部历史记录。"""
    return HistoryStore(path).load()
