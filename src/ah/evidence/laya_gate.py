"""LayaGate：LLM-judge 决策点之前的高频决策分流闸门。

设计意图（详见 tasks/active/laya-gate-integration/ROADMAP.md）：
- 在每一个 **LLM-judge 决策点** 前，先把"判断子问题"拆成原子题交给 Laya
  （choice/score/noul + 概率）做低延迟、低方差、零成本的预先分流。
- **Laya 是"决策分流"，不是替代 LLM**：高置信 → 直接判定；低置信/低覆盖 →
  升级原 LLM-judge（附 Laya 信号作参考注记）。生成/推理仍归 LLM。
- **默认关**：``AH_LAYA`` 未设置或为 ``off`` 时 ``decide()`` 一律返回
  :attr:`Action.ESCALATE`，原流程**字节等价**（回归护栏），并保持 ``LLMOFF`` 铁律。

校准结论（阶段0，已落盘）：
- 意图识别 44 真实用例：阈值 ``0.6`` → 覆盖 45.5%、自动放行准确率 100%。→ 可开闸。
- 语义断言 14 样本：最高自动准确率 0.833、0.6 时 0.769，不达 ≥0.9 安全线。→ 仅信号注记。

开关取值：``AH_LAYA=off|intent|design|assert|all``，`;` 或 `,` 分隔可叠加。
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol

from ah.utils.logging import get_logger

log = get_logger("laya_gate")

# ── 环节标识 ────────────────────────────────────────────────────────────────
STAGE_INTENT = "intent"
STAGE_DESIGN = "design"
STAGE_ASSERT = "assert"
STAGE_DATA = "data"
STAGE_REPORT = "report"
STAGE_HEAL = "heal"
# P1 新增决策环节：失败 triage 分类 / 演进候选质量门控 / 重试-回退决策。
STAGE_TRIAGE = "triage"
STAGE_QUALITY = "quality"
STAGE_RETRY = "retry"
# P2 新增决策环节：接口风险分级（score）/ 工具调用风险路由 / 演进停止条件（noul）。
STAGE_RISK = "risk"
STAGE_TOOL = "tool"
STAGE_STOP = "stop"
ALL_STAGES = (
    STAGE_INTENT, STAGE_DESIGN, STAGE_ASSERT, STAGE_DATA, STAGE_REPORT, STAGE_HEAL,
    STAGE_TRIAGE, STAGE_QUALITY, STAGE_RETRY,
    STAGE_RISK, STAGE_TOOL, STAGE_STOP,
)

# 默认开关：仅意图环节可开闸（校准达标）；其余默认关闭。
_DEFAULT_ENABLED = {STAGE_INTENT}

# 每环节默认置信阈值（源自阶段0校准结论 / 保守留白）。
_DEFAULT_THRESHOLD: dict[str, float] = {
    STAGE_INTENT: 0.6,   # 校准：@0.6 自动准确率 100%
    STAGE_ASSERT: 0.9,   # 校准未达标 → 默认不开，仅当显式启用时用最严阈值
    STAGE_TRIAGE: 0.9,   # 失败分类高保真才下结论，否则升级原 LLM 根因分析
    STAGE_QUALITY: 0.9,  # 候选采纳有成本，宁升级原质量评分也勿误放行
    STAGE_RETRY: 0.9,    # 重试涉及时序副作用，最严要求
    STAGE_RISK: 0.9,     # 风险分级影响回归权重，宁保守升级
    STAGE_TOOL: 0.9,     # 工具风险路由若误判可能放行危险操作，最严
    STAGE_STOP: 0.9,     # 停止条件过早中断或过度循环都有成本，保守
}


class Action(str, Enum):
    """LayaGate 决策动作。"""

    AUTO = "auto"            # 高置信，直接采用 Laya 判定
    ESCALATE = "escalate"    # 低置信/某环节未开 → 升级原 LLM-judge
    # HUMAN 保留：未来高风险环节（如删除/资金类）可在此转人工，本版未启用。


class Status(str, Enum):
    """判定状态：描述"为什么不是 auto"（与 action 正交）。

    对应《让 Agent 学会判断》分层模型：把"证据不足以裁决"与"调用本身故障"
    区分开，避免把任何非 auto 一律当成"模型不知道"——ERROR 是工程故障，
    INCONCLUSIVE 才是真实证据不足。

    - ``CONFIDENT``   高置信，直接采用（action=AUTO）。
    - ``INCONCLUSIVE`` 低置信/证据不足，升级原 LLM 裁决（action=ESCALATE）。
    - ``ERROR``       后端调用/解析失败，安全升级（action=ESCALATE）。
    - ``DISABLED``    环节未开闸 / AH_LAYA=off（action=ESCALATE）。
    """

    CONFIDENT = "confident"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"
    DISABLED = "disabled"


@dataclass
class LayaDecision:
    """一次分层决策输出。"""

    stage: str
    action: Action
    # 判定状态：区分"证据不足"（INCONCLUSIVE）与"调用故障"（ERROR）
    #（见类文档；与 action 正交，action 仍决定是否升级原 LLM 裁决）。
    status: Status = Status.DISABLED
    # 命中的候选答案（choice）或真值概率（noul）；未开闸时为 None
    value: Any = None
    confidence: float = 0.0
    # Laya 原始信号，供上游作参考注记（即使未开闸也有价值）
    signal: dict[str, Any] = field(default_factory=dict)
    # 该环节是否因校准未达标而仅作信号注记（不参与裁决）
    advisory_only: bool = False

    @property
    def auto(self) -> bool:
        return self.action is Action.AUTO


class LayaBackend(Protocol):
    """Laya 判定后端抽象：对一组原子题返回逐题 verdict。

    Hermetic 测试注入 fake 后端；生产用 :class:`SubprocessLayaBackend` 薄封装
    跨 venv 调用 laya-mlx（harness venv 无 mlx 依赖，见关键决策）。
    """

    def grade(self, state: str, questions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        """返回 ``{qid: {value, confidence, probabilities}}``。"""
        ...


class SubprocessLayaBackend:
    """跨 venv 薄封装：subprocess 调 src/laya-mlx/.venv/bin/python 执行判定脚本。

    每次调用一次性批量判定一组原子题（复用 Judger.grade，单进程单前向），
    符合"调用成本≈0"：不逐接口起进程。失败静默升级（绝不破坏 hermetic）。
    """

    def __init__(self, laya_python: str = "", model: str = "aac6fef/laya-multilingual-mlx") -> None:
        self.laya_python = (
            laya_python
            or os.environ.get("AH_LAYA_PYTHON", "")
            or self._default_laya_python()
        )
        self.model = model

    @staticmethod
    def _default_laya_python() -> str:
        """定位 laya-mlx 独立 venv 的解释器（workspace 约定路径）。"""
        here = os.path.dirname(os.path.abspath(__file__))
        # 从 evidence 逐级上溯到 workspace/src（含 github-repos 与 laya-mlx 并列），
        # 命中任一层目录下的 laya-mlx/.venv 即采用（AH_LAYA_PYTHON 可覆盖）。
        d = here
        while True:
            for cand in (
                os.path.join(d, "laya-mlx", ".venv", "bin", "python"),
                os.path.join(d, "src", "laya-mlx", ".venv", "bin", "python"),
            ):
                if os.path.isfile(cand):
                    return cand
            parent = os.path.dirname(d)
            if parent == d:
                return ""
            d = parent

    def _runner_script(self) -> str:
        """生成临时判定脚本（state/questions 以 JSON 入参传递）。"""
        return (
            "import json,sys\n"
            "from laya_mlx.judge import Judger\n"
            "state, questions, model = json.load(sys.stdin)\n"
            "j = Judger(model=model, dtype='float16')\n"
            "judg = j.grade(state, questions)\n"
            "out = {}\n"
            "for qid, v in judg.verdicts.items():\n"
            "    out[qid] = {'value': v.value, 'confidence': v.confidence,\n"
            "                'probabilities': v.probabilities}\n"
            "print(json.dumps(out))\n"
        )

    def grade(self, state: str, questions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        if not self.laya_python or not os.path.isfile(self.laya_python):
            raise RuntimeError(
                "laya-mlx venv 不可用：未找到解释器。设置 AH_LAYA_PYTHON 指向"
                " laya-mlx/.venv/bin/python"
            )
        payload = json.dumps([state, dict(questions), self.model])
        proc = subprocess.run(
            [self.laya_python, "-c", self._runner_script()],
            input=payload,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"laya 判定失败 rc={proc.returncode}: {proc.stderr[-500:]}")
        return json.loads(proc.stdout)



# ── 进程级工厂（真实链路激活） ────────────────────────────────────────────

_PROCESS_GATE: LayaGate | None = None
_PROCESS_GATE_TRIED = False


def laya_enabled() -> bool:
    """AH_LAYA 是否为非 off（即至少有一个环节开闸）。"""
    return bool(LayaGate._env_enabled())


def tool_enforce_enabled() -> bool:
    """工具风险环节是否进入 ENFORCE（阻断）而非 SHADOW（仅注记）。

    ``AH_LAYA_ENFORCE_TOOL=1`` 时，STAGE_TOOL 高置信高风险（value=high）将
    拒绝派发该高风险工具调用（fail-safe：拿不准就不放行），而非仅记录风险
    （文章2 "别让自愈把红灯洗绿" 的隔离/兜底语义）。默认 0 → SHADOW，
    仅写 laya_tool_risk 注记，绝不阻断（字节等价）。
    """
    return os.environ.get("AH_LAYA_ENFORCE_TOOL", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def get_gate() -> LayaGate | None:
    """构造进程级共享 LayaGate（读 ``AH_LAYA`` 环境变量）。

    - ``AH_LAYA=off``（默认）→ 返回 ``None``（原流程字节等价，零网络）。
    - 开闸环节非空 → 惰性构造一个 :class:`SubprocessLayaBackend` 后端，
      模型加载一次、进程内复用（单次判定批量、成本≈0）。
    - 后端解释器缺失（laya-mlx venv 不可用）→ 返回 ``None`` 并告警，
      绝不因 Laya 配置缺失破坏原流程。

    CLI 入口应在进程早期调用一次并注入下游（router / assert_llm / nl2case）。
    """
    global _PROCESS_GATE, _PROCESS_GATE_TRIED
    if _PROCESS_GATE_TRIED:
        return _PROCESS_GATE
    _PROCESS_GATE_TRIED = True
    if not laya_enabled():
        _PROCESS_GATE = None
        return None
    backend = SubprocessLayaBackend()
    if not backend.laya_python or not os.path.isfile(backend.laya_python):
        log.warning(
            "AH_LAYA 已开启但 laya-mlx venv 不可用，LayaGate 保持关闭。"
            "设置 AH_LAYA_PYTHON 指向 laya-mlx/.venv/bin/python"
        )
        _PROCESS_GATE = None
        return None
    _PROCESS_GATE = LayaGate(backend=backend)
    log.info(
        "LayaGate 已激活（AH_LAYA=%s，backend=%s）",
        os.environ.get("AH_LAYA", ""),
        backend.laya_python,
    )
    return _PROCESS_GATE


class LayaGate:
    """每环节 LLM-judge 前的高频决策闸门。

    Args:
        backend: LayaBackend。默认空 → 惰性创建 SubprocessLayaBackend（LLMOFF 下不触发）。
        enabled: 覆盖默认开关（覆盖 ``AH_LAYA`` 环境变量）。None → 由环境变量决定。
        thresholds: 每环节置信阈值覆盖。
    """

    def __init__(
        self,
        backend: LayaBackend | None = None,
        enabled: set[str] | None = None,
        thresholds: Mapping[str, float] | None = None,
    ) -> None:
        self._backend = backend
        self._enabled = set(enabled) if enabled is not None else self._env_enabled()
        # 明确注入 enabled（非 None）时不看环境——用于 Hermetic 测试强制开关。
        self._env_explicit = enabled is not None
        self._thresholds = dict(_DEFAULT_THRESHOLD)
        if thresholds:
            self._thresholds.update(thresholds)

    @staticmethod
    def _env_enabled() -> set[str]:
        raw = (os.environ.get("AH_LAYA", "") or "off").strip().lower()
        if not raw or raw == "off":
            return set()
        if raw == "all":
            return {s for s in _DEFAULT_ENABLED}
        return {s for s in ALL_STAGES if s in {p.strip() for p in raw.replace(",", ";").split(";")}}

    def stage_enabled(self, stage: str) -> bool:
        """该环节是否开闸（未开 → 恒 ESCALATE，字节等价）。"""
        return stage in self._enabled

    def threshold(self, stage: str) -> float:
        return self._thresholds.get(stage, 0.9)

    def _backend_for(self) -> LayaBackend:
        if self._backend is None:
            self._backend = SubprocessLayaBackend()
        return self._backend

    def decide(
        self,
        stage: str,
        state: str,
        questions: Mapping[str, Mapping[str, Any]],
    ) -> LayaDecision:
        """对某一环节的原子题组判决。

        - 环节未开闸 / AH_LAYA=off → ESCALATE（原流程字节等价）。
        - 开闸且后端失败 → 安全升级 ESCALATE（绝不因 Laya 异常改变原流程）。
        - 开闸且高置信（每条 ≥ 阈值）→ AUTO，携带 value/confidence。
        """
        # 未开闸 / AH_LAYA=off：DISABLED，也只带空 signal 避免触发 subprocess。
        if not self.stage_enabled(stage):
            return LayaDecision(
                stage=stage, action=Action.ESCALATE,
                status=Status.DISABLED, advisory_only=True,
            )

        try:
            raw = self._backend_for().grade(state, dict(questions))
        except Exception as exc:  # Laya 故障绝不破坏 hermetic/原流程
            log.warning("laya_gate[%s] backend 失败，安全升级: %s", stage, exc)
            return LayaDecision(
                stage=stage, action=Action.ESCALATE,
                status=Status.ERROR, signal={"_error": str(exc)},
            )

        thr = self.threshold(stage)
        # 全部原子题都要达阈值才算高置信（保守：任一子题低置信都升级）。
        confs = {qid: float(v.get("confidence", 0.0)) for qid, v in raw.items()}
        high_conf = bool(confs) and all(c >= thr for c in confs.values())
        if high_conf:
            return LayaDecision(
                stage=stage,
                action=Action.AUTO,
                status=Status.CONFIDENT,
                value={qid: v.get("value") for qid, v in raw.items()},
                confidence=min(confs.values()),
                signal=raw,
            )
        # 低置信：证据不足（INCONCLUSIVE）→ 升级 LLM，附带信号作参考注记。
        return LayaDecision(
            stage=stage,
            action=Action.ESCALATE,
            status=Status.INCONCLUSIVE,
            confidence=min(confs.values()) if confs else 0.0,
            signal=raw,
        )

    def best(
        self,
        stage: str,
        state: str,
        questions: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """便捷入口：返回可序列化 dict（供埋点记录 + 信号注记）。"""
        d = self.decide(stage, state, questions)
        return {
            "stage": d.stage,
            "action": d.action.value,
            "status": d.status.value,
            "value": d.value,
            "confidence": d.confidence,
            "advisory_only": d.advisory_only,
            "signal": d.signal,
        }


__all__ = [
    "get_gate",
    "tool_enforce_enabled",
    "Status",
    "LayaDecision",
    "LayaGate",
    "LayaBackend",
    "SubprocessLayaBackend",
    "STAGE_INTENT",
    "STAGE_DESIGN",
    "STAGE_ASSERT",
    "STAGE_DATA",
    "STAGE_REPORT",
    "STAGE_HEAL",
    "STAGE_TRIAGE",
    "STAGE_QUALITY",
    "STAGE_RETRY",
    "STAGE_RISK",
    "STAGE_TOOL",
    "STAGE_STOP",
]
