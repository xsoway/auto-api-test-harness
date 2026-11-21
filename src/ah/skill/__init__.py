#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/17 10:00
# @Filename : __init__.py
# @Author   : Alan_Hsu
"""API Skill：可复用步骤单元 + 声明式互调（call/with/assign）+ 静态校验。

Skill = 一段可复用步骤包（定义见 :mod:`ah.skill.model`），skill 间可声明式互调，
多个 skill 可任意组装成业务流程（``flow``，阶段 B 落地）。本阶段交付「定义 + 加载 +
静态校验 + CLI」，纯静态、零网络，``LLMOFF=1`` 安全。
"""
