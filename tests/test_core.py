"""Step 5 验证：上下文、提取、断言、加载器。"""
import httpx
import pytest

from ah.harness.http import APIResponse
from ah.harness.loader import load_cases
from ah.metrics.assertions import assert_response
from ah.metrics.extract import ExtractionError, extract_vars
from ah.utils.context import Context, ContextManager, render


# ---------- 上下文 ----------
def test_render_and_missing_var():
    ctx = Context({"user_id": 42})
    assert render("/users/${user_id}", ctx) == "/users/42"
    with pytest.raises(KeyError) as ei:
        render("/x/${ghost}", ctx)
    assert "ghost" in str(ei.value)


def test_context_manager_thread_local():
    cm = ContextManager()
    root = cm.current()
    pushed = cm.push()
    import threading

    other = []
    t = threading.Thread(target=lambda: other.append(cm.current() is root))
    t.start()
    t.join()
    assert other[0] is False  # 其它线程有自己独立的根
    assert cm.current() is pushed
    assert cm.pop() is pushed
    assert cm.current() is root


# ---------- 提取 ----------
def test_extract_jsonpath_and_regex():
    ctx = Context()
    extracted = extract_vars(
        '{"user": {"id": 7, "name": "amy"}}',
        {"uid": {"path": "user.id", "extract_type": "jsonpath"}},
        ctx,
    )
    assert extracted == {"uid": 7}
    assert ctx.get("uid") == 7
    extract_vars(
        '<a href="/u/9">link</a>',
        {"ref": {"path": r"/u/(\d+)", "extract_type": "regex"}},
        ctx,
    )
    assert ctx.get("ref") == "9"


def test_extract_no_match_raises():
    ctx = Context()
    with pytest.raises(ExtractionError):
        extract_vars('{"a": 1}', {"x": {"path": "a.b.c"}}, ctx)


# ---------- 断言 ----------
def _resp(text='{"ok": true, "n": 5, "msg": "hello"}', status=200):
    return APIResponse(httpx.Response(status, text=text))


def test_assert_status_code_and_jsonpath():
    assert_response(
        {
            "status_code": 200,
            "jsonpath": {"ok": {"eq": True}, "n": {"gt": 3}, "msg": {"contains": "ell"}},
        },
        _resp(),
        Context(),
    )


def test_assert_failure_normalized_message():
    with pytest.raises(AssertionError) as ei:
        assert_response({"status_code": 201}, _resp(status=200), Context())
    assert "{status_code} expected 201, got 200" in str(ei.value)


def test_assert_schema_and_contains():
    assert_response(
        {"schema": {"type": "object", "properties": {"n": {"type": "integer"}}}},
        _resp(),
        Context(),
    )
    with pytest.raises(AssertionError):
        assert_response({"contains": "zzz"}, _resp(), Context())


def test_assert_contains_list_requires_every_fragment():
    """contains 为子串列表时，每个片段都必须存在于响应文本中（逐项校验）。"""
    # 全部命中 -> 通过
    assert_response(
        {"contains": ["hello", "ok"]}, _resp('{"ok": true, "msg": "hello"}'), Context()
    )
    # 缺失其一 -> 失败，错误信息点明缺失片段
    with pytest.raises(AssertionError) as ei:
        assert_response({"contains": ["hello", "missing"]}, _resp(), Context())
    assert "missing" in str(ei.value)


def test_assert_llm_hermetic_skips_when_llm_off():
    from ah.metrics.assertions import assert_llm

    out = assert_llm("list should be sorted", "whatever", None)
    assert out["verdict"] == "SKIP"
    assert out["pass"] is None


def test_assert_llm_pass_true_is_pass():
    from ah.llm.mock import MockLLM
    from ah.metrics.assertions import assert_llm

    llm = MockLLM()
    llm.set_json_response({"pass": True, "reason": "all in stock"})
    out = assert_llm("list should contain all in-stock items", "x", llm)
    assert out["verdict"] == "PASS"
    assert out["pass"] is True


def test_assert_llm_pass_false_downgrades_to_unverified():
    from ah.llm.mock import MockLLM
    from ah.metrics.assertions import assert_llm

    # 语义判定"失败" ≠ test 失败：降级 UNVERIFIED + 人工复核标注，不进硬闸。
    llm = MockLLM()
    llm.set_json_response({"pass": False, "reason": "price not ascending"})
    out = assert_llm("price ascending", "x", llm)
    assert out["verdict"] == "UNVERIFIED"
    assert out["pass"] is False
    assert out.get("flagged") is True


def test_assert_llm_non_json_downgrades_to_unverified():
    from ah.metrics.assertions import assert_llm

    class _Broken:
        def judge(self, task, prompt, model="strong"):
            return "not-json-at-all"

    out = assert_llm("anything", "x", _Broken())
    assert out["verdict"] == "UNVERIFIED"
    assert out["pass"] is False

# ---------- 加载器 ----------
def test_load_single_case(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("name: get-user\nrequest:\n  method: GET\n  path: /users/${uid}\n"
                 "extract:\n  id: {path: user.id}\nauth: default_token\n")
    cases = load_cases(str(p))
    assert len(cases) == 1
    c = cases[0]
    assert c.name == "get-user"
    assert c.request_path == "/users/${uid}"
    assert c.auth == "default_token"


def test_load_list_and_scenario(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text(
        "cases:\n"
        "  - {name: a, request: {path: /a}}\n"
        "  - {name: b, request: {path: /b}}\n"
    )
    assert [c.name for c in load_cases(str(p))] == ["a", "b"]

    s = tmp_path / "scen.yaml"
    s.write_text(
        "name: order-flow\non_failure: continue\nscenario:\n"
        "  - {name: create, request: {method: POST, path: /orders}}\n"
        "  - {name: verify, request: {method: GET, path: /orders/1}}\n"
    )
    scen = load_cases(str(s))
    assert [c.scenario for c in scen] == ["order-flow", "order-flow"]
    assert [c.order for c in scen] == [0, 1]
    assert all(c.on_failure == "continue" for c in scen)
