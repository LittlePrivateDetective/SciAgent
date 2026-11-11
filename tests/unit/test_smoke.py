"""装配冒烟测试：只验"代码能不能装起来"，不连任何外部服务。

为什么值得单独一条：本项目最容易犯的错不是算法错，而是**模块接线断了**——
新增一个路由忘了导入、给函数加了必填参数没改所有调用点、循环 import。
这些在开发机上因为有热重载和缓存看不出来，CI 里却一跑就炸。
"""
from __future__ import annotations

import importlib


def test_app_imports_and_exposes_routes():
    m = importlib.import_module("app.main")
    # FastAPI 0.16x 起 include_router 是惰性的：app.routes 里只有一个 _IncludedRouter
    # 占位对象，数不出真实端点数。走 openapi() 让它展开，才是实际注册的接口。
    paths = m.app.openapi().get("paths", {})
    assert m.app.title.startswith("SciAgent")
    api = [p for p in paths if p.startswith("/api")]
    assert len(api) >= 60, f"接口数异常：{len(api)}"


def test_watcher_and_keepalive_are_wired():
    """两个后台循环都必须存在且可调用——挂了不会崩服务，但功能会静默消失。"""
    from app.core import keepalive
    from app.research import watcher

    assert callable(keepalive.start) and callable(keepalive.status)
    assert callable(watcher.start) and callable(watcher.run_once)
    st = watcher.status()
    assert {"enabled", "interval_s", "items"} <= set(st)


def test_params_registry_covers_new_keys():
    """后台参数中心必须认识新加的三个追踪参数，否则界面上根本改不了。"""
    from app.core.params import BY_KEY

    for k in ("discover_watch_enabled", "discover_watch_interval", "discover_watch_days"):
        assert k in BY_KEY, f"参数中心缺 {k}"


def test_chunker_and_retrieval_entrypoints_exist():
    """D-67 改动的两个入口要能 import 到（防止改名后调用点没同步）。"""
    from app.rag.chunker import _is_grid_fragment, build_chunks
    from app.rag.hybrid import _ensure_table_candidates, search_with_rerank

    assert callable(build_chunks) and callable(_is_grid_fragment)
    assert callable(search_with_rerank) and callable(_ensure_table_candidates)
