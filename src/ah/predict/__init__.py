"""预测分析层（predict）。

基于运行时产生的结果历史库做推断与回归辅助，全部是 *helper*，绝非测试驱动：

- :mod:`ah.predict.history` — 跨 run 累积用例结果的 JSONL 历史库。
- :mod:`ah.predict.risk`     — 按接口/功能点计算风险概率、回归推荐。
- :mod:`ah.predict.generator` — 用 LLM 从失败模式/历史生成新候选用例并验证。
- :mod:`ah.predict.contract` — 从 OpenAPI/响应样本确定性挖掘 extract/assert 建议，
  以及从自然语言意图生成新用例（NL → YAML，`case_from_intent`）。

Hermetic：历史读写与风险统计是纯本地文件/算术；只有 :mod:`generator`
在 LLM 启用时才发网络（禁用时回退为确定性空结果）。CI/默认路径零依赖。
"""

from __future__ import annotations

from ah.predict.contract import ContractAdvice, ContractMiner, case_from_intent
from ah.predict.history import HistoryStore, load_history
from ah.predict.risk import RegressionPlan, RiskProfile, rank_regression, risk_profiles

__all__ = [
    "HistoryStore",
    "load_history",
    "RiskProfile",
    "RegressionPlan",
    "rank_regression",
    "risk_profiles",
    "ContractAdvice",
    "ContractMiner",
    "case_from_intent",
]
