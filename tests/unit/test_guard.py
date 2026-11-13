"""访问防护与检索预算的单测。

这些规则一旦错了，后果两极：限流太紧 = 正常用户被 429；太松 = 形同虚设。
单测锁住的是**判定逻辑**（滑动窗口、分桶、令牌来源），不锁具体数字——
数字是后台可调的参数，写死在测试里只会让每次调参都得改测试。
"""
from __future__ import annotations

import time

import pytest

from app.core.config import settings
from app.core.guard import Bucket, Guard, client_ip
from app.rag import budget


# ---------------------------------------------------------------- 限流
def test_bucket_sliding_window():
    b = Bucket()
    now = 1000.0
    for _ in range(3):
        assert b.allow(3, now)
    # 第 4 次超限
    assert not b.allow(3, now)
    # 60 秒后窗口滑走，重新放行
    assert b.allow(3, now + 61)


def test_bucket_zero_means_unlimited():
    b = Bucket()
    for _ in range(100):
        assert b.allow(0, 1000.0)


def test_guard_separates_ask_and_read(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_min", 3, raising=False)
    monkeypatch.setattr(settings, "rate_limit_ask_per_min", 1, raising=False)
    g = Guard()
    ok, kind = g.check("1.2.3.4", "/api/chat")
    assert ok and kind == "ask"
    # ask 桶额度 1，第二次就满
    ok, kind = g.check("1.2.3.4", "/api/chat")
    assert not ok
    # read 桶独立计数，不受影响
    for _ in range(3):
        ok, kind = g.check("1.2.3.4", "/api/documents")
        assert ok, "read 桶不应被 ask 桶的额度影响"
    assert kind == "read"


def test_guard_per_ip(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_min", 2, raising=False)
    g = Guard()
    assert g.check("9.9.9.9", "/api/x")[0]
    assert g.check("9.9.9.9", "/api/x")[0]
    assert not g.check("9.9.9.9", "/api/x")[0]
    # 换 IP 立刻放行：限流的目的不是"全站配额"，而是"挡住单个来源"
    assert g.check("8.8.8.8", "/api/x")[0]


def test_upload_semaphore_single_slot():
    g = Guard()
    assert g.acquire_upload(timeout=0.01) is True
    assert g.acquire_upload(timeout=0.01) is False   # 同一时刻只解析一份
    g.release_upload()
    assert g.acquire_upload(timeout=0.01) is True
    g.release_upload()
    # 多释放一次不能把计数放成 2（BoundedSemaphore 会拦，但这里也要不炸）
    g.release_upload()


class _FakeReq:
    def __init__(self, headers=None, query=None, host="5.6.7.8"):
        self.headers = headers or {}
        self.query_params = query or {}
        self.client = type("C", (), {"host": host})()


def test_client_ip_prefers_forwarded_for():
    r = _FakeReq(headers={"x-forwarded-for": "1.1.1.1, 10.0.0.1"})
    assert client_ip(r) == "1.1.1.1"
    assert client_ip(_FakeReq()) == "5.6.7.8"


# ---------------------------------------------------------------- 检索预算
@pytest.mark.parametrize("q,kind", [
    ("对比 RAG 与 RETRO 的检索思路", "compare"),
    ("自注意力每层的计算复杂度是多少", "numeric"),
    ("多头注意力机制的作用是什么", "concept"),
    ("Attention 是哪一年发表的", "fact"),
    ("介绍一下这个库", "lookup"),
])
def test_budget_classify(q, kind):
    assert budget.classify(q) == kind


def test_budget_compare_wins_over_numeric():
    # "对比两篇的参数量"应该走对比，而不是数值抽取——先判具体意图
    assert budget.classify("对比两篇论文的参数量") == "compare"


def test_budget_mode_compare_forces_compare():
    assert budget.classify("随便问点什么", mode="compare") == "compare"


def test_budget_topk_ordering():
    # 对比题给得最多、数值题给得最少，且都受后台参数控制
    assert budget.top_k("对比 A 与 B") >= budget.top_k("有多少参数")
    assert budget.top_k("有多少参数") >= 1


def test_budget_stitch_zero_for_numeric():
    # 数值题的答案就在那一行，补邻块只会带来表格的下一行
    assert budget.stitch_n("参数量是多少") == 0
    assert budget.stitch_n("对比 A 与 B") >= 1


def test_guard_exempts_loopback(monkeypatch):
    """本机回环默认不限流：否则一次 qa_suite 会把自己打成一片 429。"""
    monkeypatch.setattr(settings, "rate_limit_per_min", 1, raising=False)
    monkeypatch.setattr(settings, "rate_limit_local", 1, raising=False)
    g = Guard()
    for _ in range(5):
        ok, kind = g.check("127.0.0.1", "/api/documents")
        assert ok and kind == "local"
    # 关掉豁免后立刻按普通 IP 计数
    monkeypatch.setattr(settings, "rate_limit_local", 0, raising=False)
    g2 = Guard()
    assert g2.check("127.0.0.1", "/api/documents")[0]
    assert not g2.check("127.0.0.1", "/api/documents")[0]
