#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15 19:20
# @Filename : store.py
# @Author   : Alan_Hsu
"""本地可观测 SQLite 存储（服务层，对齐 Langfuse 数据模型）。

表：
- ``runs``：一次测试运行（≈ Langfuse trace）。
- ``observations``：单个 span / LLM generation（≈ Langfuse span/generation/event）。
- ``tests``：单个用例结果（PASS/FAIL/FLKY，附 llm_notes 失败治理/自愈）。
- ``scores``：打分（severity / gate / flaky 等派生）。

本模块只负责 schema 与读写，不含 HTTP/CLI。所有写操作幂等（upsert 按键替换），
坏行/缺失字段不崩溃（安全转默认值），保证 LLMOFF/hermetic 下零异常。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from ah.utils.logging import get_logger

log = get_logger("observe.store")

#: SQLite 默认数据库文件名（相对工作区/显式路径）。
DEFAULT_DB = "local-obs.sqlite3"

#: 支持的 observation 类型（Langfuse 命名风格）。
OBS_TYPES = ("SPAN", "GENERATION", "EVENT")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    start_time    TEXT NOT NULL DEFAULT '',
    total         INTEGER NOT NULL DEFAULT 0,
    passed        INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0,
    flaky         INTEGER NOT NULL DEFAULT 0,
    pass_rate     REAL NOT NULL DEFAULT 0,
    result_dist   TEXT NOT NULL DEFAULT '{}',
    status_dist   TEXT NOT NULL DEFAULT '{}',
    avg_duration_ms   REAL NOT NULL DEFAULT 0,
    p95_duration_ms   REAL NOT NULL DEFAULT 0,
    total_duration_ms REAL NOT NULL DEFAULT 0,
    per_case_dur_ms   TEXT NOT NULL DEFAULT '{}',
    total_cost     REAL NOT NULL DEFAULT 0,
    source_file    TEXT NOT NULL DEFAULT '',
    source_type    TEXT NOT NULL DEFAULT ''   -- 'history' | 'report' | 'seed'
);

CREATE TABLE IF NOT EXISTS observations (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL DEFAULT '',
    parent_id     TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    type          TEXT NOT NULL DEFAULT 'SPAN',
    start_ms      REAL NOT NULL DEFAULT 0,
    latency_ms    REAL NOT NULL DEFAULT 0,
    level         TEXT NOT NULL DEFAULT 'INFO',
    status        TEXT NOT NULL DEFAULT 'ok',
    input         TEXT,
    output        TEXT,
    model         TEXT NOT NULL DEFAULT '',
    task          TEXT NOT NULL DEFAULT '',
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost          REAL NOT NULL DEFAULT 0,
    metadata      TEXT NOT NULL DEFAULT '{}',
    source        TEXT NOT NULL DEFAULT ''   -- 'llm_usage' | 'llm_detail' | 'case' | 'seed'
);
CREATE INDEX IF NOT EXISTS idx_obs_run ON observations(run_id);
CREATE INDEX IF NOT EXISTS idx_obs_type ON observations(type);

CREATE TABLE IF NOT EXISTS tests (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    result      TEXT NOT NULL DEFAULT 'SKIP',
    duration_ms REAL NOT NULL DEFAULT 0,
    failure     TEXT NOT NULL DEFAULT '',
    request     TEXT NOT NULL DEFAULT '{}',
    response    TEXT NOT NULL DEFAULT '{}',
    assertions  TEXT NOT NULL DEFAULT '[]',
    llm_notes   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tests_run ON tests(run_id);

CREATE TABLE IF NOT EXISTS scores (
    id       TEXT PRIMARY KEY,
    run_id   TEXT NOT NULL DEFAULT '',
    test_id  TEXT NOT NULL DEFAULT '',
    name     TEXT NOT NULL DEFAULT '',
    value    REAL NOT NULL DEFAULT 0,
    comment  TEXT NOT NULL DEFAULT '',
    source   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_scores_run ON scores(run_id);
"""


def _j(value: Any) -> str:
    """把任意对象安全序列化为 JSON 字符串（失败回退 '{}'）。"""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "{}"


def _num(value: Any, default: float = 0.0) -> float:
    """安全转数值，非法输入回退默认值。"""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _str(value: Any, default: str = "") -> str:
    return str(value) if value is not None else default


class ObserveStore:
    """SQLite 读写封装：schema 初始化 + 幂等 upsert + 查询。

    线程安全：为每个使用线程懒创建独立连接（``check_same_thread=False`` +
    每线程一份），使多线程 HTTP 服务可安全并发读；写操作由调用方串行化。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # 预初始化 schema（任意线程建一次即可，幂等），再对存量库做列迁移。
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        self._migrate(conn)
        conn.commit()


    def _migrate(self, conn: sqlite3.Connection) -> None:
        """对存量库做轻量列迁移（CREATE TABLE IF NOT EXISTS 不会补新列）。"""

        def has_col(table: str, col: str) -> bool:
            return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))

        if not has_col("tests", "response"):
            conn.execute(
                "ALTER TABLE tests ADD COLUMN response TEXT NOT NULL DEFAULT '{}'"
            )
        if not has_col("tests", "assertions"):
            conn.execute(
                "ALTER TABLE tests ADD COLUMN assertions TEXT NOT NULL DEFAULT '[]'"
            )


    def _get_conn(self) -> sqlite3.Connection:
        """返回当前线程的专属连接（懒创建 + 初始化 row_factory）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """关闭当前线程的连接（其余线程的连接随线程退出由 GC 回收）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as exc:  # 关闭失败不影响调用方
                log.warning("observe store close error: %s", exc)

    # ── 写（幂等 upsert）──────────────────────────────────────────────
    def upsert_run(self, run: dict[str, Any]) -> None:
        """按 id 幂等写入一条 run。缺失字段用安全默认值。"""
        rid = _str(run.get("id") or run.get("run_id"))
        if not rid:
            log.warning("upsert_run: 缺 id，跳过")
            return
        self._get_conn().execute(
            """INSERT INTO runs
               (id,name,start_time,total,passed,failed,flaky,pass_rate,
                result_dist,status_dist,avg_duration_ms,p95_duration_ms,
                total_duration_ms,per_case_dur_ms,total_cost,source_file,source_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, start_time=excluded.start_time,
                 total=excluded.total, passed=excluded.passed,
                 failed=excluded.failed, flaky=excluded.flaky,
                 pass_rate=excluded.pass_rate, result_dist=excluded.result_dist,
                 status_dist=excluded.status_dist,
                 avg_duration_ms=excluded.avg_duration_ms,
                 p95_duration_ms=excluded.p95_duration_ms,
                 total_duration_ms=excluded.total_duration_ms,
                 per_case_dur_ms=excluded.per_case_dur_ms,
                 total_cost=excluded.total_cost,
                 source_file=excluded.source_file, source_type=excluded.source_type""",
            (
                rid,
                _str(run.get("name") or rid),
                _str(run.get("start_time") or run.get("run_time")),
                _int(run.get("total")),
                _int(run.get("passed")),
                _int(run.get("failed")),
                _int(run.get("flaky")),
                _num(run.get("pass_rate")),
                _j(run.get("result_dist") or {}),
                _j(run.get("status_dist") or {}),
                _num(run.get("avg_duration_ms")),
                _num(run.get("p95_duration_ms")),
                _num(run.get("total_duration_ms")),
                _j(run.get("per_case_dur_ms") or {}),
                _num(run.get("total_cost")),
                _str(run.get("source_file")),
                _str(run.get("source_type") or "history"),
            ),
        )
        self._get_conn().commit()

    def upsert_observation(self, obs: dict[str, Any]) -> None:
        """按 id 幂等写入一条 observation。"""
        oid = _str(obs.get("id"))
        if not oid:
            log.warning("upsert_observation: 缺 id，跳过")
            return
        self._get_conn().execute(
            """INSERT INTO observations
               (id,run_id,parent_id,name,type,start_ms,latency_ms,level,status,
                input,output,model,task,prompt_tokens,completion_tokens,cost,
                metadata,source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 run_id=excluded.run_id, parent_id=excluded.parent_id,
                 name=excluded.name, type=excluded.type, start_ms=excluded.start_ms,
                 latency_ms=excluded.latency_ms, level=excluded.level,
                 status=excluded.status, input=excluded.input, output=excluded.output,
                 model=excluded.model, task=excluded.task,
                 prompt_tokens=excluded.prompt_tokens,
                 completion_tokens=excluded.completion_tokens,
                 cost=excluded.cost, metadata=excluded.metadata, source=excluded.source""",
            (
                oid,
                _str(obs.get("run_id")),
                _str(obs.get("parent_id")),
                _str(obs.get("name")),
                str(obs.get("type") or "SPAN").upper()
                if str(obs.get("type") or "SPAN").upper() in OBS_TYPES
                else "SPAN",
                _num(obs.get("start_ms")),
                _num(obs.get("latency_ms")),
                _str(obs.get("level") or "INFO"),
                _str(obs.get("status") or "ok"),
                _j(obs.get("input")),
                _j(obs.get("output")),
                _str(obs.get("model")),
                _str(obs.get("task")),
                _int(obs.get("prompt_tokens")),
                _int(obs.get("completion_tokens")),
                _num(obs.get("cost")),
                _j(obs.get("metadata") or {}),
                _str(obs.get("source")),
            ),
        )
        self._get_conn().commit()

    def update_observation(self, oid: str, fields: dict[str, Any]) -> None:
        """按 id 部分更新一条 observation（仅设置给定字段，空缺保留）。

        供运行期实时链路（Tracer 双写）在 span finish 时补 latency/status/
        tokens/output 等。字段名必须为本表既有列；未知列抛出并仅告警。
        """
        if not oid:
            log.warning("update_observation: 缺 id，跳过")
            return
        if not fields:
            return
        # 允许的列白名单，防止拼 SQL 注入；缺失列直接忽略。
        allowed = {
            "run_id": _str, "parent_id": _str, "name": _str, "type": str,
            "start_ms": _num, "latency_ms": _num, "level": _str,
            "status": _str, "input": _j, "output": _j, "model": _str,
            "task": _str, "prompt_tokens": _int, "completion_tokens": _int,
            "cost": _num, "metadata": _j, "source": _str,
        }
        sets, values = [], []
        for key, conv in allowed.items():
            if key not in fields:
                continue
            sets.append(f"{key}=?")
            values.append(conv(fields[key]))
        if not sets:
            return
        values.append(oid)
        self._get_conn().execute(
            f"UPDATE observations SET {', '.join(sets)} WHERE id=?", tuple(values)
        )
        self._get_conn().commit()

    def upsert_test(self, test: dict[str, Any]) -> None:
        """按 id 幂等写入一条用例结果。id 缺省时由 run_id+name 派生。"""
        tid = _str(test.get("id"))
        if not tid:
            tid = f"{_str(test.get('run_id'))}:{_str(test.get('name'))}"
        self._get_conn().execute(
            """INSERT INTO tests
               (id,run_id,name,result,duration_ms,failure,request,response,
                assertions,llm_notes)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                run_id=excluded.run_id, name=excluded.name, result=excluded.result,
                duration_ms=excluded.duration_ms, failure=excluded.failure,
                request=excluded.request, response=excluded.response,
                assertions=excluded.assertions, llm_notes=excluded.llm_notes""",
            (
                tid,
                _str(test.get("run_id")),
                _str(test.get("name")),
                str(test.get("result") or "SKIP").upper(),
                _num(test.get("duration_ms")),
                _str(test.get("failure")),
                _j(test.get("request") or {}),
                _j(test.get("response") or {}),
                _j(test.get("assertions") or []),
                _j(test.get("llm_notes") or {}),
            ),
        )
        self._get_conn().commit()

    def upsert_score(self, score: dict[str, Any]) -> None:
        """按 id 幂等写入一条打分。"""
        sid = _str(score.get("id"))
        if not sid:
            # 缺 id 时由 run+test+name 派生稳定 id。
            sid = (
                f"{_str(score.get('run_id'))}:{_str(score.get('test_id'))}:"
                f"{_str(score.get('name'))}"
            )
        self._get_conn().execute(
            """INSERT INTO scores
               (id,run_id,test_id,name,value,comment,source)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 run_id=excluded.run_id, test_id=excluded.test_id,
                 name=excluded.name, value=excluded.value,
                 comment=excluded.comment, source=excluded.source""",
            (
                sid,
                _str(score.get("run_id")),
                _str(score.get("test_id")),
                _str(score.get("name")),
                _num(score.get("value")),
                _str(score.get("comment")),
                _str(score.get("source")),
            ),
        )
        self._get_conn().commit()

    # ── 读 ────────────────────────────────────────────────────────────
    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        return dict(r)

    def list_runs(self, page: int = 1, limit: int = 50,
                  name: Optional[str] = None) -> tuple[list[dict[str, Any]], int]:
        """分页列出 runs；可选按名称过滤。返回 (行列表, 总数)。"""
        sql = "SELECT * FROM runs"
        args: list[Any] = []
        if name:
            sql += " WHERE name LIKE ?"
            args.append(f"%{name}%")
        total = self._get_conn().execute(
            f"SELECT COUNT(*) FROM ({sql})", args
        ).fetchone()[0]  # noqa: S608 — 固定拼接，无用户注入
        sql += " ORDER BY start_time DESC LIMIT ? OFFSET ?"
        args += [int(limit), (int(page) - 1) * int(limit)]
        rows = self._get_conn().execute(sql, args).fetchall()
        return [self._row(r) for r in rows], int(total)

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        row = self._get_conn().execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._row(row) if row else None

    def observations_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._get_conn().execute(
            "SELECT * FROM observations WHERE run_id=? ORDER BY start_ms ASC",
            (run_id,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def get_observation(self, obs_id: str) -> Optional[dict[str, Any]]:
        row = self._get_conn().execute(
            "SELECT * FROM observations WHERE id=?", (obs_id,)
        ).fetchone()
        return self._row(row) if row else None

    def list_observations(self, run_id: Optional[str] = None,
                          obs_type: Optional[str] = None,
                          page: int = 1, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
        """平铺列出 observations；可选按 run_id / type 过滤。"""
        sql = "SELECT * FROM observations WHERE 1=1"
        args: list[Any] = []
        if run_id:
            sql += " AND run_id=?"
            args.append(run_id)
        if obs_type:
            sql += " AND type=?"
            args.append(obs_type.upper())
        total = self._get_conn().execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]  # noqa: S608
        sql += " ORDER BY start_ms ASC LIMIT ? OFFSET ?"
        args += [int(limit), (int(page) - 1) * int(limit)]
        return [self._row(r) for r in self._get_conn().execute(sql, args).fetchall()], int(total)

    def tests_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._get_conn().execute(
            "SELECT * FROM tests WHERE run_id=? ORDER BY name ASC", (run_id,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def list_tests(self, run_id: Optional[str] = None,
                   result: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tests WHERE 1=1"
        args: list[Any] = []
        if run_id:
            sql += " AND run_id=?"
            args.append(run_id)
        if result:
            sql += " AND result=?"
            args.append(result.upper())
        rows = self._get_conn().execute(sql + " ORDER BY name ASC", args).fetchall()
        return [self._row(r) for r in rows]

    def scores_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._get_conn().execute(
            "SELECT * FROM scores WHERE run_id=? ORDER BY name ASC", (run_id,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def metrics(self) -> dict[str, Any]:
        """LLM 成本/token/成功率/模型分布 看板汇总（全库）。"""
        rows = self._get_conn().execute(
            "SELECT type,count(*) n,sum(prompt_tokens) pt,sum(completion_tokens) ct,"
            "sum(cost) cost_ FROM observations"
        ).fetchall()
        by_type: dict[str, dict[str, Any]] = {}
        total_cost = 0.0
        total_tokens = 0
        for r in rows:
            t = str(r["type"] or "SPAN")
            by_type[t] = {
                "requests": int(r["n"] or 0),
                "prompt_tokens": int(r["pt"] or 0),
                "completion_tokens": int(r["ct"] or 0),
                "cost": float(r["cost_"] or 0.0),
            }
            total_cost += float(r["cost_"] or 0.0)
            total_tokens += int(r["pt"] or 0) + int(r["ct"] or 0)
        # 按模型的 token/成本。
        model_rows = self._get_conn().execute(
            "SELECT model,count(*) n,sum(prompt_tokens) pt,sum(completion_tokens) ct,"
            "sum(cost) cost_ FROM observations WHERE model<>'' "
            "GROUP BY model ORDER BY cost_ DESC"
        ).fetchall()
        by_model = {
            str(m["model"]): {
                "requests": int(m["n"] or 0),
                "prompt_tokens": int(m["pt"] or 0),
                "completion_tokens": int(m["ct"] or 0),
                "cost": float(m["cost_"] or 0.0),
            }
            for m in model_rows
        }
        # 成功率：以 tests 的 PASS（含 FLKY 视为绿）与 FAIL。
        ts = self._get_conn().execute(
            "SELECT result,count(*) n FROM tests GROUP BY result"
        ).fetchall()
        result_dist = {str(t["result"]): int(t["n"] or 0) for t in ts}
        passed = result_dist.get("PASS", 0) + result_dist.get("FLKY", 0)
        failed = result_dist.get("FAIL", 0)
        total_tests = sum(result_dist.values())
        success_rate = (passed / total_tests * 100.0) if total_tests else 0.0
        n_runs = self._get_conn().execute("SELECT COUNT(*) n FROM runs").fetchone()["n"]
        return {
            "runs": int(n_runs or 0),
            "total_cost": total_cost,
            "total_tokens": total_tokens,
            "by_type": by_type,
            "by_model": by_model,
            "tests": result_dist,
            "passed": passed,
            "failed": failed,
            "total_tests": total_tests,
            "success_rate": round(success_rate, 2),
        }

    def analysis(self) -> dict[str, Any]:
        """测试结果分析聚合（跨全部 run）：状态分布/flaky/用例排名/耗时趋势。

        与 :meth:`metrics`（LLM 成本视角）互补：本方法聚焦「测试结果」维度，
        供前端「结果分析」视图使用。全部从 runs/tests 现有表计算，无需改 schema。
        """
        conn = self._get_conn()

        # 1) 状态分布：跨 run 合并 status_dist（200/404/500…），并按 HTTP 状态归因。
        state_dist: dict[str, int] = {}
        for r in conn.execute("SELECT status_dist FROM runs WHERE status_dist<>'{}'"):
            raw = r["status_dist"]
            try:
                dist = json.loads(raw or "{}")
            except (ValueError, TypeError):
                continue
            for k, v in dist.items():
                state_dist[str(k)] = state_dist.get(str(k), 0) + int(v or 0)

        # 2) flaky：总量 + 出现过 flaky 的 run 占比。
        row = conn.execute(
            "SELECT SUM(flaky) s, SUM(CASE WHEN flaky>0 THEN 1 ELSE 0 END) n FROM runs"
        ).fetchone()
        total_flaky = int(row["s"] or 0)
        flaky_runs = int(row["n"] or 0)
        n_runs = int(conn.execute("SELECT COUNT(*) n FROM runs").fetchone()["n"] or 0)

        # 3) 用例排名：同一用例跨 run 的通过率（至少出现 2 个 run 才算稳定指标）。
        case_rows = conn.execute(
            """SELECT name,
                      COUNT(*) total,
                      SUM(CASE WHEN result='PASS' OR result='FLKY' THEN 1 ELSE 0 END) passed,
                      SUM(CASE WHEN result='FAIL' THEN 1 ELSE 0 END) failed,
                      ROUND(AVG(duration_ms), 1) avg_dur
               FROM tests GROUP BY name HAVING total>=2
               ORDER BY passed*1.0/total ASC, avg_dur DESC"""
        ).fetchall()
        case_ranking = [
            {
                "name": str(c["name"]),
                "total": int(c["total"]),
                "passed": int(c["passed"] or 0),
                "failed": int(c["failed"] or 0),
                "pass_rate": round(int(c["passed"] or 0) * 100.0 / int(c["total"]), 1),
                "avg_duration_ms": float(c["avg_dur"] or 0.0),
            }
            for c in case_rows
        ]

        # 4) 耗时/通过率/flaky 历史趋势（按时间升序，供折线图）。
        trend_rows = conn.execute(
            "SELECT start_time, pass_rate, total_duration_ms, flaky "
            "FROM runs ORDER BY start_time ASC"
        ).fetchall()
        duration_trend = [
            {
                "time": str(t["start_time"] or ""),
                "pass_rate": float(t["pass_rate"] or 0.0),
                "total_duration_ms": float(t["total_duration_ms"] or 0.0),
                "flaky": int(t["flaky"] or 0),
            }
            for t in trend_rows
        ]

        return {
            "state_dist": state_dist,
            "flaky": {
                "total": total_flaky,
                "runs_affected": flaky_runs,
                "runs_total": n_runs,
            },
            "case_ranking": case_ranking,
            "duration_trend": duration_trend,
        }

    def counts(self) -> dict[str, int]:
        """各类条目数量（用于 ingest 摘要）。"""
        return {
            "runs": int(self._get_conn().execute("SELECT COUNT(*) n FROM runs").fetchone()["n"]),
            "observations": int(self._get_conn().execute(
                "SELECT COUNT(*) n FROM observations").fetchone()["n"]),
            "tests": int(self._get_conn().execute(
                "SELECT COUNT(*) n FROM tests").fetchone()["n"]),
            "scores": int(self._get_conn().execute(
                "SELECT COUNT(*) n FROM scores").fetchone()["n"]),
        }


def open_store(db_path: str | Path | None = None) -> ObserveStore:
    """打开（必要时创建）存储。缺省路径落在当前工作目录。"""
    path = Path(db_path) if db_path else Path(DEFAULT_DB)
    return ObserveStore(path)
