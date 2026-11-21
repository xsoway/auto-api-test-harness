# 框架能力实操

四个例子可从仓库根目录独立体验。业务接口都使用合成素材或 HTTP 桩，模型诊断和修复建议使用回放。

## 1. 场景编排与复用

把 `login` 和 `create-order` 两个 API Skill 组装成「登录 → 下单 → 查询订单」。流程通过 `with` 传入商品 ID，通过 `assign` 把 token 与订单号上提给后续步骤。另跑一条设置 `on_failure: stop` 的 YAML 场景，观察登录失败时的行为。[组合流程](flows/order-create.yaml) · [失败中止场景](data/order_flow.yaml)

```bash
LLMOFF=1 AH_LOCAL_OBS=0 AH_TRACE_ENABLED=0 uv run --locked \
  python -m projects.demo.examples.runtime flows
```

- 成功时，三个请求依次是 `POST /login`、`POST /orders`、`GET /orders/100`。桩校验下单和查询都带有登录返回的 `Bearer demo-token`，以及下单请求中的商品 ID。
- 登录返回 HTTP 401 时，场景结果把下单标记为 `skipped`，尝试次数和实际下单请求数均为 0。这个中止行为由场景 YAML 的 `on_failure: stop` 提供。

## 2. 从 OpenAPI 和 HAR 接入新项目

场景先查询推荐商品，再用响应中的 `sku` 创建订单。OpenAPI 定义两个接口和响应契约，HAR 记录一份合成会话。[OpenAPI](examples/store.openapi.yaml) · [HAR](examples/store.har)

```bash
lab_dir="$(mktemp -d -t ah-onboarding)"
mkdir "$lab_dir/data"
LLMOFF=1 uv run --locked ah openapi "$lab_dir" \
  projects/demo/examples/store.openapi.yaml --name endpoints
LLMOFF=1 uv run --locked ah scenario discover \
  --openapi projects/demo/examples/store.openapi.yaml \
  --har projects/demo/examples/store.har \
  --output-dir "$lab_dir/draft" --name featured-order
cat "$lab_dir/draft/relationships.json"

# 草案仍有一条 proposed 关系，预期退出码 1，诊断为 unconfirmed。
status=0
LLMOFF=1 uv run --locked ah scenario validate "$lab_dir/draft/relationships.json" || status=$?
test "$status" -eq 1
```

`data/endpoints.yaml` 中有两条请求骨架。导入器给骨架填默认 HTTP 200，建单的 HTTP 201 来自 OpenAPI 契约和会话记录。审核时确认 `GET /featured-product` 的响应 `/sku` 是 `POST /orders` 请求 `/sku` 的来源，并根据契约补上建单状态 `CREATED`。以下代码只确认这个已知合成素材中的一条关系，保留原始草案供对照。

```bash
uv run --locked python - "$lab_dir" <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
draft = json.loads((root / "draft/relationships.json").read_text())
edge, = draft["edges"]
first, second = draft["steps"]
assert first["operation"] == "default GET /featured-product"
assert second["operation"] == "default POST /orders"
assert edge["status"] == "proposed"
assert (edge["source"]["step"], edge["source"]["pointer"]) == (first["id"], "/sku")
assert (edge["target"]["step"], edge["target"]["pointer"]) == (second["id"], "/sku")
edge["status"] = "confirmed"
second["assertions"]["jsonpath"] = {"state": {"eq": "CREATED"}}
(root / "reviewed.json").write_text(json.dumps(draft, indent=2), encoding="utf-8")
PY

LLMOFF=1 uv run --locked ah scenario validate "$lab_dir/reviewed.json"
LLMOFF=1 uv run --locked ah scenario compile "$lab_dir/reviewed.json" \
  --output "$lab_dir/data/checkout.yaml"
cat "$lab_dir/data/checkout.yaml"
```

编译产物为 `schema_version: 2`，订单步骤声明 `depends_on: [step-1]`，请求里的 `sku` 引用上一步动态提取的值。此处已验证场景定义能编译，编译输出的 `business_verified` 仍为 `false`。下一步执行场景才能判定业务断言。

## 3. 执行策略与失败治理

```bash
LLMOFF=1 AH_LOCAL_OBS=0 AH_TRACE_ENABLED=0 uv run --locked --extra prop \
  python -m projects.demo.examples.runtime policies
```

| 小实验 | 运行时观察 |
|---|---|
| 超时 | 用例设置 `timeout=0.05` 秒。桩检查 HTTPX 收到这一读超时配置，再抛出模拟 `ReadTimeout`，框架记录失败。 |
| 软断言 | HTTP 200 通过，返回数量断言失败。执行继续，记录一条软断言失败，最终验真仍为 `failed`。 |
| 属性探索 | 用正整数 `limit` 契约探索 8 个输入。`limit=0` 得到 400。连续两次使用 `seed=42`，样本顺序和判定一致。 |
| 阻断 `block` | 返回数量 9，业务要求 10。回放诊断分类为产品缺陷，保留原失败，未执行脚本自愈。 |
| 观察恢复 `observe` | 连续两次 503 后进入观察恢复。第三次原样请求得到 200，记录 `retry-original` 和 `wrote_script=false`。 |
| 脚本自愈 `heal` | 脚本错误传入 `limit=0`。回放提出改为 10，框架重跑候选并记录 `validated_by=re-run`。原用例和断言仍保留。 |

观察恢复成功和修复候选重跑通过都有各自的证据。在这个版本中，原用例的失败断言仍使这两个结果的 `verdict=failed`，可以同时查看恢复记录和验真结论。[运行代码](examples/runtime.py)

## 4. 从回归报告下钻到看板

先生成新的分页闭环报告，再将两轮结果按不同批次摄入同一个本地 SQLite。模型明细使用一条单独标识的 seed 运行。

```bash
obs_work="$(mktemp -d -t ah-observe-demo)"
uv run --locked --extra dev python -m projects.demo.ai_loop --output-dir "$obs_work/runs"
report_dir="$(find "$obs_work/runs" -type d -name report)"
mkdir "$obs_work/baseline" "$obs_work/regression"
cp "$report_dir/baseline.json" "$obs_work/baseline/"
cp "$report_dir/regression.json" "$obs_work/regression/"

LLMOFF=1 uv run --locked ah observe --db "$obs_work/demo.sqlite3" \
  ingest "$obs_work/baseline" --name pagination-baseline
LLMOFF=1 uv run --locked ah observe --db "$obs_work/demo.sqlite3" \
  ingest "$obs_work/regression" --name pagination-regression
LLMOFF=1 uv run --locked ah observe --db "$obs_work/demo.sqlite3" seed --runs 1
```

先打开终端输出目录里的 `baseline.html`、`regression.html` 和各自的 `*-receipt.json`。基线执行 2 条，1 条失败。修复后执行 3 条，6 条断言通过。上一层目录的 `_evolution_log.jsonl` 记录了采纳 `products-limit-one`，以及拒绝 `products-status-only` 的原因 `quality_below_0.9`。

如果还没有 `web/dist`，在仓库根目录执行 `npm --prefix web ci && npm --prefix web run build`。随后启动看板：

```bash
LLMOFF=1 uv run --locked ah observe --db "$obs_work/demo.sqlite3" \
  serve --host 127.0.0.1 --port 8111 --web web/dist
```

打开 [本地 Traces](http://127.0.0.1:8111/#/traces)：

1. 点 `baseline`，展开 `products-limit-ten` 行，可以看到请求、HTTP 200 响应、失败断言期望 10 和实际 9。Observations 中有从报告导入的用例 SPAN。
2. 点 `regression`，核对三条通过记录，确认新增的 `products-limit-one` 实际参与回归。
3. 点 `seed 运行 1`，展开 Observations 里的 `GENERATION` 行，看模拟模型调用的名称、prompt、输出、token 与成本。这条运行的来源列为 `seed`。

完成后在运行看板的终端按 Ctrl-C 停止服务。
