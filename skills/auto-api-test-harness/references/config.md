# 配置：settings + .env + 令牌管理

配置由 `ah.core.config`（`Settings` / `load_settings` / `settings`）承载，`config/config.yaml` 是默认文件，`.env` / 环境变量可覆盖（环境变量优先）。

## config/config.yaml

```yaml
settings:
  base_url: http://127.0.0.1:8000
  token_store_path: config/tokens.json
  log_dir: artifacts/logs
  report_dir: artifacts/reports
  trace_enabled: false

# LLM 辅助路由（默认注释禁用）。仅当未设 LLMOFF 且存在 api_key 时才真实调用。
# llm:
#   provider_base_url: https://openrouter.ai/api/v1/
#   api_key: <OPENROUTER_API_KEY>
#   model_cheap: openai/gpt-4o-mini
#   model_strong: openai/gpt-4o
#   timeout: 120.0
```

## .env 环境变量（见 `.env.example`）

| 变量 | 作用 | 默认 |
|---|---|---|
| `LLMOFF=1` | 强制整套测试零网络（CI 默认）。全部 agent 回退确定性固定值 | 空 |
| `OPENROUTER_API_KEY` | 真实 LLM 的密钥；留空则禁用 LLM | 空 |
| `OPENROUTER_BASE_URL` | 覆盖 OpenRouter 聊天端点 | https://openrouter.ai/api/v1/ |
| `AH_LLM_MODEL_CHEAP` | 轻量模型（数据生成） | openai/gpt-4o-mini |
| `AH_LLM_MODEL_STRONG` | 强模型（诊断/自愈/报告） | openai/gpt-4o |
| `AH_BASE_URL` | 被测服务 base_url | http://127.0.0.1:8000 |
| `AH_TOKEN_STORE` | 令牌存储路径 | config/tokens.json |
| `AH_TRACE_ENABLED=1` | 开启 Langfuse 追踪 | 0 |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | 追踪后端凭证 | — |
| `AH_CASES` | 用例目录/glob（传给 discover_case_files） | — |
| `AH_PROFILE` | 多环境 profile 名（prod/dev/…），覆盖 base_url/domains | 空 |

```python
from ah.core.config import settings, load_settings
settings.base_url                      # 当前生效值
load_settings()                        # 重新从 config.yaml + env 加载
```

## 多环境 profile

`config.yaml` 的 `profiles:` 段按环境切换 `base_url` / `domains`（只覆盖列出的键）：

```yaml
profiles:
  prod:
    base_url: https://gateway.example.test
    domains:
      orders: { base_url: https://orders.example.test, auth: orders_token }
```

选择 profile 三种方式（优先级：显式参数 > `AH_PROFILE` env > 无）：

```bash
AH_PROFILE=prod .venv/bin/python -m ah.scheduler scheduled-run <project>
.venv/bin/python -m ah.scheduler scheduled-run <project> --profile prod
```

`load_settings(config_path=None, profile=None)`：显式 `profile` 参数优先，其次读
`AH_PROFILE`；命中后用 profile 的 `base_url` 覆盖顶部、`domains` 按键合并。CLI
`--profile` 运行时通过 `activate_profile(name)` 原位更新模块级 `settings`。
```

## 令牌管理

`ah.environment.token` 负责 auth 令牌生命周期。`Case.auth` 传入令牌名（如 `default_token`），由令牌管理器解析后作为请求 auth。

- username/password 等凭证只放在 `.env` / 环境变量，**绝不硬编码进 YAML 或代码**。
- `auth: default_token` 是单用例示例（`projects/demo/users_single.yaml`）。

## pytest 插件选项

`src/ah/core/plugin.py`（入口点 `ah = "ah.core.plugin"`）：

| 选项 | 作用 |
|---|---|
| `--ah-data-dir` | 用例 YAML 目录（或 AH_CASES glob） |
| `--ah-report-dir` | report.md / report.json 输出目录（默认 settings.report_dir） |

- `pytest_sessionfinish` 自动 `build_report` 写报告；报告失败只告警、绝不中断运行。
- fixtures：`ctx`（每次全新 `Context`）、`ah_client`（`HTTPClient(settings.base_url, trace=get_tracer())`）、`ah_hooks`（默认 `LLMHooks()` 全为 None = 隔离）。

## 关键约束

- **LLMOFF=1 是既定默认**，不是降级降级。真实 LLM 需要显式配置 + 密钥。
- 改配置后重跑全套件验证，别在配置层面引入网络依赖。