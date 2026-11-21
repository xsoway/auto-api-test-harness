# 用例模型：Case 与 YAML schema

`src/ah/harness/loader.py` 定义 `Case` 数据类与两个加载函数。全部事实以此为准。

## Case 数据类

```python
@dataclass
class Case:
    name: str
    request: dict[str, Any]                      # 必填
    extract: dict[str, dict[str, Any]] = dict    # 响应提取
    assert_: dict[str, Any] = dict               # 断言
    vars_req: list[str] = []                     # 需要先解析的变量
    vars_output: list[str] = []                  # 本步输出到 ctx 的变量
    auth: Optional[str] = None                   # 传给令牌管理器的令牌名
    llm_gen: dict[str, Any] = dict               # 走 AgentDataGen 的生成 schema
    on_failure: str = "stop"                     # "stop" | "continue"
    scenario: Optional[str] = None               # 场景共享 id
    order: int = 0                               # 场景内步骤顺序
    source_path: str = ""
    env: str = "all"                             # 目标环境：all | test | prod
    priority: str = "P2"                         # P0 | P1 | P2
    suite: str = "regression"                    # smoke | regression | prod | all
    tags: list[str] = []                         # 自由标签，供选择器匹配
    retries: int = 0                             # 瞬时错误（5xx/超时/连接）额外重试次数
    retry_backoff: float = 0.0                   # 重试之间退避秒数
    timeout: Optional[float] = None              # 用例级超时（秒），覆盖客户端默认
    soft_assert: bool = False                    # 软断言：失败只记录不判失败
```

- `request_path` 属性返回 `request["path"]`（含 `${var}` 插值模板）。
- **旧版兼容**：默认先提取再断言，失败时可能保留输出。`schema_version: 2` 为严格执行，只有验证成功的产出可供下游消费。

## 单接口 YAML

```yaml
name: get-user-by-id
request:
  method: GET
  path: /users/${user_id}        # ${var} 从 ctx 解析
  headers:
    Accept: application/json
auth: default_token              # 可选：传给令牌管理器
assert:
  status_code: 200
  jsonpath:
    user.id: { eq: "${user_id}" }
extract:
  fetched_name:
    path: user.name
    extract_type: jsonpath
```

一个文件可以是单用例映射、`cases:` 下列表、或带 `on_failure` 并列标志（见 load_cases）。

## 场景 YAML

```yaml
name: order-flow
on_failure: stop                 # 场景级失败策略
scenario:
  - name: login
    request: { method: POST, path: /login, json: { username: alice } }
    assert: { status_code: 200 }
    extract:
      user_token:
        path: token
        extract_type: jsonpath
  - name: create-order
    request:
      method: POST
      path: /orders
      headers: { Authorization: "Bearer ${user_token}" }   # 下游使用上游提取值
    assert: { status_code: 201 }
```

`scenario:` 下的每个步骤被 `load_cases` 展开为独立 `Case`，共享同一 `scenario` id 并按 `order` 顺序执行；`on_failure: stop` 让失败中止整条链路。

## 加载与发现

```python
from ah.harness.loader import load_cases, discover_case_files

cases = load_cases("projects/demo/users_single.yaml")   # -> list[Case]
# 多个用例/单用例/场景统一展开

files = discover_case_files("projects", env_var="AH_CASES")
# 读 AH_CASES（冒号/glob 分隔）；否则递归找 *.yaml，返回排序后的路径列表
```

## 关键约束

- `${var}` 插值由 `ah.utils.context.Context.render` 在 `run_single` 内执行（`_build_request`）。
- `extract.extract_type` 支持 `jsonpath`（底层 jmespath）。`assert.jsonpath` 同理。
- `on_failure` 只接受 `stop` / `continue`，其余按默认 `stop` 处理。
- 写新用例复制 `templates/`，别凭空编字段。
- `retries` / `retry_backoff`：仅对瞬时错误（5xx/超时/连接）重试，区别于 L1 自愈（固定一次）。
- `timeout`：本用例请求超时（秒），缺省用客户端默认；`soft_assert: true` 时断言失败只记录
  （`llm_notes.soft_failures`）、用例仍判通过。
- 请求/断言里可用占位符 `${rand:uuid}`、`${rand:int:min-max}`、`${rand:float}`、
  `${date:now[:fmt]}`、`${date:±Nd|h|m}`（由 `render` 先于 `${var}` 插值解析）。

## 版本 2：显式依赖与原生类型

场景根设置 `schema_version: 2`，loader 将 `strict=True`、`reference_mode="typed"` 传给各步骤。旧文件不加版本号仍使用 legacy 字符串引用；不要为兼容而把 ID 全部转字符串。

```yaml
schema_version: 2
name: synthetic-create-query
on_failure: continue
scenario:
  - name: create
    request: {method: POST, path: /synthetic/resources}
    assert: {status_code: 201}
    extract:
      create_id: {path: id, extract_type: jsonpath}
  - name: query
    depends_on: [create]
    request:
      method: POST
      path: /synthetic/query
      json: {id: "${create_id}"}
    assert: {status_code: 200}
```

这是虚构教学接口，不能替代业务接口目录和业务终态断言。

- JSON 和断言期望值的整值 `${create_id}` 保留整数/布尔/null/数组/对象类型；嵌入字符串仍是字符串。path/header 为文本位置。
- `request.data` 递归渲染。提取的 `jsonpath` 仍是历史字段名，执行语言为 JMESPath，不接受未经转换的 `$.data.id`。
- `depends_on` 使用场景内唯一的步骤名；失败、无断言或未执行的强依赖不得发出下游请求。`continue` 允许独立步骤继续，不允许消费失败产出。
- 每个计划步骤均有结果；`blocked`/`skipped` 不表示执行过 HTTP。判断业务验证应读 `verdict`，不能仅看 `succeeded`。
- 不支持的版本/引用模式必须显式报错。自动生成模型还需通过 `ah scenario validate`，确认关系后才能编译。
