# demo — 框架演示项目（参考模板）

框架自带的演示业务项目，展示「一个业务项目一个自包含目录」的标准结构，也是新增
业务用例的参考模板。**不指向真实被测服务**，全部演示用例通过桩传输隔离驱动。

想先看 AI 怎么生成、执行、诊断和补测，直接运行 [AI 接口测试闭环示例](ai-loop.md)。
想分别跑订单编排、OpenAPI/HAR 建链、失败治理和报告看板，参见 [框架能力实操](capability-examples.md)。

```
projects/demo/
├─ data/           数据集
│  ├─ users_single.yaml    单接口：GET /users/{id}，含 `${var}` 插值与 auth
│  ├─ order_flow.yaml      场景流程：登录 → 下单，user_token 向下游传递，on_failure: stop
│  └─ categorized.yaml     分类元数据：env/priority/suite/tags，供 select_cases 过滤
├─ conftest.py     pytest 配置：报告目录兜底指向本项目 report/
├─ test_demo.py    执行入口：桩传输演示单接口 / 流程 / 分类过滤三种形态
├─ report/         报告输出目录（AH_REPORT_DIR 兜底）
└─ README.md
```

## 运行

```bash
# 本项目自包含运行（桩传输，不连网）
LLMOFF=1 .venv/bin/python -m pytest -q projects/demo -p no:cacheprovider
```

框架层接线演示（`tests/test_demo_single.py` / `tests/test_demo_flow.py`）也复用
同一份 `data/` 数据，验证 `run_single` / `run_flow` 的完整接线。

## 新增业务项目

复制本项目结构：`data/` + `test_<业务>.py` + `conftest.py` + `report/` + `README`。
- 真实巡检项目：在 conftest 注册 `--run-live`，用例连专用测试环境。
- 纯演示/桩项目：保持与 demo 一致，桩传输隔离驱动。
