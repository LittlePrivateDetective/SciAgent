"""数据模型：元数据 / 检索块 / 章节树 / 引用边 / 任务轨迹 / 反思记忆。

设计说明：
- 元数据用 SQLite，向量默认也落在这里（chunks.embedding），保证零额外服务也能跑；
  若启用 Chroma，则向量另存一份，SQLite 仍是唯一事实来源（Source of Truth）。
- 每张表都保留 created_at，方便答辩时展示"真实迭代过程"。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


class Base(DeclarativeBase):
    pass


class Document(Base):
    """一篇入库文献。"""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(24), primary_key=True, default=lambda: new_id("d"))
    filename: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(512), default="")
    authors: Mapped[str] = mapped_column(Text, default="")          # JSON 字符串：["A","B"]
    affiliations: Mapped[str] = mapped_column(Text, default="")     # JSON 字符串
    abstract: Mapped[str] = mapped_column(Text, default="")
    year: Mapped[str] = mapped_column(String(8), default="")
    venue: Mapped[str] = mapped_column(String(255), default="")
    arxiv_id: Mapped[str] = mapped_column(String(64), default="")
    doi: Mapped[str] = mapped_column(String(128), default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    is_scanned: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    family_key: Mapped[str] = mapped_column(String(64), default="", index=True)  # 同族论文（v1/v7）
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)  # 正文 SHA1
    simhash: Mapped[int] = mapped_column(BigInteger, default=0, index=True)        # 64 位内容指纹
    file_path: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(24), default="ready")  # uploaded/parsing/ready/failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    chunks: Mapped[list["Chunk"]] = relationship(back_populates="doc", cascade="all, delete-orphan")
    sections: Mapped[list["Section"]] = relationship(back_populates="doc", cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        import json

        return {
            "id": self.id,
            "filename": self.filename,
            "title": self.title,
            "authors": json.loads(self.authors) if self.authors else [],
            "affiliations": json.loads(self.affiliations) if self.affiliations else [],
            "abstract": self.abstract,
            "year": self.year,
            "venue": self.venue,
            "arxiv_id": self.arxiv_id,
            "doi": self.doi,
            "page_count": self.page_count,
            "char_count": self.char_count,
            "is_scanned": bool(self.is_scanned),
            "version": self.version,
            "family_key": self.family_key,
            "status": self.status,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }


class Chunk(Base):
    """检索单元（citation-ready）：带页码与包围盒，是锚点溯源的物理载体。"""

    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(24), primary_key=True, default=lambda: new_id("c"))
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer, default=0)
    page: Mapped[int] = mapped_column(Integer, default=1, index=True)
    bbox: Mapped[str] = mapped_column(String(96), default="0,0,0,0")   # "x0,y0,x1,y1"
    text: Mapped[str] = mapped_column(Text)
    section_path: Mapped[str] = mapped_column(String(512), default="")
    kind: Mapped[str] = mapped_column(String(16), default="text")      # text/table/figure/formula/ref
    char_len: Mapped[int] = mapped_column(Integer, default=0)
    # 稠密向量：float32 小端字节串；None 表示尚未向量化
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    doc: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (Index("ix_chunk_doc_page", "doc_id", "page"),)

    @property
    def anchor(self) -> str:
        x0, y0, x1, y1 = (self.bbox or "0,0,0,0").split(",")
        return f"[p{self.page}@{int(float(x0))},{int(float(y0))},{int(float(x1))},{int(float(y1))}]"


class Translation(Base):
    """段落译文缓存（中英对照阅读）。

    主键就是 chunk_id：译文与检索块一一对应，不另建切分，
    这样"用户读到的段落"和"答案引用的段落"永远是同一个锚点。
    """

    __tablename__ = "translations"

    chunk_id: Mapped[str] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True
    )
    doc_id: Mapped[str] = mapped_column(String(24), index=True)
    page: Mapped[int] = mapped_column(Integer, default=1, index=True)
    zh: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Section(Base):
    """章节树节点（一/二/三级标题），前端树状导航直接吃这个表。"""

    __tablename__ = "sections"

    id: Mapped[str] = mapped_column(String(24), primary_key=True, default=lambda: new_id("s"))
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(512))
    page: Mapped[int] = mapped_column(Integer, default=1)
    order_idx: Mapped[int] = mapped_column(Integer, default=0)

    doc: Mapped[Document] = relationship(back_populates="sections")


class Citation(Base):
    """引用边：src 引用 dst（dst 未入库时为空，前端画成灰色外节点）。"""

    __tablename__ = "citations"

    id: Mapped[str] = mapped_column(String(24), primary_key=True, default=lambda: new_id("r"))
    src_doc_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    dst_doc_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    raw_text: Mapped[str] = mapped_column(Text, default="")
    ref_index: Mapped[int] = mapped_column(Integer, default=0)
    matched_by: Mapped[str] = mapped_column(String(24), default="")  # doi/arxiv/title/none


class Task(Base):
    """一次 Agent 任务的总体记录，用于轨迹回放与答辩演示。"""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(24), primary_key=True, default=lambda: new_id("t"))
    goal: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(32), default="qa")   # qa/compare/survey/review/write
    status: Mapped[str] = mapped_column(String(24), default="running")
    plan_json: Mapped[str] = mapped_column(Text, default="[]")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    steps_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class TaskStep(Base):
    """任务内单步：哪个 Agent、调了什么工具、耗时多少、是否失败。"""

    __tablename__ = "task_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer, default=0)
    agent: Mapped[str] = mapped_column(String(32), default="")
    tool: Mapped[str] = mapped_column(String(64), default="")
    input_json: Mapped[str] = mapped_column(Text, default="{}")
    output_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="ok")
    error: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Lesson(Base):
    """反思经验库：Reflector 写入，Planner 读取复用——"自主进化"的实体。"""

    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scene: Mapped[str] = mapped_column(String(128), index=True)      # 任务场景，如 qa/去重
    error_type: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(Text, default="")
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    hits: Mapped[int] = mapped_column(Integer, default=0)             # 被复用次数 → 演进曲线
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Memory(Base):
    """长期项目记忆：用户偏好、研究进展等键值对。"""

    __tablename__ = "memories"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), default="profile")
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class LLMCall(Base):
    """模型调用账本：成本与延迟可量化（答辩时可展示）。"""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scene: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    route: Mapped[str] = mapped_column(String(16), default="cloud")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class TrashEntry(Base):
    """回收站：被删除文献的可恢复快照。

    QA 事故复盘（2026-11-11）：原实现 DELETE 无二次确认、无审计、不可恢复，
    一次误触就永久丢掉 7 篇 / 7110 个检索块。现在删除只写快照 + 审计日志，
    源文件始终保留在磁盘，恢复时重新解析即可（低频操作，允许慢）。
    """

    __tablename__ = "trash_entries"

    doc_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(512), default="")
    file_path: Mapped[str] = mapped_column(String(512), default="")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    actor: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(String(255), default="")
    deleted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AuditLog(Base):
    """关键写操作审计：谁、什么时候、对哪篇、做了什么。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    doc_id: Mapped[str] = mapped_column(String(24), default="", index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class RecommendFeedback(Base):
    """推荐反馈：赞 / 踩 / 点开（官方 1.4.3 第 3 点）。

    只记行为，不记用户画像：单用户场景下它就是"这个方向我感兴趣/不感兴趣"的
    显式信号，下一条推荐里 dislike 直接过滤、like 参与排序。
    """

    __tablename__ = "recommend_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    arxiv_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(16), index=True)   # like / dislike / click
    reason: Mapped[str] = mapped_column(String(320), default="")  # 当时的推荐理由，便于复盘
    actor: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class AppSetting(Base):
    """后台参数覆盖：只在「后台改过的项」里存一行，没改过的继续用 .env / 默认值。

    为什么不直接改写 .env：.env 是部署时的单一事实来源，运行时改它会让
    "文件里的值"和"进程里的值"长期不一致，回滚时没人说得清哪个在生效。
    这里把运行期覆盖单独存表，界面上能明确标出"来源：后台 / .env / 默认"。
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")      # JSON 编码，保留类型
    actor: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
