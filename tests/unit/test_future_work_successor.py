"""未来工作方向：后继文献约束 + 句向量精排的行为约束。

三个点：
1. 候选只取「引用了本文」的后继，不把全库较新论文都算成后续工作；
2. 一条后继都没有时，才退到同主题文献，并如实标 basis=related；
3. 句级精排没过线就不给结论（宁可标"仍开放"，不当伪命中）。
"""
import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Chunk, Citation, Document
from app.research import future_work as fw

DIM = 1024
FUTURE_SENT = ("We leave exploration of sparse attention for future work, "
               "since it may reduce memory substantially.")


def _vec(*head):
    v = np.zeros(DIM, dtype=np.float32)
    for i, x in enumerate(head):
        v[i] = x
    n = float(np.linalg.norm(v)) or 1.0
    return (v / n).astype(np.float32)


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _doc(db, did, title, year):
    d = Document(id=did, filename=f"{did}.pdf", title=title, year=year)
    db.add(d)
    db.commit()
    return d


def _chunk(db, cid, did, text, vec, page=3):
    db.add(Chunk(id=cid, doc_id=did, text=text, kind="text", page=page,
                 bbox="10,20,300,40", embedding=vec.tobytes()))
    db.commit()


def _cite(db, cid, src, dst):
    db.add(Citation(id=cid, src_doc_id=src, dst_doc_id=dst, raw_text="ref",
                    ref_index=1, matched_by="title"))
    db.commit()


@pytest.fixture(autouse=True)
def _clear_cache():
    fw._FW_CACHE.update({"key": None, "data": None, "ts": 0.0})
    yield
    fw._FW_CACHE.update({"key": None, "data": None, "ts": 0.0})


def test_successors_only_counts_incoming_citations(tmp_path):
    db = _session(tmp_path)
    _doc(db, "a", "Source Paper", "2019")
    _doc(db, "b", "Later Paper", "2021")
    _cite(db, "r1", "b", "a")          # b 引用 a
    _cite(db, "r2", "a", "b")          # a 也引用 b（不应互相抵消）

    succ = fw._successors(db)
    assert succ["a"] == ["b"]
    assert succ["b"] == ["a"]


def test_citing_successor_is_marked_successor(tmp_path, monkeypatch):
    db = _session(tmp_path)
    _doc(db, "a", "Sparse Attention Survey", "2019")
    _doc(db, "b", "Efficient Attention", "2021")
    _cite(db, "r1", "b", "a")
    # 两边向量方向一致：句级精排必然过线（这里测的是链路与标注，不是阈值）
    _chunk(db, "c1", "a", FUTURE_SENT, _vec(1.0))
    _chunk(db, "c2", "b", "We propose sparse attention that reduces memory by 40%.",
           _vec(1.0))

    monkeypatch.setattr(fw, "embed_texts",
                        lambda texts: np.vstack([_vec(1.0) for _ in texts]))
    out = fw.collect_future_work(db, max_items=10)

    hit = [x for x in out["items"] if x["status"] == "possibly_addressed"]
    assert hit, "应当命中后继文献"
    assert hit[0]["basis"] == "successor"
    assert hit[0]["resolved_by"][0]["doc_id"] == "b"
    assert out["stats"]["with_citing_successor"] >= 1


def test_no_successor_falls_back_to_related(tmp_path, monkeypatch):
    db = _session(tmp_path)
    _doc(db, "a", "Sparse Attention Survey", "2019")     # 没有人引用它
    _doc(db, "d", "Efficient Attention Two", "2021")
    _chunk(db, "c1", "a", FUTURE_SENT, _vec(1.0))
    _chunk(db, "c2", "d", "We propose sparse attention that reduces memory by 40%.",
           _vec(1.0))

    monkeypatch.setattr(fw, "embed_texts",
                        lambda texts: np.vstack([_vec(1.0) for _ in texts]))
    out = fw.collect_future_work(db, max_items=10)

    hit = [x for x in out["items"] if x["status"] == "possibly_addressed"]
    assert hit and hit[0]["basis"] == "related"
    assert out["stats"]["with_citing_successor"] == 0


def test_sentence_rerank_blocks_unrelated_match(tmp_path, monkeypatch):
    """块级召回命中、句级却差很远时不许给结论（防"都提 GPT-3"式伪命中）。"""
    db = _session(tmp_path)
    _doc(db, "a", "Sparse Attention Survey", "2019")
    _doc(db, "b", "Efficient Attention", "2021")
    _cite(db, "r1", "b", "a")
    _chunk(db, "c1", "a", FUTURE_SENT, _vec(1.0))
    _chunk(db, "c2", "b", "We propose sparse attention that reduces memory by 40%.",
           _vec(1.0))

    # 句向量与未来工作句正交 → 句级 cos = 0，必须判开放
    def fake_embed(texts):
        out = []
        for t in texts:
            out.append(_vec(1.0) if "future work" in t.lower() else _vec(0.0, 1.0))
        return np.vstack(out)

    monkeypatch.setattr(fw, "embed_texts", fake_embed)
    out = fw.collect_future_work(db, max_items=10)

    assert out["stats"]["possibly_addressed"] == 0
    assert all(x["status"] == "open" for x in out["items"])


def test_doc_matrix_skips_dimension_mismatch(tmp_path):
    db = _session(tmp_path)
    _doc(db, "a", "Sparse Attention Survey", "2019")
    _chunk(db, "c1", "a", "text", np.ones(8, dtype=np.float32))   # 8 维脏数据

    mat, chunks = fw._doc_matrix(db, "a", DIM, offline=False, cache={})
    assert mat is None and chunks == []
