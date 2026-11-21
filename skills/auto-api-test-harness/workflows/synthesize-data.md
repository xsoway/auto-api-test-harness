# 请求体生成 / 造数（AgentDataGen）

**进入时机**：用户说"帮我生成一个请求体 / 造数 / 按 schema 造样例数据"——对应 `generate_data`。

## 前置知识

- `references/llm.md`（AgentDataGen）
- `references/dispatch.md`（generate_data 分支依赖）
- `references/cases.md`（`case.llm_gen`）

## 步骤

1. **确认 schema**：你要生成的请求体对应哪个 JSON schema / 用例的 `llm_gen` 配置。未提供时用 `{}`（隔离下也返回 `{}`）。

2. **调用 AgentDataGen**：
   ```python
   from ah.llm.agents import AgentDataGen
   gen = AgentDataGen(llm=None)                       # hermetic
   body = gen.generate_request_body({"type": "object", "properties": {"name": {"type": "string"}}})
   # LLM 关闭 -> {}
   ```
   走 dispatch 也行：
   ```python
   from ah.core.dispatch import get_router
   res = get_router().dispatch(
       "生成请求体", use_llm=False,
       case_schema={"type": "object"}, example={"a": 1},
   )
   body = res.output          # hermetic {}
   ```

3. **真实 LLM 模式**（需要 `workflows/add-llm.md` 先接好）：
   - `AgentDataGen(llm=client).generate_request_body(schema, example)` 返回 LLM 按 schema 生成的具体 JSON 实例（`judge("data-gen", ..., model="cheap")`）。

4. **落地到用例**：把生成的 body 写回 `case.request.json` 或作为 `llm_gen` 的参考样例，然后走 `workflows/add-case.md` 跑起来。

## 验收

- 隔离下返回 `{}`（确定性，无网络）。
- 开了 LLM 时返回可被 `json.loads` / 直接用作请求体的 dict，字段与 schema 一致。
- 生成的 body 丢进用例后用例能按预期断言。

## 关键约束

- **隔离默认**：`llm=None` 就是返回 `{}`——别误以为它能凭空"造数"。
- `llm_gen` 用例在 `run_single` 里仅当配置了 `hooks.data_gen` 才实际生成；否则忽略。