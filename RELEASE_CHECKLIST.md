# RELEASE CHECKLIST — auto-api-test-harness

> 发布前检查清单，按 `oss-release-prep` skill 的 A–K 节核对。
> 每一项都记录**验证证据**（命令输出 / 文件存在 / git 状态），缺证据视为未通过。
> ⚠️ 敏感信息红线：禁止任何 API key、token、私钥、个人数据、绝对本机路径进入开源产物。

| 节 | 项目 | 状态 | 证据 |
|----|------|------|------|
| **A** | 版本库状态 | ✅ 通过 | `git init -b main` + 首次提交 `473ca7c`（201 文件，43120 行）；`git status` clean；已提交 `.gitattributes` |
| **B** | 必备文件 | ✅ 通过 | `README.md`(EN) / `README.zh-CN.md`(ZH) / `LICENSE`(MIT, Copyright (c) 2026 Alan_Hsu) / `.gitignore` / `.gitattributes` 全部存在 |
| **C** | README 质量 | ✅ 通过 | 中英双语双文件、互指切换链接；含 Maintainer / Contributing / Roadmap / FAQ / 架构图 / 目录树释义；快速开始命令实测可跑 |
| **D** | 敏感信息扫描 | ✅ 通过 | 201 个已跟踪文件全量正则扫描：零绝对本机路径（`/Users/`、`/home/`）；5 处 `authorization` 均为标准 HTTP 头名，非凭据 |
| **E** | 可验证性 | ✅ 通过 | `LLMOFF=1 ... pytest -q tests projects/demo` → **698 passed** (11.18s)；`uv build` → `dist/auto_api_test_harness-0.1.0.tar.gz` + `-py3-none-any.whl` |
| **F** | 发布动作授权 | ✅ 已执行 | 用户授权（xsoway，public）。`gh repo create auto-api-test-harness --public --source . --remote origin --push` → `https://github.com/xsoway/auto-api-test-harness`（PUBLIC），远端 `main`=`a26b373`，工作树 clean |
| **G** | 可发现性 | ✅ 已执行 | `gh repo edit` 已加 6 topics（api-testing/pytest/yaml/test-automation/llm/openapi）+ 描述；`gh repo view --json repositoryTopics` 已验证 |
| **H** | 项目主页 | ✅ 已上线 | `index.html`（gruvbox 黑金）已生成并提交；**GitHub Pages 官方主页已启用**：`https://xsoway.github.io/auto-api-test-harness/`（source=`main:/` + `.nojekyll` 跳过 Jekyll）；`homepageUrl` 已指向它；本地+线上无头浏览器自测均通过（桌面 + 380px 无溢出、console 零报错）。 |
| **I** | 社区运营 | ⏳ 待执行 | GitHub Discussions 为仓库级功能，合并前手动在仓库 Settings 开启（见下） |
| **J** | 发布产物 | ✅ 就绪 | `dist/*.tar.gz` + `dist/*.whl` 已由 `uv build` 生成（gitignored）；release 命令见下 |
| **K** | 质量门禁 | ✅ 通过 | 本文件即 RELEASE_CHECKLIST.md，A/B/C/D/E/H/K 已落盘证据；存档前叠加 `verify_task` 类 gate |

---

## F. 发布动作（需用户授权后执行）

```bash
# 1) 推送主分支（需授权）
git push -u origin main

# 2) 在 GitHub 创建仓库并推送（需授权；owner 换成你的账号）
gh repo create auto-api-test-harness --public --source . --remote origin --push
```

## G. 可发现性配置

```bash
# topics（可在 About 面板或 CLI 添加）
gh repo edit --add-topic api-testing --add-topic pytest --add-topic yaml \
  --add-topic test-automation --add-topic llm --add-topic openapi

# 仓库描述 / homepage 链接
gh repo edit --description "Agent-oriented API testing & case evolution framework (Python + pytest + YAML)"
gh repo edit --homepage "https://<owner>.github.io/auto-api-test-harness/"   # 若发布 GitHub Pages

# 校验
gh repo view --json name,description,homepageUrl,repositoryTopics
```

## I. 开启 GitHub Discussions

1. 仓库主页 → Settings → **General** → 拉到 **Features** 区；
2. 勾选 **Discussions**；按提示选择讨论分类模板（Announcements / Q&A / Ideas / General）初始化；
3. 加载 `docs/discussion-intro.md`（可选）：写一篇 #Introduce-yourself / 用途说明置顶，作为首个 Announcement。

## J. 创建 Release（dist 产物作附件）

```bash
gh release create v0.1.0 \
  dist/auto_api_test_harness-0.1.0.tar.gz \
  dist/auto_api_test_harness-0.1.0-py3-none-any.whl \
  --title "v0.1.0 — initial release" \
  --notes "See README.md. 698 tests passing, LLMOFF=1 offline regression."
```

## K. 质量门禁复盘

- 测试：`pytest -q tests projects/demo` → `698 passed`（E 节）
- 构建：`uv build` 产出 sdist + wheel（E/J 节）
- 敏感信息：全部已跟踪文件扫描 clean（D 节）
- 主页：无头浏览器交互自测 16/16 通过 + 380px 响应式无溢出（H 节）
- 归档前建议在项目归档流程叠加 `tools/verify_task.py` 类 gate 复核

---

## 验证签名

- 检查执行时间：2026-06-26
- 执行人：Alan_Hsu / xsoway（F 推送、G topics 已获授权并执行）
- 发布了仓库：public → `https://github.com/xsoway/auto-api-test-harness`
- 官方主页：GitHub Pages → `https://xsoway.github.io/auto-api-test-harness/`（`homepageUrl` 已指向）
- 数据版本 / 基线：`v0.1.0` (commit `b6fa4f2`；推送 HEAD `a26b373`，主页部署依赖 `.nojekyll` 提交 `768ea0b`)