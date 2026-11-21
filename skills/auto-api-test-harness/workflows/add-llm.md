# 接入真实 LLM 辅助

**进入时机**：用户说"接入 OpenAI/OpenRouter / 开真实 LLM / 让 agent 更聪明"。

## 前置知识

- `references/llm.md`（`llm_enabled`、4 个 agent、`LLMHooks`）
- `references/config.md`（环境变量）
- `references/run.md`（hooks 接入编排）

## 关键事实

`LLMOFF=1` 是隔离模式（默认）。真实 LLM 需要**同时满足**：未设 `LLMOFF` 且存在 `OPENROUTER_API_KEY`。其中任一条不满足 → `llm_enabled()` 为 False → `LLMClient()` 抛 `LLMDisabledError`（不是软降级）。

## 步骤

1. **配置密钥与模型**（`.env`，参考 `.env.example`）：
   ```dotenv
   OPENROUTER_API_KEY=sk-...            # 你填
   # OPENROUTER_BASE_URL=https://openrouter.ai/api/v1/
   AH_LLM_MODEL_CHEAP=openai/gpt-4o-mini
   AH_LLM_MODEL_STRONG=openai/gpt-4o
   # 注意：注释掉 / 移除 LLMOFF=1（否则仍隔离）
   ```
   > 只把密钥放 `.env`，绝不写进代码或 YAML。`${ENV_VAR}` 引用可留作占位。

2. **确认开关生效**：
   ```python
   from ah.llm import llm_enabled
   assert llm_enabled() is True          # 否则退回隔离
   ```

3. **构造 LLM 并注入 helper agent / hooks**：
   ```python
   from ah.llm import LLMClient
   from ah.core.orchestrator import LLMHooks, run_single
   from ah.llm.agents import AgentDataGen, AgentTriage, AgentHeal

   llm = LLMClient(...)                  # llm_enabled() 必须为 True
   hooks = LLMHooks(
       data_gen=lambda case, ctx: AgentDataGen(llm=llm).generate_request_body(case.llm_gen),
       triage=lambda case, resp, err: AgentTriage(llm=llm).explain_failure(case, resp, err),
       heal=lambda case, resp, err, attempt: AgentHeal(client, ctx, llm=llm).heal(case, resp, err, attempt),
   )
   result = run_single(case, ctx, client, hooks=hooks)
   ```

4. **dispatch 走 LLM 兜底意图**（可选）：
   ```python
   from ah.core.dispatch import IntentRouter
   router = IntentRouter(llm=llm)
   intent = router.route("帮我把这个请求搞出来", use_llm=True)  # 规则未命中才调 LLM
   ```

5. **验证**：重启进程让环境变量生效；跑一条真实 LLM 路径，确认 `llm_notes` 里出现 `data_gen` / `triage` / `heal` 注记而非隔离固定值。

## 验收

- `llm_enabled()` 为 True，`LLMClient` 不再抛 `LLMDisabledError`。
- hooks 跑通，`llm_notes` 体现真实 LLM 输出。
- 密钥只在 `.env`，不在代码/YAML/报告里明文泄露。

## 关键约束

- 还原默认时：设回 `LLMOFF=1` 或清掉 key，保证隔离兜底。
- **hook 异常只告警，不破坏测试**：`run_single` 吞掉 hook 异常并记 `llm_notes`。
- 别为一个"看起来更智能"擅自默认开 LLM——隔离是默认、是 CI 基线。