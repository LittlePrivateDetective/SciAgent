"""运行期参数中心：把「散落在 .env 里的几十个开关」变成一个可校验、可审计、
可热更新的注册表。

设计取舍：
1. **默认值不另存一份**——直接读 `Settings.model_fields[key].default`。否则两处
   默认值迟早不一致（改了 config.py 忘了改后台，界面上显示默认 8、实际是 4）。
2. **覆盖值单独存表**（`app_settings`），不写回 .env。界面能明确标出
   "来源：后台 / .env / 默认"，回滚时不会互相打架。
3. **每个参数都标生效方式**：hot（下一次请求就生效）/ nextdoc（只影响之后入库的
   文献）/ reindex（要重建向量）/ restart（要重启进程）。没有这个标注，
   用户会以为改完立刻生效，然后对着没变的检索结果怀疑系统坏了。
4. **密钥永不回传明文**：GET 只给掩码和"是否已配置"，PUT 时空值代表"不修改"。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import BASE_DIR, settings
from app.core.logger import get_logger

log = get_logger(__name__)

GROUPS: list[dict] = [
    {"key": "model", "label": "云端模型", "desc": "主对话模型与网关；改完下一次请求即生效"},
    {"key": "local", "label": "本地模型", "desc": "Ollama 本地推理；与云端互为主备"},
    {"key": "task", "label": "场景模型", "desc": "翻译 / 写作 / 视觉各自可单独指定模型"},
    {"key": "rag", "label": "检索与索引", "desc": "召回、重排、分块与向量化；部分改动要重建索引"},
    {"key": "service", "label": "服务与安全", "desc": "上传上限、日志级别、演示令牌与端口"},
    {"key": "track", "label": "论文追踪", "desc": "定期扫 arXiv 新论文；默认关闭，演示时在后台打开"},
]

SCOPES = {
    "hot": {"label": "立即生效", "tone": "ok"},
    "nextdoc": {"label": "仅影响之后入库", "tone": "info"},
    "reindex": {"label": "需重建索引", "tone": "warn"},
    "restart": {"label": "需重启进程", "tone": "warn"},
}


@dataclass(frozen=True)
class Param:
    key: str
    group: str
    label: str
    kind: str                     # int / float / bool / str / secret / enum
    desc: str
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str = ""
    options: tuple[str, ...] = ()
    scope: str = "hot"
    advanced: bool = False
    placeholder: str = ""

    def default(self) -> Any:
        f = settings.__class__.model_fields.get(self.key)
        return f.default if f is not None else ""

    def to_dict(self) -> dict:
        default = self.default()
        if self.kind == "bool":
            default = bool(default)
        d = {
            "key": self.key,
            "group": self.group,
            "label": self.label,
            "kind": self.kind,
            "desc": self.desc,
            "scope": self.scope,
            "scope_label": SCOPES.get(self.scope, SCOPES["hot"])["label"],
            "advanced": self.advanced,
            "default": "" if self.kind == "secret" else default,
            "min": self.min,
            "max": self.max,
            "step": self.step,
            "unit": self.unit,
            "options": list(self.options),
            "placeholder": self.placeholder,
        }
        return d


P: list[Param] = [
    # ---------------- 云端模型 ----------------
    Param("llm_base_url", "model", "接口地址", "str",
          "OpenAI 兼容的 base_url，例如 https://api.deepseek.com/v1。本地 vLLM 填 http://127.0.0.1:8000/v1",
          placeholder="https://api.deepseek.com/v1"),
    Param("llm_model", "model", "模型名", "str",
          "主对话模型。带思维链的长推理模型在“逐段翻译/写作”场景会吃光输出预算返回空串，"
          "这类任务已在「场景模型」里单独指定非推理模型"),
    Param("llm_api_key", "model", "API Key", "secret",
          "只从服务端读取，界面永不回传明文", placeholder="sk-…"),
    Param("llm_temperature", "model", "temperature", "float",
          "越低越确定。问答/溯源建议 0.1–0.3；写作可到 0.6", 0, 2, 0.05),
    Param("llm_max_tokens", "model", "最大输出 token", "int",
          "单次回答的上限。中文 1 token ≈ 0.6 字，2048 约等于 1200 字", 256, 16384, 64),
    Param("llm_timeout", "model", "请求超时", "int",
          "秒。长文写作/长上下文容易超 60 秒，本地小机器上推理更慢", 10, 300, 5, "秒"),
    Param("llm_route", "model", "路由策略", "enum",
          "auto=有 key 走云端、否则本地/模拟；cloud=强制云端；local=强制本地 Ollama",
          options=("auto", "cloud", "local")),
    Param("llm_thinking", "model", "模型先思考再答", "bool",
          "关掉后同题 2.3s → 1.4s，答案更短、结论不变；复杂推理题会略降。"
          "只有云端模型认这个开关，本地 Ollama 不受影响"),
    Param("llm_mock", "model", "强制模拟模式", "bool",
          "打开后完全不调模型，用固定文案占位。演示网络不稳时用，答辩前记得关", advanced=True),

    # ---------------- 本地模型 ----------------
    Param("ollama_enabled", "local", "启用 Ollama", "bool",
          "本地推理开关。打开且路由为 local 时，所有模型调用走本机 Ollama"),
    Param("ollama_base_url", "local", "Ollama 地址", "str",
          "默认 http://127.0.0.1:11434", placeholder="http://127.0.0.1:11434"),
    Param("ollama_model", "local", "本地模型名", "str",
          "需与本机 ollama list 里的名字一致，例如 qwen2.5:7b-instruct", placeholder="qwen2.5:7b-instruct"),

    # ---------------- 场景模型 ----------------
    Param("trans_model", "task", "翻译模型", "str",
          "中英对照/划词翻译用。建议非推理模型：推理过程会吃掉输出预算，整页译文大面积为空"),
    Param("trans_batch", "task", "翻译批大小", "int",
          "一次请求译几段。调大吞吐高，但单次失败丢的段数也多", 1, 8, 1, "段"),
    Param("trans_workers", "task", "翻译并发", "int",
          "一页内的并发批数。云端建议 3；本地单卡别超过 1–2，否则排队反而更慢", 1, 8, 1),
    Param("trans_max_pages", "task", "整篇翻译页数上限", "int",
          "超过这个页数不提供“整篇翻译”，只按页懒翻译，避免一次点掉几百页额度", 1, 1000, 10, "页"),
    Param("write_model", "task", "写作模型", "str",
          "写作 Copilot 用。同样建议非推理模型"),
    Param("write_max_tokens", "task", "写作输出上限", "int",
          "四段合计约 1600–2400 token，留足余量才不会被截断", 1000, 16000, 500),
    Param("vlm_base_url", "task", "视觉模型地址", "str",
          "扫描件 OCR 用（可选）。空表示不启用，扫描件将没有词级文本层", advanced=True),
    Param("vlm_api_key", "task", "视觉模型 Key", "secret", "与上面的地址配套", advanced=True),
    Param("vlm_model", "task", "视觉模型名", "str", "例如 qwen-vl-max", advanced=True),

    # ---------------- 检索与索引 ----------------
    Param("top_k", "rag", "召回条数 top_k", "int",
          "进入重排前的候选数。调大提高召回但变慢；8 是本项目实测的平衡点", 1, 30, 1, "条"),
    Param("rerank_top_n", "rag", "重排后保留", "int",
          "重排后送给模型的证据条数。超过 5 条时答案会开始拼凑，溯源准确率下降", 1, 20, 1, "条"),
    Param("hybrid_alpha", "rag", "向量权重 α", "float",
          "混合检索里向量的占比，BM25 占 1-α。0.6 偏语义；术语/编号查询可调到 0.3", 0, 1, 0.05),
    Param("chunk_max_chars", "rag", "分块最大字数", "int",
          "一个检索单元的上限。调大上下文更完整但定位更粗（高亮会覆盖更多行）", 200, 2000, 50, "字", "nextdoc"),
    Param("chunk_overlap", "rag", "分块重叠", "int",
          "相邻块的重叠字数。检索/对照阅读会自动剥掉前缀，避免重复计数", 0, 400, 10, "字", "nextdoc"),
    Param("emb_provider", "rag", "向量化方式", "enum",
          "hash=本地哈希（零依赖，用于演示）；api=调嵌入接口；local=本地模型", ("hash", "api", "local"), "reindex"),
    Param("emb_model", "rag", "嵌入模型", "str", "例如 BAAI/bge-m3", "reindex"),
    Param("emb_dim", "rag", "向量维度", "int",
          "必须与模型输出维度一致，否则写入会错位", 256, 4096, 64, "维", "reindex"),
    Param("vector_backend", "rag", "向量库", "enum",
          "auto=能用 Chroma 就用、否则 SQLite；切换后要重建索引", ("auto", "chroma", "sqlite"), "reindex"),
    Param("rerank_provider", "rag", "重排方式", "enum",
          "auto=有 key 调服务、失败退回本地规则；none=只用本地规则", ("auto", "api", "none")),
    Param("rerank_url", "rag", "重排接口地址", "str", "OpenAI 兼容的 rerank 端点", advanced=True),
    Param("rerank_model", "rag", "重排模型名", "str", "", advanced=True),
    Param("rerank_api_key", "rag", "重排 Key", "secret", "", advanced=True),

    # ---------------- 服务与安全 ----------------
    Param("max_upload_mb", "service", "上传大小上限", "int",
          "单个 PDF 的上限。541 页的长书约 20MB，留足余量", 1, 500, 5, "MB"),
    Param("log_level", "service", "日志级别", "enum",
          "排障时开 DEBUG，演示时用 INFO", ("DEBUG", "INFO", "WARNING", "ERROR")),
    Param("demo_access_token", "service", "演示访问令牌", "secret",
          "设置后所有写接口需要带 Authorization: Bearer <token>；留空表示不鉴权"),
    Param("keepalive_enabled", "service", "后台保活", "bool",
          "每 5 分钟一次心跳：刷新数据库连接，并让本地模型留在显存。"
          "关掉后 Ollama 会在空闲 5 分钟时卸掉模型，下一次请求要重新加载"),
    Param("keepalive_interval", "service", "保活间隔", "int",
          "秒。默认 300（5 分钟）。本地模型默认 5 分钟无请求就卸载，"
          "间隔要小于这个值，建议 240–300", 30, 1800, 30, "秒"),
    Param("keepalive_warm_model", "service", "预热本地模型", "bool",
          "本地路由下给 Ollama 发空请求并带 keep_alive=30m，模型常驻显存。"
          "不产生任何 token 费用"),
    Param("keepalive_warm_cloud", "service", "预热云端连接", "bool",
          "云端路由下每轮发一次 1-token 请求保活连接池。一天 288 次、会产生少量费用，"
          "只省掉首次 TLS 握手（100–300ms），默认关", advanced=True),
    Param("port", "service", "服务端口", "int",
          "改完要重启进程才生效；容器里注意同步改端口映射", 1024, 65535, 1, "", "restart"),

    # ---------------- 论文追踪 ----------------
    Param("discover_watch_enabled", "track", "定期追踪新论文", "bool",
          "开着时按下面的间隔扫 arXiv，把符合库内主题的新论文攒成推荐列表。"
          "每轮会调 arXiv 一次 + 逐篇生成中文理由，没配 key 的机器建议关"),
    Param("discover_watch_interval", "track", "追踪间隔", "int",
          "秒。默认 21600（6 小时）。改完下一轮调度就按新间隔走，不用重启",
          300, 259200, 300, "秒"),
    Param("discover_watch_days", "track", "只看最近几天", "int",
          "arXiv 的 submittedDate 窗口。设 7 表示只推最近一周提交的论文；"
          "调大能多推一些，但命中率会下降", 7, 365, 1, "天"),
]

BY_KEY: dict[str, Param] = {p.key: p for p in P}

# 启动快照：本模块在 main 的 startup 回灌覆盖之前就被 import，此刻 settings 里
# 全是 .env / 出厂值。恢复默认要回到**这个**快照，而不是出厂值——
# 否则点了"全部恢复默认"会把 .env 里配的模型名、API Key 一起清成空，
# 服务当场失联（实测：重置后 llm_api_key 变空，必须重启才能恢复）。
BOOT_VALUES: dict[str, Any] = {q.key: getattr(settings, q.key, None) for q in P}


# ---------------------------------------------------------------- 取值 / 写值
def _env_keys() -> set[str]:
    """哪些键在 .env 或环境变量里被显式设置过（用于标注来源）。"""
    keys: set[str] = set()
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                keys.add(line.split("=", 1)[0].strip().upper())
        except OSError:
            pass
    keys.update(k.upper() for k in os.environ)
    return keys


def mask(secret: str) -> str:
    """密钥只回掩码。4 位前缀 + 4 位后缀足够辨认是哪把，又不泄露内容。"""
    s = secret or ""
    if not s:
        return ""
    if len(s) <= 8:
        return "•" * len(s)
    return s[:4] + "•" * 6 + s[-4:]


def coerce(p: Param, raw: Any) -> tuple[Any, str]:
    """类型转换 + 范围校验，返回 (值, 错误信息)。错误信息非空即校验失败。"""
    if p.kind == "bool":
        if isinstance(raw, bool):
            return raw, ""
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}, ""
    if p.kind == "int":
        try:
            v = int(float(raw))
        except (TypeError, ValueError):
            return None, "需要整数"
        if p.min is not None and v < p.min:
            return None, f"不能小于 {int(p.min)}"
        if p.max is not None and v > p.max:
            return None, f"不能大于 {int(p.max)}"
        return v, ""
    if p.kind == "float":
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None, "需要数字"
        if p.min is not None and v < p.min:
            return None, f"不能小于 {p.min}"
        if p.max is not None and v > p.max:
            return None, f"不能大于 {p.max}"
        return round(v, 4), ""
    if p.kind == "enum":
        v = str(raw).strip()
        if v not in p.options:
            return None, "只能是 " + " / ".join(p.options)
        return v, ""
    # str / secret
    v = str(raw).strip()
    if p.kind == "str" and len(v) > 400:
        return None, "太长（上限 400 字符）"
    if p.kind == "secret" and len(v) > 400:
        return None, "太长（上限 400 字符）"
    return v, ""


def get_value(key: str) -> Any:
    return getattr(settings, key, None)


def describe(db: Session | None = None) -> dict:
    """返回界面需要的全部参数：定义 + 当前值 + 来源 + 覆盖时间。"""
    from app.db.models import AppSetting

    envs = _env_keys()
    overrides: dict[str, AppSetting] = {}
    if db is not None:
        for row in db.scalars(select(AppSetting)).all():
            overrides[row.key] = row

    out = []
    for p in P:
        cur = get_value(p.key)
        row = overrides.get(p.key)
        if p.kind == "secret":
            # 密钥一律不回传：value 恒为空，界面靠 masked + is_set 判断"配没配"。
            # 回半截掩码也够用了，但把"是否为空"和"值"分开语义更清楚：
            # value="" 且 is_set=true 就代表"已配置，不回显"。
            value = ""
            is_set = bool(cur)
        elif p.kind == "bool":
            # Settings 里这些字段是 int（0/1，便于环境变量），但界面开关给的是布尔。
            # 直接回 0 会让前端序列化成 "0"，与布尔 false 的 "false" 不等 →
            # 一打开后台就显示"已修改 1 项"，还能把 false 存进库（实测踩到）。
            value = bool(cur)
            is_set = bool(cur)
        else:
            value = cur
            is_set = bool(cur)
        source = "db" if row else ("env" if p.key.upper() in envs else "default")
        d = p.to_dict()
        d.update({
            "value": value,
            "is_set": is_set,
            "source": source,
            "masked": mask(str(cur or "")) if p.kind == "secret" else "",
            "updated_at": row.updated_at.strftime("%m-%d %H:%M") if row else "",
            "actor": row.actor if row else "",
        })
        out.append(d)
    return {
        "groups": GROUPS,
        "params": out,
        "scopes": SCOPES,
        "meta": {
            "total": len(out),
            "overridden": len(overrides),
            "advanced": sum(1 for p in P if p.advanced),
        },
    }


def apply_value(key: str, value: Any) -> None:
    """把值写进 settings，并对需要重建的资源做失效处理。"""
    p = BY_KEY.get(key)
    if p is None:
        raise KeyError(key)
    setattr(settings, key, value)
    if key in {"llm_base_url", "llm_api_key", "llm_timeout"}:
        # httpx.Client 建的时候就把超时固定了，改配置不重建就一直用旧值
        try:
            from app.core.llm import gateway

            gateway.reset()
        except Exception:  # noqa: BLE001 重建失败不影响配置本身
            log.warning("模型网关重建失败，超时/地址可能仍是旧值", exc_info=True)
    if key == "log_level":
        try:
            from app.core.logger import setup_logging

            setup_logging()
        except Exception:  # noqa: BLE001
            log.warning("日志级别热更新失败", exc_info=True)


def save(db: Session, values: dict[str, Any], actor: str = "admin") -> dict:
    """校验全部通过后才写库，避免"改 10 项成功 7 项"的中间态。"""
    from app.db.models import AppSetting

    rejected: list[dict] = []
    staged: list[tuple[Param, Any]] = []
    for key, raw in (values or {}).items():
        p = BY_KEY.get(key)
        if p is None:
            rejected.append({"key": key, "error": "未知参数"})
            continue
        if p.kind == "secret" and (raw is None or str(raw) == ""):
            continue                      # 空值 = 不修改，避免把 key 清空
        v, err = coerce(p, raw)
        if err:
            rejected.append({"key": key, "error": err})
            continue
        staged.append((p, v))

    if rejected:
        return {"ok": False, "rejected": rejected, "applied": []}

    applied = []
    for p, v in staged:
        row = db.get(AppSetting, p.key)
        # 值改回"不覆盖时本来就该是"的值（启动快照，可能是 .env 或默认值）→ 删掉覆盖行。
        # 否则把参数改回原值后，后台仍显示"已覆盖 1 项"，来源也标成"后台覆盖"，
        # 而实际效果与从未改过完全一样。这条是被回归脚本的 overridden==0 断言抓出来的。
        if p.kind != "secret" and v == BOOT_VALUES.get(p.key, p.default()):
            if row is not None:
                db.delete(row)
            apply_value(p.key, v)
            applied.append({"key": p.key, "value": v, "scope": p.scope, "reverted": True})
            continue
        payload = json.dumps(v, ensure_ascii=False)
        if row is None:
            db.add(AppSetting(key=p.key, value=payload, actor=actor))
        else:
            row.value = payload
            row.actor = actor
            row.updated_at = datetime.now()
        apply_value(p.key, v)
        applied.append({"key": p.key, "value": mask(str(v)) if p.kind == "secret" else v,
                        "scope": p.scope})
    if applied:
        db.commit()
        log.info("后台参数更新：%s", ", ".join(a["key"] for a in applied))
    return {"ok": True, "rejected": [], "applied": applied}


def reset(db: Session, keys: list[str] | None = None) -> dict:
    """恢复默认：删掉覆盖行，把 settings 写回默认值（.env 里的值不下沉）。"""
    from app.db.models import AppSetting

    targets = [BY_KEY[k] for k in keys if k in BY_KEY] if keys else list(P)
    reset_keys = []
    values: dict[str, Any] = {}
    for p in targets:
        row = db.get(AppSetting, p.key)
        if row is not None:
            db.delete(row)
        boot = BOOT_VALUES.get(p.key, p.default())
        apply_value(p.key, boot)
        reset_keys.append(p.key)
        values[p.key] = mask(str(boot or "")) if p.kind == "secret" else boot
    db.commit()
    return {"ok": True, "reset": reset_keys, "values": values}


def load_overrides(db: Session) -> int:
    """进程启动时把库里保存的覆盖值写回 settings。"""
    from app.db.models import AppSetting

    n = 0
    for row in db.scalars(select(AppSetting)).all():
        p = BY_KEY.get(row.key)
        if p is None:
            continue
        try:
            v, err = coerce(p, json.loads(row.value))
        except (json.JSONDecodeError, TypeError):
            log.warning("参数覆盖值解析失败，跳过：%s", row.key)
            continue
        if err:
            log.warning("参数覆盖值不合法，跳过：%s (%s)", row.key, err)
            continue
        apply_value(p.key, v)
        n += 1
    if n:
        log.info("已应用 %d 项后台参数覆盖", n)
    return n
