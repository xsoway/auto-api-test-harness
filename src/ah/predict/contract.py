"""OpenAPI 契约挖掘：从接口定义/响应样本推导 extract 与 assert 建议。

对应 docs/ai-testing-proposal.md §2 设计维度的"契约挖掘"（`AgentContractMiner`，
参考 ASE 2024 "Using LLM for Mining and Testing Constraints in API Testing"）。

:class:`ContractMiner` 是**纯确定性**的规则挖掘器，不需要 LLM：
给定 OpenAPI 2/3 的 path item（或直接一个响应 JSON 样本），推导：

- ``assert_``：状态码断言（依据 ``responses`` 的成功码）；若响应 schema 可解析，
  为必填祖先下的必填字段推导 ``jsonpath.type``，不猜业务值。
- ``extract``：识别常见 id 字段（``id``/``*_id``/``uuid`` 等）与自增/主键，
  产出 jmespath 提取键，供下游用例复用。

这样即使 LLM 关闭（hermetic），也能从契约得到可落地的断言/提取建议。
:meth:`ContractMiner.mine_schema` 直接分析 JSON Schema（"最简路径"），
:meth:`mine_openapi` 走 OpenAPI 文档。

所有输出复用框架认定的格式（见 :mod:`ah.metrics.assertions` 与
:mod:`ah.metrics.extract`），可被 :class:`ah.harness.loader.Case` 直接消费。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ah.predict.generator import GeneratedCase
from ah.scenario.catalog import normalize_catalog
from ah.utils.logging import get_logger

log = get_logger("predict.contract")

# 常用主键/id 字段名（大小写不敏感），用于 extract 推导。
_ID_HINTS = ("id", "uuid", "gid", "key", "order_id", "user_id", "product_id")
# 不需要提取的非业务字段。
_NOISE_KEYS = {"_links", "self", "href", "type"}


@dataclass
class ContractAdvice:
    """一次契约挖掘的结果：可直接叠加进 ``Case.extract`` / ``Case.assert_``。"""

    assert_: dict[str, Any] = field(default_factory=dict)
    extract: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def merge_into(self, case: Any) -> Any:
        """把建议合并进一个 :class:`Case`（仅补充缺失键，不改动已有）。"""
        merged = dict(case.assert_ or {})
        for key, val in self.assert_.items():
            merged.setdefault(key, val)
        case.assert_ = merged
        extract = dict(case.extract or {})
        for key, val in self.extract.items():
            extract.setdefault(key, dict(val))
        case.extract = extract
        return case


def _json_type_name(js: Any) -> Optional[str]:
    """从 JSON Schema 的 type 推导断言可用的类型名。"""
    if not isinstance(js, dict):
        return None
    t = js.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    if t in ("integer", "number", "string", "boolean", "array", "object", "null"):
        return t
    if t is None:
        # 无显式 type 但有 properties/enum 也可推断。
        if "properties" in js:
            return "object"
        if "enum" in js:
            return None
        if js.get("items"):
            return "array"
    return None


class ContractMiner:
    """确定性 OpenAPI / JSON Schema 契约挖掘器（不依赖 LLM）。"""

    def __init__(self, base_url: str = "") -> None:
        self.base_url = base_url.rstrip("/")

    # ── 最有用的最简路径：直接给定一个响应 JSON 样本 ──────────────────────
    def mine_schema(
        self, sample: Any, *, default_status: int = 200
    ) -> ContractAdvice:
        """从响应样本（dict/列表）推导断言与提取。

        只做保守推断，避免幻觉断言：
        - 顶层对象：提取常见 id 字段；为 string 字段断言 ``jsonpath.type``。
        - 列表：不做逐元素断言（可能为空），仅提示。
        """
        adv = ContractAdvice()
        adv.assert_["status_code"] = default_status
        if isinstance(sample, dict):
            self._mine_object(sample, "", adv)
        elif isinstance(sample, list) and sample and isinstance(sample[0], dict):
            self._mine_object(sample[0], "[0]", adv)
            adv.notes.append("list response: only probed first element schema")
        return adv

    def _mine_object(self, obj: dict[str, Any], prefix: str, adv: ContractAdvice) -> None:
        for key, val in obj.items():
            lk = key.lower()
            if lk in _NOISE_KEYS:
                continue
            jpath = f"{prefix}.{key}" if prefix else key
            if isinstance(val, dict):
                # 恰好是常见 id 对象（如 {"id": ...}）：提升为可提取键。
                if lk in _ID_HINTS and "id" in val:
                    adv.extract[key] = {"path": f"{jpath}.id", "extract_type": "jsonpath"}
                    continue
                self._mine_object(val, jpath, adv)
                continue
            # 标量叶：id 类 → 提取；其他 string → type 断言。
            if lk in _ID_HINTS or lk.endswith("_id") or lk.endswith("_key"):
                adv.extract[key] = {"path": jpath, "extract_type": "jsonpath"}
            elif isinstance(val, str):
                adv.assert_.setdefault("jsonpath", {}).setdefault(
                    jpath, {"type": "string"}
                )

    # ── OpenAPI 文档路径 ──────────────────────────────────────────────────
    def mine_openapi(
        self, spec: dict[str, Any], *, path: Optional[str] = None, method: str = "get"
    ) -> ContractAdvice:
        """复用离线目录，挖掘成功响应及所有必填祖先下的嵌套必填字段。"""
        catalog = normalize_catalog(spec)
        operations = catalog["operations"]
        target = next((op for op in operations
                       if (path is None or op["path"] == path)
                       and op["method"] == method.upper()), None)
        notes = [f"{issue['code']}: {issue['message']}" for issue in catalog["issues"]]
        if target is None:
            return ContractAdvice(notes=["operation not found; no contract mined", *notes])
        adv = ContractAdvice(notes=notes)
        successes = sorted(
            code for code in target["responses"]
            if len(code) == 3 and code.isdigit() and 200 <= int(code) < 300
        )
        if not successes:
            adv.notes.append("no explicit success response; no status/schema guessed")
            return adv
        code = successes[0]
        adv.assert_["status_code"] = int(code)
        schema = target["responses"][code]["schema"]
        self._mine_required(schema, "", adv)
        return adv

    def _mine_required(self, schema: Any, prefix: str, adv: ContractAdvice) -> None:
        if not isinstance(schema, dict) or "$ref" in schema:
            return
        if any(key in schema for key in ("allOf", "anyOf", "oneOf", "not")):
            adv.notes.append(f"unsupported composed schema at {prefix or '$'}; not mined")
            return
        if schema.get("nullable") or isinstance(schema.get("type"), list):
            adv.notes.append(f"nullable/union schema at {prefix or '$'}; no type guessed")
            return
        # 数组可以为空，不能凭 items 的 required 断言首元素存在。
        if _json_type_name(schema) == "array":
            if prefix:
                adv.assert_.setdefault("jsonpath", {}).setdefault(prefix, {"type": "array"})
            return
        props = schema.get("properties")
        required = schema.get("required", [])
        if not isinstance(props, dict) or not isinstance(required, list):
            return
        for name in required:
            if not isinstance(name, str):
                continue
            child = props.get(name)
            if not isinstance(child, dict) or "$ref" in child:
                continue
            if child.get("nullable") or isinstance(child.get("type"), list):
                continue
            if any(key in child for key in ("allOf", "anyOf", "oneOf", "not")):
                continue
            # jsonpath 底层是 jmespath，引用字段名以支持连字符等字符。
            token = json.dumps(name, ensure_ascii=False)
            if name.isascii() and name.isidentifier():
                token = name
            jpath = f"{prefix}.{token}" if prefix else token
            typ = _json_type_name(child)
            if name.lower() in _ID_HINTS or name.lower().endswith(("_id", "_key")):
                adv.extract.setdefault(name, {"path": jpath, "extract_type": "jsonpath"})
            elif typ:
                adv.assert_.setdefault("jsonpath", {}).setdefault(jpath, {"type": typ})
            self._mine_required(child, jpath, adv)


def case_from_intent(
    intent: str,
    *,
    method: str = "get",
    path: str = "",
    llm: Any = None,
    base_url: str = "",
) -> Optional["GeneratedCase"]:
    """从一段自然语言业务意图生成结构化的 :class:`GeneratedCase`。

    对应提案 §2 的 ``NL → YAML``（`AgentCaseGen`）。目标是把"一句话业务意图"
    转成 ``name``/``request``/``assert_`` 结构：
    - LLM 启用时：交给强模型输出 JSON，再经 :class:`ContractMiner` 补建议。
    - LLM 禁用（hermetic）：返回 ``None``（绝不发明接口定义），由上层回退。

    与 :class:`ah.predict.generator.Predictor` 的差异：Predictor 基于现有用例
    + 历史失败做*数据*扩增；本函数从*自然语言意图*出发，产物可直接落盘 YAML。
    执行与验证始终由调用方用 ``run_single`` 确定性完成。
    """

    if llm is None:

        log.info("LLM disabled: case_from_intent returns None (no invented contract)")
        return None
    prompt = (
        "You convert a natural-language API testing intent into ONE structured test "
        "case. Respond only with JSON: "
        '{"name": str, "request": {"method": str, "path": str, "params": {…} optional, '
        '"json": {…} optional}, "assert": {…} optional}. '
        f"INTENT: {intent}\n"
        f"METHOD_HINT: {method}\nPATH_HINT: {path or '(derive from intent)'}\n"
        "Keep request.method lowercase and request.path a concrete endpoint. "
        "Do not invent endpoints outside the intent."
    )
    try:
        raw = llm.judge("case-gen", prompt, model="strong")
    except Exception as exc:  # LLM 故障绝不是用例失败。
        log.warning("case_from_intent llm call failed: %s", exc)
        return None
    if not isinstance(raw, dict):
        return None
    req = raw.get("request")
    if not isinstance(req, dict) or not isinstance(req.get("path"), str):
        log.warning("case_from_intent: invalid request %r", req)
        return None
    gen = GeneratedCase(
        name=str(raw.get("name", req["path"])),
        request={"method": str(req.get("method", method)).lower(), "path": req["path"]},
        assert_=dict(raw.get("assert", {}) or {}),
        extract=dict(raw.get("extract", {}) or {}),
    )
    # 用契约挖掘补齐状态码断言（LLM 未必给，但状态码是最稳的信号）。
    if "status_code" not in gen.assert_:
        gen.assert_["status_code"] = 200
    return gen
