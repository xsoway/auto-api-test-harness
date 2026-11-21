# 报告洞察（AgentReport）

**进入时机**：用户说"分析一下运行报告 / 总结测试结果 / 洞察失败模式"——对应 `analyze_report`。

## 前置知识

- `references/reports.md`（build_report / AgentReport 产物结构）

## 步骤

1. **拿到结果**：运行后各步骤的 `StepResult` 列表（或从 `ReportWriter` / `build_report` 读磁盘）。
   ```python
   from ah.reporting.writer import ResultRecord, build_report
   records = [ResultRecord.from_step(step) for step in results]
   md_path = build_report(records)              # -> artifacts/reports/report.md（+ json）
   ```

2. **跑 AgentReport 洞察**：
   ```python
   from ah.llm.agents import AgentReport
   summary = AgentReport(llm=None).analyze_results(results)   # hermetic 计数 markdown
   ```
   走 dispatch：
   ```python
   from ah.core.dispatch import get_router
   res = get_router().dispatch("分析报告", use_llm=False, results=results)
   if res.skipped:
       print(res.reason)          # 缺 results
   else:
       print(res.output)          # markdown 摘要
   ```

3. **给用户洞察小结**（回答"赢没赢 / 差在哪 / 下一步"）：
   - `**N/M passed** (P%)` 整体健康度。
   - `Top failure causes`：按首行错误去重降序——主要失败模式。
   - `Per-case notes`：逐用例 `ok` / `FAILED: <err>` / `ok with llm: <notes>`。
   - 下一步建议：改断言？补 extract？服务问题？接入 LLM 深化注记？

4. **失败项**转 `workflows/diagnose.md` 逐个追因。

## 验收

- 报告 markdown 含 `cases: **N/M passed**`、`Top failure causes`、`Per-case notes`。
- 你给用户的洞察基于报告实数据，不含编造数字。

## 关键约束

- 隔离下报告只有计数，无 LLM 注记——汇报时区分"确定性统计"与"LLM 洞见"。
- `AgentReport.analyze_results` 接收的是 `StepResult`（非 `ResultRecord`）。