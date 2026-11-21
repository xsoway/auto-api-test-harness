# 接入新被测服务

**进入时机**：用户说"接入另一个服务 / 换 base_url / 加令牌登录"。

## 前置知识

- `references/config.md`（`AH_BASE_URL`、`Settings`、令牌）
- `references/cases.md`（`auth` 字段）

## 步骤

1. **设 base_url**（三选一，优先级：环境变量 > config.yaml）：
   - `.env`：`AH_BASE_URL=https://new-service.example/api`
   - 环境：`export AH_BASE_URL=...`
   - 改 `config/config.yaml` 的 `settings.base_url`
   代码侧统一读 `settings.base_url`（`from ah.core.config import settings`）。

2. **令牌 / 认证**（若服务需要）：
   - `Case.auth` 填令牌名（如 `default_token`），由令牌管理器解析（`references/config.md`）。
   - username/password/密钥只放 `.env`（`AH_TOKEN_STORE` 指明令牌缓存路径）。
   - 需要自定义认证流程 → 在 `ah.environment.token` 扩展，别在用例里硬编码凭证。

3. **写针对该服务的用例**：走 `workflows/add-case.md`（单接口）或 `add-flow.md`（场景）。

4. **验证**：服务可达时
   ```bash
   AH_CASES=projects/<业务> LLMOFF=1 .venv/bin/python -m pytest -q -p no:cacheprovider
   ```
   确认新服务的用例按预期通过/失败；连不上时能明确区分"连接失败"与"断言失败"。

## 验收

- `settings.base_url` 指向新服务，用例打的是新地址。
- 认证通过 `auth`/令牌管理器注入，无明文凭证入仓库。
- 报告能区分不同服务的用例（用不同的 `name` 前缀或报告分开）。

## 关键约束

- **别硬编码 base_url / 密钥进 YAML**——都走配置与环境变量。
- `pytest` 的 `ah_client` fixture 用 `settings.base_url` 构建，改配置后重启进程生效。