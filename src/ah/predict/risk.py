"""风险概率建模 + 回归测试推荐。

输入一批历史结果（:class:`HistoryStore` 读回的 dict 记录），对每个接口
（按 ``interface`` 主键）计算：

- ``failure_rate`` — 失败占比。
- ``risk`` — 一个 [0,1] 的预测风险概率，融合失败率、近期权重和类别严重度。
- ``confidence`` — 基于样本量的置信度（样本越少置信越低）。
- ``top_failure_classes`` — 该接口最常见失败类别，供回归定位。

:class:`rank_regression` 依风险降序返回一个回归计划（:class:`RegressionPlan`），
用于"下次回归重点跑哪些接口、预测哪个接口最容易出问题"。全部纯本地算术。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ah.evidence.laya_gate import STAGE_RISK
from ah.utils.logging import get_logger

log = get_logger("predict.risk")

#: 各类别对"该接口高风险"的加权。断言/状态类多为确定性错误（更可信），
#: 超时/网络类可能是瞬时（权重略低）。可调。
_CLASS_SEVERITY = {
    "assertion": 1.0,
    "schema": 1.0,
    "jsonpath": 1.0,
    "status": 0.9,
    "extraction": 0.85,
    "timeout": 0.5,
    "network": 0.5,
    "unknown": 0.7,
}

#: 最小样本量才算可信（低于此按比例压低置信度）。
_MIN_SAMPLES = 3
#: 完全可信所需样本量。
_SATURATE_SAMPLES = 10


@dataclass
class RiskProfile:
    """单个接口的风险画像。"""

    interface: str
    total: int
    failures: int
    failure_rate: float
    risk: float
    confidence: float
    top_failure_classes: list[str] = field(default_factory=list)
    #: Laya 前置打分写入的原始信号（未采用时为空 dict）。
    laya_signal: dict = field(default_factory=dict)


def _severity(class_name: str) -> float:
    return _CLASS_SEVERITY.get(class_name, 0.7)


def risk_profiles(
    rows: list[dict[str, Any]], *, gate: Any = None
) -> list[RiskProfile]:
    """从历史记录计算各接口的风险画像，忽略成功记录的类别维度。

    前置决策分流：当 ``gate`` 非 None 且该环节开闸时，先咨询 LayaGate 给每个
    接口打一个 [0,1] 回归风险分。高置信 AUTO 且 value 为合法 0-1 数值 → 用该值
    覆盖算术 risk 并把 ``signal`` 写入 ``laya_signal``；否则（未开闸/低置信/非法
    值/异常）回退下方纯算术 risk（字节等价）。gate=None（默认/off）→ 完全跳过，
    零网络。
    """
    _risk_questions = {
        "risk_score": {
            "type": "score",
            "instructions": "给这个接口的回归风险打分（0-1，越高越需要优先回归）",
            "criteria": [
                "低风险，近期稳定无失败",
                "中风险，偶发失败或类别可疑",
                "高风险，多次失败或破坏性类别",
            ],
        }
    }
    by_interface: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("interface") or str(row.get("name", ""))
        bucket = by_interface.setdefault(
            key,
            {"total": 0, "failures": 0, "classes": {}},
        )
        bucket["total"] += 1
        is_pass = row.get("result", "FAIL") == "PASS"
        if not is_pass:
            bucket["failures"] += 1
            fcls = row.get("failure_class") or "unknown"
            bucket["classes"][fcls] = bucket["classes"].get(fcls, 0) + 1

    profiles: list[RiskProfile] = []
    for interface, bucket in by_interface.items():
        total = bucket["total"]
        failures = bucket["failures"]
        failure_rate = failures / total if total else 0.0
        # 风险 = 失败率 × 类别严重度均值（无失败时为 0）。
        if failures:
            top_classes = sorted(
                bucket["classes"], key=lambda c: bucket["classes"][c], reverse=True
            )
            avg_sev = sum(_severity(c) * bucket["classes"][c] for c in bucket["classes"])
            avg_sev /= failures
        else:
            top_classes = []
            avg_sev = 0.0
        risk = min(1.0, failure_rate * avg_sev)
        confidence = min(1.0, total / _SATURATE_SAMPLES) if total >= 1 else 0.0
        if total < _MIN_SAMPLES:
            confidence *= total / _MIN_SAMPLES
        # 前置 Laya 打分：仅当 gate 显式传入（非 None）且该环节开闸时咨询，
        # 高置信 AUTO + 合法 0-1 数值才覆盖；否则一律回退算术 risk（字节等价）。
        laya_signal: dict = {}
        if gate is not None:
            try:
                failure_classes = ", ".join(
                    f"{c}×{n}" for c, n in bucket["classes"].items()
                ) or "无"
                state = (
                    f"接口 {interface} | 样本量 {total} | 失败 {failures} "
                    f"| 失败率 {failure_rate:.2%} | 失败类别 {failure_classes}"
                )
                dec = gate.best(STAGE_RISK, state, _risk_questions)
                if dec.get("action") == "auto":
                    value = dec.get("value")
                    if isinstance(value, (int, float)):
                        # laya score 返回 0-based 期望分（区间 [0, n_bands-1]），
                        # 除以 (n_bands-1) 归一化到 [0,1] 再用于风险值。
                        n_bands = max(1, len(_risk_questions["risk_score"]["criteria"]))
                        laya_score = (
                            float(value) / (n_bands - 1) if n_bands > 1 else float(value)
                        )
                        if 0.0 <= laya_score <= 1.0:
                            risk = laya_score
                            laya_signal = dict(dec.get("signal") or {})
                        else:
                            log.warning(
                                "laya risk 越界值 %s 被忽略，回退算术 risk", value
                            )
            except Exception as exc:  # Laya 故障绝不改变原流程
                log.warning("laya risk 分流失败，回退算术 risk: %s", exc)
        profiles.append(
            RiskProfile(
                interface=interface,
                total=total,
                failures=failures,
                failure_rate=round(failure_rate, 4),
                risk=round(risk, 4),
                confidence=round(confidence, 4),
                top_failure_classes=top_classes,
                laya_signal=laya_signal,
            )
        )
    profiles.sort(key=lambda p: (p.risk, p.total), reverse=True)
    return profiles


@dataclass
class RegressionPlan:
    """一次回归的运行推荐。"""

    profiles: list[RiskProfile] = field(default_factory=list)
    #: 建议作为回归必跑集的接口（风险 > threshold）。
    focused: list[RiskProfile] = field(default_factory=list)

    @property
    def focused_interfaces(self) -> list[str]:
        return [p.interface for p in self.focused]

    def to_dict(self) -> dict[str, Any]:
        return {
            "focused": [
                {
                    "interface": p.interface,
                    "risk": p.risk,
                    "confidence": p.confidence,
                    "failures": p.failures,
                    "total": p.total,
                    "top_failure_classes": p.top_failure_classes,
                }
                for p in self.focused
            ],
            "all_profiles": [
                {
                    "interface": p.interface,
                    "risk": p.risk,
                    "confidence": p.confidence,
                }
                for p in self.profiles
            ],
        }


def rank_regression(
    rows: list[dict[str, Any]], *, risk_threshold: float = 0.3, top_k: int = 5
) -> RegressionPlan:
    """生成回归计划：按风险降序，挑出风险超阈值的接口作为必跑集。

    Hermetic 纯统计；无历史时返回空计划。
    """
    profiles = risk_profiles(rows)
    focused = [p for p in profiles if p.risk >= risk_threshold][:top_k]
    return RegressionPlan(profiles=profiles, focused=focused)
