"""请求级模型路由（X-LLM-Route）的单测。

三条要守住的东西：
1. 默认行为不能被这个功能改变——不带头时 route() 的结果与以前逐字节一致；
2. 覆盖只活在一次请求里，出了请求必须消失（否则就是"点一次，全站变本地"）；
3. 流式响应跨线程迭代时仍然看得见同一个覆盖——这正是用中间件而非端点参数的原因，
   也是最容易被"本地能跑通、一开流式就回云端"这种问题绕过的一环。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from app.core import llm as llm_mod
from app.core.config import settings
from app.core.llm import (
    RouteOverrideMiddleware,
    gateway,
    local_model_status,
    reset_route_override,
    route_override,
    set_route_override,
)


@pytest.fixture(autouse=True)
def _cloud_default(monkeypatch):
    """每个用例都从"云端可用、本地已启用"这个基准出发。"""
    monkeypatch.setattr(settings, "llm_mock", False)
    monkeypatch.setattr(settings, "llm_route", "auto")
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(settings, "ollama_enabled", 1)
    monkeypatch.setattr(settings, "ollama_model", "sciagent-3b-v2")
    yield


@pytest.fixture(autouse=True)
def _no_probe_cache():
    llm_mod._LOCAL_PROBE.update(at=0.0, payload=None)
    yield
    llm_mod._LOCAL_PROBE.update(at=0.0, payload=None)


def test_without_override_route_is_unchanged():
    assert route_override() is None
    assert gateway.route() == "cloud"


def test_override_local_only_lives_in_its_scope():
    token = set_route_override("local")
    try:
        assert gateway.route() == "local"
    finally:
        reset_route_override(token)
    assert gateway.route() == "cloud"


def test_override_local_falls_back_when_ollama_disabled(monkeypatch):
    """要本地但本机没开 Ollama：退回全局策略，而不是硬着头皮 502。"""
    monkeypatch.setattr(settings, "ollama_enabled", 0)
    token = set_route_override("local")
    try:
        assert gateway.route() == "cloud"
    finally:
        reset_route_override(token)


def test_override_cannot_escape_mock(monkeypatch):
    """强制模拟优先级最高（D-77）：演示时点过"模拟模式"，切模型不该把它顶掉。"""
    monkeypatch.setattr(settings, "llm_mock", True)
    token = set_route_override("local")
    try:
        assert gateway.route() == "mock"
    finally:
        reset_route_override(token)


def test_override_ignores_garbage():
    token = set_route_override("ollama")
    try:
        assert route_override() is None
        assert gateway.route() == "cloud"
    finally:
        reset_route_override(token)


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(RouteOverrideMiddleware)

    @app.get("/probe")
    def probe():
        return {"route": gateway.route()}

    @app.get("/stream")
    def stream():
        def gen():
            # 分块之间会换线程执行，覆盖若只存在"某个线程的局部"就会在这丢掉
            for _ in range(3):
                yield gateway.route() + "\n"

        return StreamingResponse(gen(), media_type="text/plain")

    return TestClient(app)


def test_middleware_applies_header_to_sync_endpoint():
    c = _client()
    assert c.get("/probe").json()["route"] == "cloud"
    assert c.get("/probe", headers={"X-LLM-Route": "local"}).json()["route"] == "local"
    # 请求结束必须复位：否则下一个访客会继承上一个人的选择
    assert c.get("/probe").json()["route"] == "cloud"


def test_middleware_applies_header_to_streaming_body():
    c = _client()
    assert c.get("/stream").text == "cloud\ncloud\ncloud\n"
    assert c.get("/stream", headers={"X-LLM-Route": "local"}).text == "local\nlocal\nlocal\n"
    assert c.get("/stream").text == "cloud\ncloud\ncloud\n"


class _Resp:
    status_code = 200

    def __init__(self, names):
        self._names = names

    def json(self):
        return {"models": [{"name": n} for n in self._names]}


class _Client:
    def __init__(self, names):
        self._names = names

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        return _Resp(self._names)


def test_local_status_requires_model_present(monkeypatch):
    """端口通、模型列表非空，但名字对不上，也必须判成不可用。

    这就是这次真实踩到的坑：.env 还写着 qwen2.5:3b，本机只剩自训模型，
    只看"端口是否可达"的实现会显示一切正常，用户一点就 404。
    """
    monkeypatch.setattr(llm_mod.httpx, "Client", lambda *a, **k: _Client(["qwen2.5:3b"]))
    st = local_model_status(max_age=0)
    assert st["reachable"] is True
    assert st["available"] is False
    assert "sciagent-3b-v2" in st["reason"]


def test_local_status_accepts_latest_tag(monkeypatch):
    """ollama list 里带 :latest，配置里不带——要认成同一个模型，否则永远"不可用"。"""
    monkeypatch.setattr(llm_mod.httpx, "Client",
                        lambda *a, **k: _Client(["sciagent-3b-v2:latest"]))
    st = local_model_status(max_age=0)
    assert st["available"] is True
    assert st["reason"] == ""


def test_local_status_disabled_does_not_probe(monkeypatch):
    monkeypatch.setattr(settings, "ollama_enabled", 0)
    monkeypatch.setattr(llm_mod.httpx, "Client",
                        lambda *a, **k: pytest.fail("开关关闭时不该发起探测请求"))
    st = local_model_status(max_age=0)
    assert st["enabled"] is False
    assert st["available"] is False
    assert "OLLAMA_ENABLED" in st["reason"]
