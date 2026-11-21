#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/10/15 19:25
# @Filename : __init__.py
# @Author   : Alan_Hsu
"""本地类 Langfuse 可观测性平台（observe 子包）。

提供 SQLite 存储（store）、现有 jsonl/report 数据摄入（ingest）、
LLM call 明细持久化（llmdetail）与本地 HTTP 服务（server / cli）。
Hermetic 安全：零网络、可离线、LLMOFF 可测。
"""

from ah.observe.store import ObserveStore, open_store

__all__ = ["ObserveStore", "open_store"]
