# 报告：build_report + AgentReport

报告由 `ah.reporting.writer`（`build_report` / `ReportWriter`）与 `ah.llm.agents.AgentReport` 两层产出。pytest 插件在 `pytest_sessionfinish` 自动落盘。
> **项目自包含执行入口的报告**：业务项目 ``test_<业务>.py`` 用 ``run_single`` /
> ``run_flow`` 直接驱动时**不经插件记录**（``pytest_sessionfinish`` 因
> ``_session_records`` 为空不会自动落盘）。已通过
> ``src/ah/core/selfcontained.py`` 的 :class:`AutoReporter` 补齐：项目 ``conftest.py``
> 一行接入，会话结束自动把本轮用例写成 ``<项目 report/>run.log``（逐用例日志）+
> ``report.md``/``report.json``/``report.html``（报告）。带 step 巡检的项目用
> ``run_reporter.record(step, start=...)``（step 粒度、含真实状态码）；纯护栏 /
> 流程解析项目经 ``pytest_runtest_logreport`` 自动记录每个 test 节点。
>
> **按运行留档不覆盖**：:class:`AutoReporter` 支持 ``report_basename``（如
> ``"test-tcc-m-api"``）——指定后每轮报告写为
> ``{basename}-{YYYYmmdd_HHMMSS_mmm}.{md,json,html}``（时间戳带微秒），
> **每次运行独立文件、互不覆盖**；未指定则仍写固定 ``report.*``。

## build_report（磁盘产物）

```python
def build_report(
    results: list[ResultRecord],
    trace_urls: Optional[dict[str, str]] = None,
    *,
    report_dir: Optional[str] = None,
    history_rows: Optional[list[dict]] = None,   # None → 自动读 report_dir/history.jsonl
    basename: str = "report",                    # 输出文件名主干，默认 report
) -> Path

- ``basename`` 控制输出文件名（不含扩展名）：默认 ``report`` → ``report.md``/``report.json``；传不同值（如 ``test-tcc-m-api-20261008_153000``）写独立文件，避免覆盖。

- 在 `report_dir`（默认 `settings.report_dir`，即 `artifacts/reports`）下写 `report.md` + `report.json`。
- markdown 含：运行时间、`base_url` 环境、`Summary: passed/total passed, failed`、(可选) Langfuse trace urls。
- `ResultRecord`（`from_step(step, duration=0)` / `to_dict` / `from_dict`）是步骤结果的持久化形态。`result` 区分 `PASS` / `FAIL` / `UNVERIFIED` / `BLOCKED` / `SKIP`，同时保留 `status`、`verdict`、`reason`、`attempts`；不得把非 FAIL 一律当 PASS。
- 若 `report_dir/history.jsonl` 存在（或传入 `history_rows`），附「**预测分析 / 高风险接口**」段 + `report.json.prediction`。见 `references/predict.md`。无历史则不出段。

pytest 侧：`--ah-report-dir` 或 `AH_REPORT_DIR` 控制输出目录；报告生成失败只 `log.warning`，绝不中断运行。

新增 `report.json.execution_counts` 保存计划/执行/验证通过/失败/阻塞/跳过/未验证及实际断言计数。`planned` 包括未发请求的步骤，`attempts=0` 表示没有 HTTP 尝试。legacy 无断言的 `succeeded=True` 不等于 `verdict=passed`；机器验收应读取 verdict 与证据，而非仅取旧摘要通过率。此处未实现方案 C 的被测构建/环境绑定完整契约。

## ReportWriter（面向对象封装）

```python
writer = ReportWriter(report_dir=...)   # 默认 settings.report_dir
```

HTML 单文件报告（参考 LLM-wiki agent-trace 报告的绿色渐变设计风格）：

```python
path = writer.write_html(results)                 # 同 write 的结果数据 → report.html
```

- `build_html_report(results, report_dir=...)` 在报告目录写**自包含** `report.html`（head 无外部 `<link>`/`<script>`，完全内联、可离线浏览），返回路径。
- 结构：hero 渐变头部（Test Report + 通过率徽标 + HTML/Markdown/JSON 下载按钮）、sticky 侧边栏（运行信息 metrics + 侧导航）、结果总览表、逐用例 `<details>` 卡片（请求 / 响应 / 自愈三列）、可选「预测分析」表。
- **失败治理 hint-bar**：当本次有 `product_defect` 被 block（`gov_stats.blocked`）或
  observe 无侵入恢复（`gov_stats.observe_recovered`）时，失败分支下方渲染
  「失败治理」提示条，含 block 终止数（等人工、不盲自愈）与 observe 恢复数
  （只重跑原步骤、不回写脚本）。能力矩阵在 md + json 的 `capabilities.total` 现为
  **28** 项（id 24-28 = governor / cluster / observe / knowledge / 调度簇群隔离）。
- 绿色主题：`--forest/--emerald/--jade/--mint`；徽标 pill：`badge pass/fail/skip`；失败/响应/自愈文本经 Jinja `| e` 转义防注入。
- 下载按钮读取内嵌 `#json-data`（application/json）与 `#md-data`（hidden `<pre>`，浏览器解码实体后 `.textContent` 即原始 markdown）；JSON 中 `<` 转义为 `\u003c` 防破坏 script 闭合。
- 「自愈」文字（侧导航 / 卡片列 / 详情折叠名）仅在存在自愈或请求/响应详情时渲染——纯 `ResultRecord(name, result)` 且无 request/response/heal 的占位记录不出现「自愈」字样。


## AgentReport（洞察分析）

```python
class AgentReport:
    def __init__(self, llm: Any = None) -> None
    def analyze_results(self, results: list[StepResult]) -> str
```

返回 markdown 摘要，结构：
- `## Run analysis`：`- cases: **N/M passed** (NN.N%)`
- `### Top failure causes`：按首行错误去重统计，降序——定位主要失败模式。
- `### Per-case notes`：逐用例 `ok` / `FAILED: <error>` / `ok with llm: <llm_notes>`。

隔离模式：纯计数固定输出，无 LLM 注记。真实 LLM 时补上诊断性注记。

```python
from ah.llm.agents import AgentReport
summary = AgentReport(llm=None).analyze_results(results)   # LLM 关闭也工作
```

## 如何解读产物

1. 先看 `Summary` 的通过率与总数，判断整体健康度。
2. 读报告后走 `workflows/analyze-report.md` 给用户洞察小结。
3. 失败用例进入 `workflows/diagnose.md`。
4. 有「预测分析」段时，`workflows/analyze-report.md` 里额外给用户高风险接口 + 回归建议（来源 `references/predict.md`）。

## 关键约束

- **报告失败不能中断测试运行**——任何报告写盘异常只告警。
- 读 `report.md` 后再下结论，别凭印象汇报通过率。