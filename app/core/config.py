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
    # 默认关闭：没有 key 时 gateway.route() 本身就会退回 mock，不需要这个开关兜底。
    # 而它默认开着会让"后台恢复默认"顺手把真实调用切成模拟回复，是个隐蔽的坑。
    llm_mock: int = 0

    # ---------- 翻译模型（中英对照阅读专用）----------
    # 单独配一个**非推理**模型：主模型若带思维链，逐段翻译会被推理过程吃掉
    # 输出预算，大面积返回空串（实测 deepseek-v4.1-flash 必现）。
    # 选 flash 而不是 plus：同样质量下吞吐 120 tok/s vs 43 tok/s，
    # 一页正文的译完时间从 20 秒降到 5 秒，这个差别直接决定"能不能边滚边读"。
    trans_model: str = "qwen-flash"
    trans_batch: int = 4          # 一次请求译几段
    trans_workers: int = 3        # 一页内的批量任务并发数
    trans_max_pages: int = 60     # 超过这个页数不做"整篇翻译"，只按需翻

    # ---------- 写作 Copilot ----------
    # 与翻译同因：写作用主模型（带思维链）会偶发返回空 JSON——推理过程把
    # max_tokens 吃光，正文一个字都没吐出来（实测复现过：四段全空、title 退回 idea）。
    # 写作不需要长链推理，用非推理模型既稳定又快（120 tok/s）。
    write_model: str = "qwen-flash"
    write_max_tokens: int = 6000  # 四段合计约 1600–2400 token，留足余量

    # ---------- 视觉模型（扫描件 OCR）----------
    vlm_base_url: str = ""
    vlm_api_key: str = ""
    vlm_model: str = ""

    # ---------- 本地模型 ----------
    ollama_enabled: int = 0
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:3b"
    llm_route: str = "auto"
    # 是否让云端推理模型"先想再答"。开着答案更稳，但思维链本身要几百 token、
    # 几秒钟；关掉后同一道题 2.3s → 1.4s，答案更短但结论不变（实测见
    # docs/主模型切换测试.md）。默认 1 保持原行为，只有显式设 0 才发这个参数。
    llm_thinking: int = 1

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

    # ---------- 保活 ----------
    # 每 5 分钟一次心跳：刷新 DB 连接 + 让本地模型常驻显存（Ollama 默认 5 分钟卸载）
    keepalive_enabled: int = 1
    keepalive_interval: int = 300      # 秒
    keepalive_warm_model: int = 1      # 本地路由下预热 Ollama
    keepalive_warm_cloud: int = 0      # 云端预热会产生 token 费用，默认关

    # ---------- 论文追踪（官方 1.4.2 定期刷新）----------
    # 默认关：开启后会周期调 arXiv 与逐篇生成推荐理由，没配 key 的机器只会刷错误日志。
    # 演示时在后台打开，或 .env 里 DISCOVER_WATCH_ENABLED=1。
    discover_watch_enabled: int = 0
    discover_watch_interval: int = 21600   # 6 小时扫一次
    discover_watch_days: int = 7           # 只看最近 N 天提交的新论文

    # ---------- 服务 ----------
    port: int = 8000
    data_dir: str = "./data"
    max_upload_mb: int = 120
    log_level: str = "INFO"
    demo_access_token: str = ""
    # 后台控制台口令：只从环境变量读，不进数据库、不回显。留空 = 不鉴权（仅本机开发）
    admin_password: str = ""

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
