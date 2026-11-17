"""回收站彻底删除的行为约束。

用临时库 + 临时文件跑，不碰真实 data/。四个点：
1. 删快照、删源 PDF、写审计；
2. 记录不存在时如实报错，不静默成功；
3. **共用同一份 PDF 时不删文件**（否则删一条会毁掉另一条的恢复能力）；
4. 清空回收站逐条处理并返回计数。
"""
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import AuditLog, Base, Document, TrashEntry
from app.pipeline.ingest import purge_all_trash, purge_document


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _entry(db, tmp_path, doc_id, filename="a.pdf", body=b"%PDF-1.4 demo"):
    f = tmp_path / filename
    f.write_bytes(body)
    db.add(TrashEntry(doc_id=doc_id, filename=filename, title="标题", file_path=str(f),
                      chunk_count=3, page_count=2, actor="ui", reason="user_delete"))
    db.commit()
    return f


def test_purge_removes_snapshot_and_pdf(tmp_path):
    db = _session(tmp_path)
    f = _entry(db, tmp_path, "d1")

    out = purge_document(db, "d1", actor="tester")

    assert out["ok"] is True
    assert out["removed_file"] is True
    assert not f.exists()                      # PDF 真删了
    assert db.get(TrashEntry, "d1") is None     # 快照没了
    log = db.scalars(select(AuditLog).where(AuditLog.action == "purge")).all()
    assert len(log) == 1 and log[0].doc_id == "d1"


def test_purge_unknown_id_reports_error(tmp_path):
    db = _session(tmp_path)
    out = purge_document(db, "not-exist")
    assert out["ok"] is False
    assert "回收站" in out["error"]


def test_purge_keeps_pdf_shared_with_another_entry(tmp_path):
    """同一份 PDF 被两条快照引用时，删第一条不能把文件带走。"""
    db = _session(tmp_path)
    f = _entry(db, tmp_path, "d1")
    db.add(TrashEntry(doc_id="d2", filename="a.pdf", title="标题", file_path=str(f),
                      chunk_count=3, page_count=2, actor="ui", reason="user_delete"))
    db.commit()

    first = purge_document(db, "d1")
    assert first["ok"] is True
    assert first["removed_file"] is False
    assert f.exists(), "还有一条记录引用它，文件必须保留"

    second = purge_document(db, "d2")
    assert second["removed_file"] is True
    assert not f.exists()


def test_purge_all_reports_counts(tmp_path):
    db = _session(tmp_path)
    _entry(db, tmp_path, "d1", "a.pdf")
    _entry(db, tmp_path, "d2", "b.pdf")

    out = purge_all_trash(db)

    assert out["ok"] is True
    assert out["purged"] == 2
    assert out["removed_files"] == 2
    assert out["failed"] == []
    assert db.scalars(select(TrashEntry)).all() == []


def test_purge_all_on_empty_is_not_error(tmp_path):
    db = _session(tmp_path)
    out = purge_all_trash(db)
    assert out["ok"] is True and out["purged"] == 0


# ---------------------------------------------------------------- 事故回归
def test_purge_keeps_pdf_when_library_doc_uses_relative_path(tmp_path, monkeypatch):
    """2026-11-17 事故：Document 存绝对路径、TrashEntry 存相对路径。

    直接比字符串永远不相等，共用检查形同虚设，清空回收站把 24 篇在用文献的
    PDF 一起删了。这里让两边路径写法不同，断言文件必须留下。
    """
    db = _session(tmp_path)
    (tmp_path / "data" / "pdf").mkdir(parents=True)
    pdf = tmp_path / "data" / "pdf" / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 live")

    # 在库文献：绝对路径
    db.add(Document(id="live1", filename="a.pdf", title="在用文献",
                    file_path=str(pdf), status="ok"))
    # 回收站：相对路径（历史快照就是这么存的）
    db.add(TrashEntry(doc_id="old1", filename="a.pdf", title="在用文献",
                      file_path="data/pdf/a.pdf", chunk_count=3, page_count=2,
                      actor="ui", reason="duplicate"))
    db.commit()

    monkeypatch.chdir(tmp_path)
    out = purge_document(db, "old1")

    assert out["ok"] is True
    assert out["removed_file"] is False, "在库文献还在用这份 PDF，不能删"
    assert pdf.exists()
    assert db.get(TrashEntry, "old1") is None   # 快照照样清掉


def test_purge_keeps_pdf_when_same_filename_still_in_library(tmp_path, monkeypatch):
    """路径写法再错一次也不会误删：同名文件还在库，就当它在用。"""
    db = _session(tmp_path)
    other = tmp_path / "elsewhere" / "a.pdf"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"%PDF-1.4 other")
    db.add(Document(id="live2", filename="a.pdf", title="同名", file_path=str(other), status="ok"))
    db.add(TrashEntry(doc_id="old2", filename="a.pdf", title="同名",
                      file_path=str(tmp_path / "gone" / "a.pdf"), chunk_count=1,
                      page_count=1, actor="ui", reason="duplicate"))
    db.commit()

    out = purge_document(db, "old2")
    assert out["removed_file"] is False
    assert other.exists()
