#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15 19:50
# @Filename : server.py
# @Author   : Alan_Hsu
"""本地可观测 HTTP 服务（Langfuse 风格 REST，标准库实现）。

仅依赖标准库（http.server + sqlite），零新增运行时依赖，LLMOFF/hermetic 安全。
端点：
- ``GET  /api/traces``           Traces 列表（分页 / name 过滤）
- ``GET  /api/traces/{id}``      Trace 详情（meta + observations 树 + tests + scores）
- ``GET  /api/observations``     平铺列表（run_id / type 过滤）
- ``GET  /api/observations/{id}`` 单条 observation（含 input/output/usage/cost）
- ``GET  /api/tests``            用例结果列表
- ``GET  /api/scores``           打分列表
- ``GET  /api/metrics``          LLM 成本/token/成功率/模型分布 看板
- ``GET  /<static>``             托管前端构建产物（web/dist）
- ``GET  /``                     索引页（前端或占位说明）
响应统一 JSON（Langfuse 风格平铺数组 + 分页）。

数据来自 :class:`ah.observe.store.ObserveStore`；调用方先用 ingest/seed 灌数据，
再启动服务。本模块不主动灌数据，职责单一。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from ah.observe.store import ObserveStore
from ah.utils.logging import get_logger

log = get_logger("observe.server")

#: 前端构建产物目录（相对 harness 根；缺省相对 cwd）。
WEB_DIST = Path("web") / "dist"

#: 允许的静态扩展（前端资源）。
_STATIC_EXTS = {".html", ".js", ".css", ".svg", ".png", ".ico", ".map", ".json", ".woff2"}


def _json_body(dumps_value: Any, status: int = 200) -> tuple[str, int, str]:
    """包装 JSON 响应体（Langfuse 风格：dict/list 直接序列化）。"""
    body = json.dumps(dumps_value, ensure_ascii=False, default=str)
    return body, status, "application/json; charset=utf-8"


class ObserveHandler(BaseHTTPRequestHandler):
    """REST handler：把 /api/* 路由到 store 查询，其余托静态。"""

    server_version = "LocalObs/1.0"
    #: 由 serve() 注入的 store；请求期间共享只读连接（SQLite 读并发安全）。
    store: Optional[ObserveStore] = None
    web_dir: Optional[Path] = None

    # ── 基础 ──────────────────────────────────────────────────────────
    def log_message(self, fmt: str, *args: Any) -> None:  # 走 logging 而非 stderr
        log.info("http: %s %s", self.address_string(), fmt % args)

    def _send(self, body: str, status: int, ctype: str) -> None:
        data = body.encode("utf-8") if not isinstance(body, bytes) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")  # 允许前端 dev server 跨域
        self.end_headers()
        self.wfile.write(data)

    def _json(self, value: Any, status: int = 200) -> None:
        body, st, ctype = _json_body(value, status)
        self._send(body, st, ctype)

    def _query(self) -> dict[str, str]:
        """把 query string 解析为 dict（多值取首个）。"""
        q = urlparse(self.path).query
        out: dict[str, str] = {}
        for kv in q.split("&"):
            if not kv:
                continue
            k, _, v = kv.partition("=")
            out[k] = v
        return out

    def _page(self) -> tuple[int, int]:
        """从 query 解析 page(1-based)/limit。"""
        q = self._query()
        try:
            page = max(1, int(q.get("page", "1")))
        except (TypeError, ValueError):
            page = 1
        try:
            limit = max(1, int(q.get("limit", "50")))
        except (TypeError, ValueError):
            limit = 50
        return page, min(limit, 200)

    # ── 路由 ──────────────────────────────────────────────────────────
    # do_GET 是 BaseHTTPRequestHandler 约定命名，无需按小鸟大小写改造。
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if self.store is None:
            self._json({"error": "store 未初始化"}, 500)
            return
        try:
            self._route(parsed, path)
        except Exception as exc:  # 路由异常不崩服务
            log.warning("route error %s: %s", path, exc, exc_info=True)
            self._json({"error": "internal error"}, 500)

    def _route(self, parsed: Any, path: str) -> None:
        store = self.store
        assert store is not None


        # /api/metrics
        if path == "/api/metrics":
            self._json(store.metrics())
            return
        # /api/analysis
        if path == "/api/analysis":
            self._json(store.analysis())
            return
        # /api/traces
        if path == "/api/traces":
            page, limit = self._page()
            name = self._arg("name")
            rows, total = store.list_runs(page=page, limit=limit, name=name or None)
            self._json({"data": rows, "total": total, "page": page, "limit": limit})
            return
        # /api/traces/{id}
        if path.startswith("/api/traces/"):
            rid = unquote(path[len("/api/traces/"):].split("/")[0])
            run = store.get_run(rid)
            if run is None:
                self._json({"error": f"trace {rid} 不存在"}, 404)
                return
            self._json({
                "id": rid,
                "name": run.get("name"),
                "start_time": run.get("start_time"),
                "total": run.get("total"),
                "passed": run.get("passed"),
                "failed": run.get("failed"),
                "flaky": run.get("flaky"),
                "pass_rate": run.get("pass_rate"),
                "result_dist": json.loads(run.get("result_dist") or "{}"),
                "status_dist": json.loads(run.get("status_dist") or "{}"),
                "avg_duration_ms": run.get("avg_duration_ms"),
                "p95_duration_ms": run.get("p95_duration_ms"),
                "total_duration_ms": run.get("total_duration_ms"),
                "per_case_dur_ms": json.loads(run.get("per_case_dur_ms") or "{}"),
                "total_cost": run.get("total_cost"),
                "source_type": run.get("source_type"),
                # observations 结构化为树（按 parent_id 层级）。
                "observations": _build_tree(store.observations_for_run(rid)),
                "tests": [_decode_test(t) for t in store.tests_for_run(rid)],
                "scores": store.scores_for_run(rid),
            })
            return
        # /api/observations/{id}
        if path.startswith("/api/observations/"):
            oid = unquote(path[len("/api/observations/"):].split("/")[0])
            obs = store.get_observation(oid)
            if obs is None:
                self._json({"error": f"observation {oid} 不存在"}, 404)
                return
            obs = dict(obs)
            obs["metadata"] = json.loads(obs.get("metadata") or "{}")
            self._json(obs)
            return
        # /api/observations
        if path == "/api/observations":
            page, limit = self._page()
            rid = self._arg("run_id")
            typ = self._arg("type")
            rows, total = store.list_observations(
                run_id=rid or None, obs_type=typ or None, page=page, limit=limit
            )
            self._json({"data": rows, "total": total, "page": page, "limit": limit})
            return
        # /api/tests
        if path == "/api/tests":
            rid = self._arg("run_id")
            result = self._arg("result")
            rows = store.list_tests(run_id=rid or None, result=result or None)
            self._json({"data": rows, "total": len(rows)})
            return
        # /api/scores
        if path == "/api/scores":
            rows = store.scores_for_run(self._arg("run_id")) if self._arg("run_id") else []
            self._json({"data": rows, "total": len(rows)})
            return
        # 静态（前端构建产物）
        if self.web_dir is not None and self.web_dir.is_dir():
            if self._serve_static(path):
                return
        # 根路径 → 索引说明。
        if path in ("/", "/index.html"):
            self._json({
                "service": "local-obs (Langfuse-style)",
                "endpoints": [
                    "/api/traces", "/api/traces/{id}", "/api/observations",
                    "/api/observations/{id}", "/api/tests", "/api/scores", "/api/metrics",
                ],
                "hint": "前端就绪后访问 / （Vite build 产物在 web/dist）。",
            })
            return
        self._json({"error": f"not found: {path}"}, 404)

    def _arg(self, key: str) -> str:
        """读取单个 query 参数（多值取首个，缺省空串）。"""
        return self._query().get(key, "")

    def _serve_static(self, path: str) -> bool:
        """托静态文件；path 为空根 → 尝试 index.html。"""
        assert self.web_dir is not None
        rel = path.lstrip("/")
        if not rel or rel.endswith("/"):
            rel = rel + "index.html" if rel else "index.html"
        target = (self.web_dir / rel).resolve()
        # 防目录穿越：必须仍在 web_dir 内。
        try:
            target.relative_to(self.web_dir.resolve())
        except ValueError:
            self._json({"error": "forbidden"}, 403)
            return True
        if not target.is_file():
            return False
        ext = target.suffix.lower()
        ctype = "text/html" if ext == ".html" else (
            "application/javascript" if ext == ".js" else (
                "text/css" if ext == ".css" else (
                    "application/json" if ext == ".json" else (
                        "image/svg+xml" if ext == ".svg" else "application/octet-stream"
                    )
                )
            )
        )
        try:
            self._send(target.read_bytes(), 200, ctype)
        except OSError as exc:
            log.warning("static read error %s: %s", target, exc)
            self._json({"error": "read failed"}, 500)
        return True


def _build_tree(obs_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把平铺 observations 组织成父→子树（Langfuse span 树）。

    无 parent_id/root 的作为顶层；子节点挂到 name 匹配或 parent_id 匹配的父节点。
    每个节点保留原始行 + ``children``；input/output 若为 JSON 文本则解析为对象，
    并注入 ``has_input``/``has_output`` 供前端判断是否可展开 LLM 明细。
    """
    nodes: dict[str, dict[str, Any]] = {}
    for o in obs_list:
        d = dict(o)
        d["children"] = []
        # input/output 在 store 中是 JSON 文本（含 "null"）；先解析为真实对象。
        for key in ("input", "output"):
            raw = d.get(key)
            if isinstance(raw, str) and raw:
                try:
                    d[key] = json.loads(raw)
                except (ValueError, TypeError):
                    d[key] = raw  # 非 JSON 文本则原样保留
        # has_input/has_output 依据解析后的实际内容（跳过 "null"/空串）。
        d["has_input"] = d.get("input") not in (None, "", {})
        d["has_output"] = d.get("output") not in (None, "", {})
        nodes[str(d["id"])] = d
    roots: list[dict[str, Any]] = []
    for o in obs_list:
        d = nodes[str(o["id"])]
        pid = str(o.get("parent_id") or "")
        if pid and pid in nodes:
            nodes[pid]["children"].append(d)
        else:
            roots.append(d)
    return roots


def _decode_test(t: dict[str, Any]) -> dict[str, Any]:
    """把一条 test 行的 JSON 文本字段解码为对象，供前端详情渲染。"""
    d = dict(t)
    for key in ("request", "response", "assertions", "llm_notes"):
        raw = d.get(key)
        if isinstance(raw, str) and raw:
            try:
                d[key] = json.loads(raw)
            except (ValueError, TypeError):
                d[key] = raw
    return d


def serve(
    store: ObserveStore,
    *,
    host: str = "127.0.0.1",
    port: int = 8111,
    web_dir: str | Path | None = None,
    block: bool = True,
) -> ThreadingHTTPServer:
    """启动本地服务。

    ``block=True``：前台阻塞运行（serve_forever），Ctrl-C 停止。
    ``block=False``：后台线程运行，返回 server 供测试/嵌入（调用方负责
    ``server.shutdown()`` / ``server.server_close()``）。
    """
    ObserveHandler.store = store
    ObserveHandler.web_dir = (
        Path(web_dir) if web_dir else (WEB_DIST if WEB_DIST.is_dir() else None)
    )
    server = ThreadingHTTPServer((host, port), ObserveHandler)
    log.info("local-obs 服务已启动: http://%s:%s/ (store=%s)", host, port, store.db_path)
    if block:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log.info("local-obs 服务已停止")
        finally:
            server.server_close()
    else:
        _start_background(server)
    return server


def _start_background(server: ThreadingHTTPServer) -> None:
    """在守护线程里跑 serve_forever，使 block=False 的服务可处理请求。"""
    import threading

    t = threading.Thread(target=server.serve_forever, daemon=True, name="local-obs-serve")
    t.start()


if __name__ == "__main__":
    # 便捷入口：python -m ah.observe.server [--db PATH] [--port N] [--web DIR] [--seed]
    import argparse

    from ah.observe.ingest import seed_demo

    ap = argparse.ArgumentParser(description="本地类 Langfuse 可观测服务")
    ap.add_argument("--db", default="local-obs.sqlite3", help="SQLite 路径")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8111)
    ap.add_argument("--web", default=None, help="前端构建产物目录")
    ap.add_argument("--seed", action="store_true", help="启动前灌入合成演示数据")
    args = ap.parse_args()
    store = ObserveStore(args.db)
    if args.seed:
        n = seed_demo(store)
        log.info("已灌入演示数据: %s", n)
    serve(store, host=args.host, port=args.port, web_dir=args.web)
