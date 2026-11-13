"""QA-P1-01：限定文献时，点名补齐也不得越过 doc_ids。"""
from app.rag import hybrid


def hit(chunk_id: str, doc_id: str) -> hybrid.Hit:
    return hybrid.Hit(chunk_id=chunk_id, doc_id=doc_id, doc_name=doc_id, page=1,
                      bbox="1,2,3,4", text="attention", section_path="", kind="text",
                      score=1.0)


def test_scope_hits_hard_filter():
    rows = [hit("text", "text-doc"), hit("scan", "scan-doc")]
    assert [h.doc_id for h in hybrid._scope_hits(rows, ["text-doc"])] == ["text-doc"]
    assert len(hybrid._scope_hits(rows, None)) == 2
    assert len(hybrid._scope_hits(rows, [])) == 2


def test_mentioned_doc_backfill_respects_scope(monkeypatch):
    rows = [hit("text", "text-doc")]
    monkeypatch.setattr(hybrid, "_alias_boosts", lambda q: {"scan-doc": 1.7})
    called = []
    monkeypatch.setattr(hybrid, "search", lambda *a, **kw: called.append(a) or [hit("scan", "scan-doc")])
    out = hybrid._ensure_mentioned_docs(rows, "Transformer", 5, ["text-doc"])
    assert [h.doc_id for h in out] == ["text-doc"]
    assert called == []


def test_stitched_result_final_scope_guard():
    rows = [hit("text", "text-doc"), hit("scan", "scan-doc")]
    assert all(h.doc_id == "text-doc" for h in hybrid._scope_hits(rows, ["text-doc"]))
