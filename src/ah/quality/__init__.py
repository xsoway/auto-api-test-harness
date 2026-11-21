#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026/11/15 15:10
# @Filename : __init__.py
# @Author   : Alan_Hsu
"""质量治理包（后周期）：健康度评分 / 冗余识别 / 复盘清洗。

对应《AI驱动"自动化测试后周期"诊治体系升级》四大支柱在本框架的落地：

- :mod:`ah.quality.health` — 全用例健康度评分(0-100) + 断言密度 + 一票否决 + 无效断言清单。
- :mod:`ah.quality.redundancy` — 用例等价重复指纹识别 + 低价值僵尸用例筛排 + 语义等价(LLM 分支)。
- :mod:`ah.quality.audit` — 已入库用例（含 AI 生成）复盘清洗，防"用新问题解决旧问题"。

全部能力确定性优先，LLM 关闭回退，满足 Hermetic 铁律。
"""
