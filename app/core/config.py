"""全局配置：所有可变参数集中在此，密钥只从环境变量读取。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（app/core/config.py -> 上两级）
BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """从 .env 读取配置；字段名自动映射为大写环境变量。"""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 大模型 ----------
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-v4.1-flash"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048
    llm_timeout: int = 90
    llm_mock: int = 1

    # ---------- 视觉模型（扫描件 OCR）----------
    vlm_base_url: str = ""
    vlm_api_key: str = ""
    vlm_model: str = ""

    # ---------- 本地模型 ----------
    ollama_enabled: int = 0
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:3b"
    llm_route: str = "auto"

    # ---------- 向量化 ----------
    emb_provider: str = "hash"
    emb_base_url: str = ""
    emb_api_key: str = ""
    emb_model: str = "BAAI/bge-m3"
    emb_dim: int = 1024
    emb_local_model: str = "BAAI/bge-small-zh-v1.5"

    # ---------- 重排（Rerank）----------
    # auto=有 key 就调重排服务、失败自动退回本地规则重排；api=强制调；none=只用本地规则
    rerank_provider: str = "auto"
    rerank_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = "qwen3.7-text-rerank"

    # ---------- 向量库 ----------
    vector_backend: str = "auto"
    chroma_dir: str = "./data/chroma"

    # ---------- 检索 ----------
    top_k: int = 8
    rerank_top_n: int = 4
    chunk_max_chars: int = 600
    chunk_overlap: int = 80
    hybrid_alpha: float = 0.6

    # ---------- 服务 ----------
    port: int = 8000
    data_dir: str = "./data"
    max_upload_mb: int = 120
    log_level: str = "INFO"
    demo_access_token: str = ""

    # ---------- 派生路径 ----------
    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else (BASE_DIR / p).resolve()

    @property
    def pdf_dir(self) -> Path:
        return self.data_path / "pdf"

    @property
    def upload_dir(self) -> Path:
        return self.data_path / "uploads"

    @property
    def page_dir(self) -> Path:
        return self.data_path / "pages"

    @property
    def export_dir(self) -> Path:
        return self.data_path / "exports"

    @property
    def log_dir(self) -> Path:
        return self.data_path / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_path / "sciagent.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def chroma_path(self) -> Path:
        p = Path(self.chroma_dir)
        return p if p.is_absolute() else (BASE_DIR / p).resolve()

    def ensure_dirs(self) -> None:
        """启动时创建所有必需目录，避免运行期报错。"""
        for d in (
            self.data_path,
            self.pdf_dir,
            self.upload_dir,
            self.page_dir,
            self.export_dir,
            self.log_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()
