#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : 2026-10-08 20:37:43
# @Filename : scaffold.py
# @Author   : Alan_Hsu

"""WS2 · 业务项目脚手架与测试入口生成（功能点 5、6、7）。

职责：
- ``ah scaffold <business> --api <url>`` —— 生成新业务项目目录骨架
  （``projects/<business>/``：``data/`` 最小用例 + ``test_<name>.py`` 执行入口 +
  ``conftest.py`` + ``README.md``），带 hermetic 护栏与 ``--run-live`` 巡检范本。
- ``ah scaffold --gen-entry <business>`` —— 从已有 ``data/*.yaml`` 用
  :func:`ah.harness.loader.load_cases` 确定性生成/刷新 ``test_<name>.py`` 执行入口
  （纯代码生成，不依赖 LLM）。
- ``ah add-case <project> "<自然语言>"`` —— 用 :class:`ah.predict.assist.AgentNL2Case`
  把自然语言转成用例，追加写回 ``data/<name>.yaml``（追加 + 去重），可选
  ``--run-live`` 验证。

所有命令在 ``LLMOFF=1`` 下可跑：scaffold/gen-entry 是纯本地文件生成；add-case 在
LLM 关闭时返回明确错误（提示需配置 LLM），绝不发起任何网络调用。
"""

from __future__ import annotations

import re
from argparse import ArgumentParser, Namespace, _SubParsersAction
from pathlib import Path
from string import Template

import yaml

from ah.harness.http import HTTPClient
from ah.harness.loader import Case, load_cases
from ah.llm.openai import LLMClient, llm_enabled
from ah.predict.assist import AgentNL2Case
from ah.predict.generator import GeneratedCase
from ah.utils.context import Context
from ah.utils.logging import get_logger

log = get_logger("ah.scaffold")

#: 仓库根：scaffold 默认把新项目写在 repo 的 ``projects/`` 下。
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROJECT_DIR = _REPO_ROOT / "projects"

#: 生成文件头里的时间戳。
_TIMESTAMP = "2026-10-08 20:37:43"


# ───────────────────────── 工具函数 ─────────────────────────

def _slugify(text: str) -> str:
    """把业务名转成仅含小写字母/数字/下划线的安全标识。

    用作目录名与文件名前缀，避免中文/空格/特殊字符进入文件路径与函数名。
    空结果回退为 ``project``。
    """
    out = re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_").lower()
    return out or "project"


def _dedent(raw: str) -> str:
    """去掉模板首尾换行，便于直接嵌入生成文件。"""
    return raw.strip("\n")


def _write(dst: Path, content: str) -> None:
    """写文件并确保父目录存在。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(content, encoding="utf-8")


# ───────────────────────── 测试执行入口代码生成（功能6） ─────────────────────────
# 模板外层统一用 '''（单引号三引号），内嵌的 Python 模块 docstring 用 """（双引号
# 三引号），两者不冲突；占位符用 $（string.Template），避免与代码里的 {} 冲突。

_TEST_ENTRY = Template(_dedent('''
#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : ${now}
# @Filename : ${test_name}
# @Author   : Alan_Hsu

"""${project_doc_head}

加载 ./data/ 下全部 YAML 用例，默认运行 hermetic 护栏（不连网），加 --run-live 时
对真实服务逐用例巡检。报告自动写到本项目 report/。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import httpx

from ah.core.orchestrator import run_single
from ah.harness.http import HTTPClient
from ah.harness.loader import load_cases
from ah.utils.context import Context

_DIR = Path(__file__).resolve().parent
DATA_DIR = _DIR / "data"
# 是否连真实服务巡检：显式 --run-live 或设 AH_RUN_LIVE=1。
_RUN_LIVE = os.getenv("AH_RUN_LIVE") == "1"


def cases() -> list:
    """本项目全部用例（从 data/ 下全部 .yaml 加载）。"""
    out: list = []
    for p in sorted(DATA_DIR.iterdir()):
        if p.suffix == ".yaml":
            out.extend(load_cases(p))
    return out


# ---- 数据质量护栏：保证本项目用例集合法、可被可靠执行 ---------------------


def test_dataset_has_expected_shape():
    """用例非空、name 合法、每条都有 status_code 断言。"""
    cs = cases()
    assert cs, "data/ 下没有可加载用例"
    assert all(c.name for c in cs), "存在空用例 name"
    assert all(
        isinstance(c.assert_.get("status_code"), int) for c in cs
    ), "存在缺失 status_code 断言的用例"


def test_dataset_names_are_unique():
    """用例 name 唯一，避免参数化 id 冲突与报告混淆。"""
    names = [c.name for c in cases()]
    assert len(names) == len(set(names)), "存在重复用例 name"


# ---- hermetic 逻辑验证：不连真实服务 ---------------------------------------


def test_all_hermetic_reachable_with_served_fragments(run_reporter):
    """注入全 contains 片段的 stub 响应，全部用例逻辑通过（不连网）。"""
    fragments: list[str] = []
    for c in cases():
        n = c.assert_.get("contains")
        if isinstance(n, str):
            fragments.append(n)
        elif isinstance(n, list):
            fragments.extend(n)
    body = '<html lang="en">' + " ".join(fragments) + "</html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    client = HTTPClient("", client=httpx.Client(transport=httpx.MockTransport(handler)))
    failed: list[str] = []
    for c in cases():
        start = time.perf_counter()
        result = run_single(c, Context(), client)
        run_reporter.record(result, start=start)
        if not result.succeeded:
            failed.append(c.name)
    assert not failed, f"未通过用例: {failed}"


# ---- 真实连通性巡检：--run-live 触发，默认跳过 ------------------------------


@pytest.mark.parametrize("case", cases(), ids=lambda c: c.name)
def test_live(case, request, run_reporter):
    """逐用例连接真实服务校验（仅 --run-live / AH_RUN_LIVE=1 时运行）。"""
    if not (request.config.getoption("--run-live") or _RUN_LIVE):
        pytest.skip("未启用 --run-live，跳过真实巡检")
    client = HTTPClient("")
    start = time.perf_counter()
    result = run_single(case, Context(), client)
    run_reporter.record(result, start=start)
    if not result.succeeded:
        pytest.fail(f"{case.name!r} real 巡检失败: {result.error}")


if __name__ == "__main__":
    # 便捷直跑：默认离线验证；透传额外 pytest 参数。
    args = list(__import__("sys").argv[1:])
    __import__("sys").exit(
        pytest.main(["-q", "-p", "no:cacheprovider", "-s", str(__file__), *args])
    )
'''))


_CONFTEST = Template(_dedent('''
#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time     : ${now}
# @Filename : conftest.py
# @Author   : Alan_Hsu

"""${project_id} 业务项目 pytest 配置（由 ah scaffold 生成）。

本项目自包含运行：数据集在 ``data/``、执行入口为 ``test_${test_stem}.py``、报告写到
本项目 ``report/``（``AH_REPORT_DIR`` 兜底指向），与其它业务项目隔离。

默认（不指定参数）只执行 hermetic 护栏用例，不连接真实服务。需要连真实服务做全量
巡检时加 ``--run-live``（或设环境变量 ``AH_RUN_LIVE=1``），此时会逐用例校验。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ah.core.selfcontained import AutoReporter

_PROJECT_ROOT = Path(__file__).resolve().parent
_REPORT_DIR = _PROJECT_ROOT / "report"
_REPORTER_KEY = "${project_id}_reporter"


def pytest_sessionstart(session: "pytest.Session") -> None:
    """建本项目 AutoReporter：逐用例日志 + 会话结束自动写报告到 report/。"""
    session.config.stash[_REPORTER_KEY] = AutoReporter(
        _REPORT_DIR,
        log_name="ah.selfcontained.${project_id}",
        report_basename="test-${project_id}",
    )


def pytest_sessionfinish(session: "pytest.Session", exitstatus: int) -> None:
    """跑完自动生成报告；无记录或失败都不中断。"""
    rep = session.config.stash.get(_REPORTER_KEY, None)
    if rep is not None:
        rep.write_reports()
    del exitstatus


@pytest.fixture
def run_reporter(request: "pytest.FixtureRequest") -> AutoReporter:
    """暴露本项目 AutoReporter，便于执行入口每驱动一条用例后 record()。"""
    return request.session.config.stash[_REPORTER_KEY]


def pytest_addoption(parser: "pytest.Parser") -> None:
    """注册本项目巡检开关：--run-live 触发真实服务巡检。"""
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="连接真实服务逐用例巡检（默认仅 hermetic，不连网）。",
    )


def pytest_configure(config: "pytest.Config") -> None:
    """将报告目录兜底指向本项目 report/，避免与其它项目报告混淆。"""
    os.environ.setdefault("AH_REPORT_DIR", str(_REPORT_DIR))
    config.addinivalue_line("markers", "${project_id}_live: 连接真实服务的巡检测试")
'''))


_DATA_SKELETON = Template(_dedent('''
# --- ah scaffold ---
# 新业务项目最小骨架用例。请按需修改方法/路径/断言/参数。
# schema 参考 skills/auto-api-test-harness/templates/single_case.yaml.j2。
cases:
- name: health-check
  request:
    method: GET
    path: ${api_url}
  assert:
    status_code: 200
'''))


_README = Template(_dedent('''
# ${project_id} 业务项目

由 ``ah scaffold`` 生成的自包含测试项目。默认跑 hermetic 护栏（不连网），
``--run-live`` 连真实服务巡检，报告自动写到 ``report/``。

## 结构

- ``data/${data_name}`` —— 用例数据集（YAML）。
- ``test_${test_stem}.py`` —— 执行入口：数据护栏 + hermetic 逻辑验证 + ``--run-live`` 巡检。
- ``conftest.py`` —— ``--run-live`` 开关 + 报告目录兜底。
- ``README.md`` —— 本说明。
- ``report/`` —— 运行报告（会话结束自动生成）。

## 运行

```bash
# hermetic：只跑护栏与逻辑验证，不连真实服务
pytest test_${test_stem}.py -q

# 真实巡检：连真实服务逐用例校验
pytest test_${test_stem}.py -q --run-live
```

## 追加用例

```bash
ah add-case ${project_id} "查询用户详情应返回 200 且包含 name 字段"
```

## 目标服务

- 入口 API：``${api_url}``
'''))


# ───────────────────────── 功能5：新项目脚手架 ─────────────────────────

def scaffold_project(
    business: str,
    api: str,
    *,
    project_dir: str | Path,
    gen_entry: bool = False,
    name: str = "",
) -> int:
    """生成/刷新一个业务项目。

    默认生成完整骨架（data + test 入口 + conftest + README）。``gen_entry=True`` 时
    只从已有 ``data/*.yaml`` 刷新测试执行入口（纯代码生成）。

    返回 0=成功，非 0=失败。
    """
    project_id = _slugify(business)
    proj = Path(project_dir) / project_id
    data_dir = proj / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    data_name = f"{_slugify(name or project_id)}.yaml"
    test_stem = _slugify(name or project_id)
    test_path = proj / f"test_{test_stem}.py"

    # 从已有 data/ 收集用例做确定性计数与合法性校验（功能6）。
    loaded: list[Case] = []
    for p in sorted(data_dir.glob("*.yaml")):
        try:
            loaded.extend(load_cases(p))
        except (ValueError, FileNotFoundError) as exc:
            log.error("无法加载 %s：%s", p, exc)
            return 2
    expected_cases = len(loaded)

    if not gen_entry:
        # 生成最小骨架用例（仅当目标 data 文件还不存在，避免覆盖既有数据）。
        data_path = data_dir / data_name
        if not data_path.exists():
            _write(data_path, _DATA_SKELETON.substitute(api_url=api))
            expected_cases = 1
            log.info("生成用例数据 %s", data_path)
        else:
            log.info("跳过已存在的用例数据 %s", data_path)

    # 生成 / 刷新测试执行入口。
    now = _TIMESTAMP
    _write(
        test_path,
        _TEST_ENTRY.substitute(
            now=now,
            test_name=test_path.name,
            project_doc_head=f"{project_id}（{api}）业务项目执行入口",
        ),
    )
    log.info("生成执行入口 %s", test_path)

    if not gen_entry:
        # conftest.py（存在则不覆盖，保留用户自定义）。
        conftest = proj / "conftest.py"
        if not conftest.exists():
            _write(
                conftest,
                _CONFTEST.substitute(
                    now=now, project_id=project_id, test_stem=test_stem
                ),
            )
            log.info("生成 conftest %s", conftest)

        # README.md（存在则不覆盖）。
        readme = proj / "README.md"
        if not readme.exists():
            _write(
                readme,
                _README.substitute(
                    project_id=project_id,
                    data_name=data_name,
                    test_stem=test_stem,
                    api_url=api,
                ),
            )
            log.info("生成 README %s", readme)

    log.info("项目就绪：%s（%d 条用例）", proj, expected_cases)
    return 0


# ───────────────────────── 功能7：NL→用例落地 ─────────────────────────

def add_case(
    project: str,
    text: str,
    *,
    project_dir: str | Path,
    run_live: bool = False,
) -> int:
    """把自然语言转成用例并追加到 ``data/<name>.yaml``。

    - 用 :class:`AgentNL2Case` 解析（LLM 关闭时返回 None → 报清晰错误，绝不联网）。
    - 追加写回该 data 文件的 ``cases`` 列表；与既有用例按 ``(method, path, name)``
      去重，重复则跳过。
    - ``run_live=True`` 且 LLM 可用时对真实服务验证。

    返回 0=成功，非 0=失败/判闸。
    """
    project_id = _slugify(project)
    proj = Path(project_dir) / project_id
    data_dir = proj / "data"
    if not proj.is_dir():
        log.error("项目不存在：%s（先在目标目录跑 ah scaffold）", proj)
        return 2

    data_files = sorted(data_dir.glob("*.yaml"))
    if not data_files:
        log.error("项目 %s 下没有 data/*.yaml，无法追加用例", project_id)
        return 2

    # 目标：项目名同名 yaml；不存在则用第一个。
    target = data_dir / f"{project_id}.yaml"
    if not target.exists():
        target = data_files[0]

    existing = load_cases(target)
    existing_keys = {(c.request.get("method", "GET"), c.request_path, c.name) for c in existing}

    # 构造 LLM：hermetic 判定由 llm_enabled 保证，非法路径绝不联网。
    if not llm_enabled():
        log.error(
            "LLM 未启用（LLMOFF 或未配置 api_key）：add-case 需要 LLM 才能把自然语言转成"
            "用例。Hermetic 下请用 MockLLM / 配置 api_key 后重试。",
        )
        return 3
    try:
        llm = LLMClient()
    except Exception as exc:  # noqa: BLE001 - 禁用/缺依赖统一成可读错误
        log.error("无法初始化 LLM：%s", exc)
        return 3

    client = HTTPClient("") if run_live else None
    agent = AgentNL2Case(llm, client=client, ctx=Context())
    res = agent.to_case(text, existing=existing, validate=run_live)
    if res is None:
        log.error("自然语言转用例失败（LLM 未返回合法候选）。")
        return 3
    gen = res.generated

    return _persist_case(target, gen, existing_keys)


def _persist_case(
    data_file: Path,
    gen: GeneratedCase,
    existing_keys: set[tuple[str, str, str]],
) -> int:
    """把一条已生成用例追加到 ``data_file`` 的 ``cases`` 列表（去重后）。

    与既有用例按 ``(method, path, name)`` 比对去重：重复则跳过不写并返回 0（视为
    已成功，避免重复入库）。新用例额外打 ``ah-nl`` 来源标签，便于追溯。

    返回 0=成功（含去重跳过），非 0=失败。
    """
    key = (
        gen.request.get("method", "GET"),
        str(gen.request.get("path", "")),
        gen.name,
    )
    if key in existing_keys:
        log.info("用例已存在，跳过（去重）：%s %s %s", *key)
        return 0

    mapping = gen.to_yaml_mapping()
    mapping["name"] = gen.name
    mapping.setdefault("request", {})
    mapping["request"].setdefault("method", "GET")
    if not mapping["request"].get("path"):
        mapping["request"]["path"] = "/"
    mapping.setdefault("assert", {"status_code": 200})
    # 标记来源，便于追溯。
    tags = mapping.get("tags", [])
    mapping["tags"] = [t for t in tags if isinstance(t, str)] + ["ah-nl"]

    raw = _read_data(data_file)
    if isinstance(raw.get("cases"), list):
        # cases 列表形式：直接复用既有列表。
        cases_list = raw["cases"]
    elif "request" in raw or "name" in raw:
        # 单用例文件：原文件本身即一条用例，保留为首元素，避免追加时丢数据。
        cases_list = [dict(raw)]
    else:
        cases_list = []
    cases_list.append(mapping)
    # 保留原文件 `cases:` 之前的头部注释，追加写入。
    head = _data_head(data_file)
    _write_data(data_file, head, cases_list)
    log.info("已把用例 %r 追加到 %s（validated=%s）", gen.name, data_file, gen.validated)
    return 0

def _read_data(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def _data_head(path: Path) -> str:
    """提取 YAML 文件开头的注释行（头部说明），追加时保留。

    只收 ``#`` 开头的连续注释（允许穿插空行），到第一个内容键（如 ``cases:`` /
    ``name:`` / ``request:``）为止，避免把既有用例本身误当头部而重复写入。
    """
    lines: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.rstrip("\n").strip()
                if not stripped:
                    continue
                # 遇到首个内容行：若是注释继续，否则停止。
                if stripped.startswith("#"):
                    lines.append(line)
                    continue
                break
    except OSError:
        return ""
    return "".join(lines).rstrip("\n")


def _write_data(path: Path, head: str, cases_list: list) -> None:
    """把头部注释 + cases 列表写回 YAML。"""
    body = yaml.safe_dump({"cases": cases_list}, allow_unicode=True, sort_keys=False)
    if head:
        head = head + "\n"
    path.write_text(head + body, encoding="utf-8")


# ───────────────────────── CLI 挂载（register / run 契约） ─────────────────────────

def register(subparsers: "_SubParsersAction[ArgumentParser]") -> None:
    """把本模块子命令挂到主 CLI 的 subparsers（由主代理的 cli.py 调用）。"""
    p = subparsers.add_parser("scaffold", help="生成/刷新业务项目脚手架或测试执行入口")
    p.add_argument("business", help="业务名（作为 projects/ 下目录名）")
    p.add_argument("--api", default="http://localhost", help="被测服务入口 URL 或 spec 路径")
    p.add_argument("--name", default="", help="用例/入口文件名（缺省取自 business）")
    p.add_argument(
        "--gen-entry",
        action="store_true",
        help="只从已有 data/*.yaml 生成/刷新测试执行入口（功能6，纯代码生成）",
    )
    p.add_argument(
        "--project-dir",
        default=str(DEFAULT_PROJECT_DIR),
        help="项目根目录（默认仓库 projects/）",
    )
    p.set_defaults(func=_run_scaffold)

    p2 = subparsers.add_parser("add-case", help="把自然语言转成用例并追加到 data/")
    p2.add_argument("project", help="业务项目名（projects/ 下目录）")
    p2.add_argument("text", nargs="+", help="自然语言描述（自动用空格连接）")
    p2.add_argument(
        "--project-dir",
        default=str(DEFAULT_PROJECT_DIR),
        help="项目根目录（默认仓库 projects/）",
    )
    p2.add_argument("--run-live", action="store_true", help="对真实服务验证新用例")
    p2.set_defaults(func=_run_add_case)


def _run_scaffold(args: Namespace) -> int:
    return scaffold_project(
        args.business,
        args.api,
        project_dir=Path(args.project_dir),
        gen_entry=args.gen_entry,
        name=args.name,
    )


def _run_add_case(args: Namespace) -> int:
    return add_case(
        args.project,
        " ".join(args.text),
        project_dir=Path(args.project_dir),
        run_live=args.run_live,
    )


def run(args: Namespace) -> int:
    """执行本模块子命令；0=成功，非 0=失败/判闸。

    ``args.func`` 由 :func:`register` 的 ``set_defaults`` 绑定。
    """
    func = getattr(args, "func", None)
    if func is None:
        log.error("未知子命令：%s", getattr(args, "subcommand", ""))
        return 2
    return func(args)
