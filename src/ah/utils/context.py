"""变量上下文、插值与线程局部隔离。

:class:`Context` 是用于在上下游调用之间传递变量的有序 ``str -> any`` 映射。
:class:`ContextManager` 维护一条线程局部上下文链，使每个 pytest 测试线程拥有
自己的变量（全局/并行安全）。

插值会替换模板中的 ``${name}`` 引用（正则 ``\\$\\{([\\w.]+)\\}``）为
``str(ctx.get(name))``。仅支持扁平名称——不支持诸如
``${resp.ok}`` 的嵌套路径。缺失变量会抛出 :class:`KeyError`
并指明缺失的键（供后续 LLM 三分类的干净输入）。
"""

from __future__ import annotations

import random
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_VAR_RE = re.compile(r"\$\{([\w.]+)\}")
# 生成占位符：``${rand:uuid}`` / ``${date:now}`` 等（含冒号的非常量模板）。
_PLACEHOLDER_RE = re.compile(r"\$\{([a-z][a-z0-9_]*):([^}]*)\}")
_RAND_INT_RE = re.compile(r"^int:(-?\d+)-(-?\d+)$")
_RAND_FLOAT_RE = re.compile(r"^float:(-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)$")
# 日期偏移：``+1d`` / ``-2h`` / ``+30m``（d=天 h=小时 m=分钟）。
_DATE_OFFSET_RE = re.compile(r"^([+-])(\d+)([dhm])$")


def _resolve_single_placeholder(name: str, arg: str) -> str:
    """解析单个 ``name:arg`` 占位符为确定性可生成的字符串。

    raise ValueError 表示不识别该占位符（调用方保持原样/报错）。
    """
    if name == "rand":
        if arg == "uuid":
            return str(uuid.uuid4())
        m = _RAND_INT_RE.match(arg)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            return str(random.randint(lo, hi))
        m = _RAND_FLOAT_RE.match(arg)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            return f"{random.uniform(lo, hi):.3f}"
        raise ValueError(f"rand:{arg}")
    if name == "date":
        now = datetime.now(timezone.utc)
        if arg == "now":
            return now.strftime("%Y-%m-%dT%H:%M:%SZ")
        if arg.startswith("now:"):
            # ``now:fmt``：按自定义 strftime 格式输出当前 UTC 时间。
            return now.strftime(arg.split(":", 1)[1])
        m = _DATE_OFFSET_RE.match(arg)
        if m:
            sign, num, unit = m.group(1), int(m.group(2)), m.group(3)
            delta = timedelta(
                days=num if unit == "d" else 0,
                hours=num if unit == "h" else 0,
                minutes=num if unit == "m" else 0,
            )
            target = now + delta if sign == "+" else now - delta
            return target.strftime("%Y-%m-%dT%H:%M:%SZ")
        raise ValueError(f"date:{arg}")
    raise ValueError(f"{name}:{arg}")


def resolve_placeholders(text: str) -> str:
    """在插值变量之前解析生成占位符（``${rand:...}`` / ``${date:...}``）。

    不识别 / 解析失败的占位符保持原样，交由变量插值或后续报错处理，保证
    既有 ``${var}`` 语义不受影响。生成类占位符每次调用产生新值，天然非确定；
    用例应据此只做格式/非空断言。
    """
    if "${" not in text:
        return text

    def _sub(m: "re.Match[str]") -> str:
        try:
            return _resolve_single_placeholder(m.group(1), m.group(2))
        except ValueError:
            return m.group(0)  # 保底：不识别则原样保留。

    return _PLACEHOLDER_RE.sub(_sub, text)



class Context:
    """支撑 set/get/has 语义的有序变量存储。"""

    def __init__(self, data: Optional[dict[str, Any]] = None) -> None:
        self._data: dict[str, Any] = dict(data or {})

    def set(self, name: str, value: Any) -> None:
        self._data[name] = value

    def get(self, name: str, default: Any = None) -> Any:
        return self._data.get(name, default)

    def has(self, name: str) -> bool:
        return name in self._data

    def copy(self) -> "Context":
        return Context(self._data.copy())

    def as_dict(self) -> dict[str, Any]:
        return self._data.copy()

    def __repr__(self) -> str:
        return f"<Context {list(self._data.keys())}>"


class ContextManager:
    """线程局部上下文链。每个线程维护自己的栈顶 Context。"""

    def __init__(self) -> None:
        self._local = threading.local()

    def _stack(self) -> list[Context]:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = [Context()]
            self._local.stack = stack
        return stack

    def current(self) -> Context:
        return self._stack()[-1]

    def push(self, ctx: Optional[Context] = None) -> Context:
        """将一个全新的（或给定的）上下文压入该线程的栈；返回它。"""
        self._stack().append(ctx if ctx is not None else Context())
        return self.current()

    def pop(self) -> Context:
        stack = self._stack()
        if len(stack) == 1:
            raise RuntimeError("cannot pop the root context")
        return stack.pop()

    def set(self, name: str, value: Any) -> None:
        self.current().set(name, value)

    def get(self, name: str, default: Any = None) -> Any:
        # 从栈底向下搜索（最内层优先），使嵌套流程能继承变量。
        for ctx in reversed(self._stack()):
            if ctx.has(name):
                return ctx.get(name, default)
        return default


context_manager = ContextManager()


def render(template: str, ctx: Context) -> str:
    """将 ``${name}`` 引用替换为 ``str(ctx.get(name))``。

    先生成占位符（``${rand:...}`` / ``${date:...}``，见 :func:`resolve_placeholders`），
    再插值上下文变量。抛出指明第一个缺失变量的 :class:`KeyError`。
    """
    if "${" not in template:
        return template
    # 先解析生成占位符（rand/date），避免被当作缺失变量报错。
    template = resolve_placeholders(template)

    def _sub(m: "re.Match[str]") -> str:
        name = m.group(1)
        if not ctx.has(name):
            raise KeyError(f"missing context variable: {name!r}")
        return str(ctx.get(name))

    return _VAR_RE.sub(_sub, template)


def render_value(value: Any, ctx: Context, *, typed: bool = False) -> Any:
    """递归渲染；typed 模式的整值引用保留 JSON 类型，插值仍返回字符串。"""
    if isinstance(value, dict):
        return {key: render_value(item, ctx, typed=typed) for key, item in value.items()}
    if isinstance(value, list):
        return [render_value(item, ctx, typed=typed) for item in value]
    if not isinstance(value, str):
        return value
    match = _VAR_RE.fullmatch(value) if typed else None
    if match:
        name = match.group(1)
        if not ctx.has(name):
            raise KeyError(f"missing context variable: {name!r}")
        return ctx.get(name)
    return render(value, ctx)
