"""统一大模型网关。

设计要点（答辩可讲）：
1. 只用 OpenAI 兼容协议（/chat/completions），所以 DeepSeek / 通义 / 智谱 / 本地 vLLM 全都能接；
2. 本地 Ollama 作为可切换/兜底后端，呼应"本地模型部署加分"；
3. 没有密钥时进入 mock 模式，返回明确标注的假响应——保证断网或评审环境整条链路不崩；
4. 所有调用带重试、超时、token 统计与结构化日志，失败向上抛 LLMError 由 API 层转成友好提示。
"""
from __future__ import annotations

import json
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence

import httpx

from app.core import runtime
from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)


# 上游的几类"不是代码问题、但必须让用户看懂"的错误。
# 直接把原始 400 抛到界面上，用户看到的是一大段英文 JSON，第一反应是"系统崩了"；
# 而真实原因往往是账户欠费或额度用尽——这类问题充值就能解决，说清楚能省一轮沟通。
_UPSTREAM_HINTS = (
    ("arrearage", "账户欠费", "模型账户欠费（如阿里云百炼的 Arrearage），请充值后重试"),
    ("access denied", "账户异常", "模型账户状态异常或被拒绝访问，请检查账户余额与模型开通状态"),
    ("insufficient", "额度不足", "账户额度不足，请充值或换用其它模型"),
    ("rate limit", "触发限流", "上游模型触发限流，请稍后重试"),
    ("invalid_api_key", "密钥无效", "API Key 无效，请在后台「云端模型」里更新"),
)


def upstream_hint(text: str) -> str:
    low = (text or "").lower()
    for needle, _, cn in _UPSTREAM_HINTS:
        if needle in low:
            return cn
    return ""


class LLMError(RuntimeError):
    """模型调用失败（网络/鉴权/额度/格式）。"""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMResult:
    text: str
    model: str
    route: str
    latency_ms: int = 0
    usage: Usage = field(default_factory=Usage)


MOCK_HINT = "【模拟模式】未配置 LLM_API_KEY，以下内容不是真实模型输出。"


def _mock_reply(json_mode: bool) -> str:
    if json_mode:
        return json.dumps(
            {
                "answer": MOCK_HINT + "请在 .env 中填入 LLM_API_KEY 后重试。",
                "citations": [],
                "confidence": "low",
            },
            ensure_ascii=False,
        )
    return MOCK_HINT + "请在 .env 中配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 后重试。"


def want_thinking(override: bool | None = None) -> bool:
    """本次调用要不要思维链：请求里带了就用请求的，否则跟随后台全局设置。

    前端「快速 / 深思」按钮走这里。它只是覆盖**这一次**提问，不改全局——
    否则用户在页面上点一下，后台配置就被悄悄改掉，下次别人进来还以为系统变慢了。
    """
    return bool(settings.llm_thinking) if override is None else bool(override)


# ---------------- 请求级路由覆盖（"这一问走本地"） ----------------
# 为什么用 contextvar 而不是直接改 settings：
# settings 是进程级全局状态。界面上的一个按钮若写它，等于**一个人点一下，全站
# 所有人的模型都被换掉**；公网挂的就是这个进程，那更是一个无鉴权的写接口。
# 覆盖只活在这一次 HTTP 请求的上下文里，随响应结束消失，并发之间天然隔离。
_ROUTE_OVERRIDE: ContextVar[str | None] = ContextVar("sciagent_route_override", default=None)


def set_route_override(value: str | None):
    """设置本次请求的路由覆盖，返回 token 供 reset。非法值等同"没指定"。"""
    v = (value or "").strip().lower()
    return _ROUTE_OVERRIDE.set(v if v in {"local", "cloud"} else None)


def reset_route_override(token) -> None:
    try:
        _ROUTE_OVERRIDE.reset(token)
    except (LookupError, ValueError):
        # 覆盖是在中间件里 set 的，而流式响应可能在别的线程收尾；
        # reset 失败最多是"这份上下文多留了一个值"，绝不能因此打断响应。
        pass


def route_override() -> str | None:
    return _ROUTE_OVERRIDE.get()


class RouteOverrideMiddleware:
    """把请求头 ``X-LLM-Route`` 变成"仅本次请求有效"的路由覆盖。

    为什么是纯 ASGI 中间件，而不是给问答端点加一个参数：真正调模型的地方在
    ``app/agents/nodes.py`` 深处，一路把 route 传下去要改十几个函数签名，
    还会漏掉以后新增的调用点。contextvar 在这里能跨线程生效靠的是：它在
    本中间件里 set，之后线程池任务、流式分块迭代都是从这份上下文复制出去的，
    所以 ``/api/chat/stream`` 的每个 token 批次也看得见同一个值。
    """

    def __init__(self, app) -> None:  # noqa: ANN001 纯 ASGI 约定
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        raw = ""
        for key, val in scope.get("headers") or []:
            if key == b"x-llm-route":
                raw = val.decode("latin-1")
                break
        token = set_route_override(raw)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_route_override(token)


_LOCAL_PROBE: dict = {"at": 0.0, "payload": None}


def local_model_status(max_age: float = 10.0) -> dict:
    """本地 Ollama 到底能不能用（带短缓存，避免每次打开页面都去探端口）。

    判据三条缺一不可：开关打开、``/api/tags`` 可达、配置的模型名真的在列表里。
    第三条最容易漏——``OLLAMA_MODEL`` 指向一个本机不存在的名字时，接口通、
    列表也非空，但一推理就 404，前端点下去是"转半天然后报错"。
    这次就是这么坏的：``.env`` 还写着 ``qwen2.5:3b``，本机只剩自训模型。
    """
    now = time.time()
    cached = _LOCAL_PROBE["payload"]
    if cached is not None and now - float(_LOCAL_PROBE["at"] or 0) < max_age:
        return cached

    out = {
        "enabled": bool(settings.ollama_enabled),
        "url": settings.ollama_base_url,
        "model": settings.ollama_model,
        "available": False,
        "reachable": None,
        "latency_ms": None,
        "models": [],
        "reason": "",
    }
    if not out["enabled"]:
        out["reason"] = "本机未启用 Ollama（OLLAMA_ENABLED=0）"
    else:
        t0 = time.time()
        try:
            with httpx.Client(timeout=2.5) as c:
                r = c.get(settings.ollama_base_url.rstrip("/") + "/api/tags")
            out["latency_ms"] = int((time.time() - t0) * 1000)
            out["reachable"] = r.status_code == 200
            names = [m.get("name") for m in ((r.json() or {}).get("models") or [])]
            out["models"] = names[:12]
            model = settings.ollama_model
            if r.status_code != 200:
                out["reason"] = f"Ollama 返回 HTTP {r.status_code}"
            elif model not in names and f"{model}:latest" not in names:
                out["reason"] = f"本机没有模型 {model}"
            else:
                out["available"] = True
        except Exception as exc:  # noqa: BLE001 探测失败只如实说明，不影响主流程
            out["reachable"] = False
            out["error"] = str(exc)[:120]
            out["reason"] = f"连不上 {settings.ollama_base_url}"

    _LOCAL_PROBE["at"] = now
    _LOCAL_PROBE["payload"] = out
    return out


class LLMGateway:
    """对外唯一入口：chat() / chat_stream() / chat_json()。"""

    def __init__(self) -> None:
        self._client: httpx.Client | None = None

    # ---------------- 内部工具 ----------------
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=settings.llm_timeout)
        return self._client

    def reset(self) -> None:
        """丢弃缓存的 httpx 客户端。

        连接池里存的是**建池时的超时**，后台把 llm_timeout 从 90 改成 30 后，
        不重建的话实际还在用 90 秒——配置生效了，行为没变，最难查。
        """
        try:
            if self._client is not None:
                self._client.close()
        except Exception:  # noqa: BLE001
            pass
        self._client = None

    def route(self) -> str:
        """决定本次走 cloud / local / mock。

        优先级：强制模拟 > 请求级覆盖 > 全局路由 > 缺 key 兜底。

        `llm_mock` 必须排在最前（D-77）：它的说明写的是"完全不调模型，用固定文案
        占位"，但原来它和"没 key"共用一条分支，于是 `ollama_enabled=true` 时被
        改判成 local——本机 Ollama 没开，用户按下"模拟模式"反而每次问答都 502。
        本意是"演示时别依赖网络"，结果又依赖了另一个更不稳定的东西。

        请求级覆盖（`X-LLM-Route`）排在全局之前，但它**只能指向真的存在的东西**：
        要 local 而本机没开 Ollama，就退回全局策略，而不是硬着头皮 502。
        """
        if settings.llm_mock:
            return "mock"

        # 离线模式优先于一切（除了显式 mock）：它的定义就是"不发任何网络请求"，
        # 请求头里带不带 cloud 都不该把流量导出去。本机没装模型时仍然走 mock 分支，
        # 因为 route 末尾会按 ollama_enabled 兜底，不会硬发请求。
        if runtime.is_offline():
            if settings.ollama_enabled:
                return "local"
            log.warning("离线模式开启，但 OLLAMA_ENABLED 未打开，按 mock 兜底")
            return "mock"

        forced = _ROUTE_OVERRIDE.get()
        if forced == "local":
            if settings.ollama_enabled:
                return "local"
            log.warning("请求指定本地模型，但 OLLAMA_ENABLED 未打开，按全局策略继续")
        elif forced == "cloud" and settings.llm_api_key:
            return "cloud"

        if settings.llm_route == "local" and settings.ollama_enabled:
            return "local"
        if not settings.llm_api_key:
            return "local" if settings.ollama_enabled else "mock"
        return "cloud"

    def describe(self) -> dict:
        r = self.route()
        model = settings.ollama_model if r == "local" else settings.llm_model
        return {
            "route": r,
            "model": model if r != "mock" else "mock",
            "base_url": settings.ollama_base_url if r == "local" else settings.llm_base_url,
            "mock": r == "mock",
        }

    # ---------------- 调用 ----------------
    def chat(
        self,
        messages: Sequence[dict],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        retries: int = 2,
        thinking: bool | None = None,
    ) -> LLMResult:
        route = self.route()
        started = time.time()

        if route == "mock":
            return LLMResult(_mock_reply(json_mode), "mock", "mock", int((time.time() - started) * 1000))

        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                if route == "local":
                    res = self._chat_ollama(messages, json_mode, temperature, max_tokens)
                else:
                    res = self._chat_cloud(messages, json_mode, temperature, max_tokens, model, thinking)
                res.latency_ms = int((time.time() - started) * 1000)
                log.info(
                    "LLM route=%s model=%s latency=%dms tokens=%d",
                    res.route, res.model, res.latency_ms, res.usage.total,
                )
                return res
            except Exception as exc:  # noqa: BLE001 统一兜底重试
                last_err = exc
                log.warning("LLM 调用失败(第 %d 次): %s", attempt + 1, exc)
                if attempt < retries:
                    time.sleep(1.2 * (attempt + 1))

        raise LLMError(f"模型调用失败：{last_err}") from last_err

    def _chat_cloud(
        self,
        messages: Sequence[dict],
        json_mode: bool,
        temperature: float | None,
        max_tokens: int | None,
        model: str | None = None,
        thinking: bool | None = None,
    ) -> LLMResult:
        model_name = model or settings.llm_model
        payload: dict = {
            "model": model_name,
            "messages": list(messages),
            "temperature": settings.llm_temperature if temperature is None else temperature,
            "max_tokens": settings.llm_max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        # DashScope 兼容层认 enable_thinking。只在显式关掉时才发这个字段：
        # 不认这个参数的供应商（或老模型）会直接 400，默认不发就不改变原有行为。
        if not want_thinking(thinking):
            payload["enable_thinking"] = False

        resp = self.client.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code >= 400:
            hint = upstream_hint(resp.text)
            if hint:
                raise LLMError(f"{hint}（HTTP {resp.status_code}）")
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        u = data.get("usage") or {}
        return LLMResult(
            text=text.strip(),
            model=data.get("model", settings.llm_model),
            route="cloud",
            usage=Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0)),
        )

    def _chat_ollama(
        self,
        messages: Sequence[dict],
        json_mode: bool,
        temperature: float | None,
        max_tokens: int | None,
    ) -> LLMResult:
        payload: dict = {
            "model": settings.ollama_model,
            "messages": list(messages),
            "stream": False,
            "options": {
                "temperature": settings.llm_temperature if temperature is None else temperature,
                # 本地不吃云端的 4096：49 tok/s 下那是 80 秒以上
                "num_predict": min(
                    max_tokens if max_tokens is not None else settings.ollama_max_tokens,
                    settings.ollama_max_tokens,
                ),
            },
        }
        if json_mode:
            payload["format"] = "json"

        resp = self.client.post(f"{settings.ollama_base_url.rstrip('/')}/api/chat", json=payload)
        if resp.status_code >= 400:
            raise LLMError(f"Ollama HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        text = (data.get("message") or {}).get("content", "") or ""
        return LLMResult(
            text=text.strip(),
            model=data.get("model", settings.ollama_model),
            route="local",
            usage=Usage(data.get("prompt_eval_count", 0), data.get("eval_count", 0)),
        )

    # ---------------- 流式 ----------------
    def chat_stream(self, messages: Sequence[dict], *, model: str | None = None,
                    thinking: bool | None = None) -> Iterator[tuple[str, str]]:
        """逐段产出 ``(kind, text)``：``kind="reason"`` 是思维链，``kind="text"`` 是正文。

        为什么要区分：深思模式下推理模型会先吐 ``reasoning_content``。如果把它当成
        正文推给前端，用户看到的是"先说一堆废话、最后再重说一遍答案"。分开之后前端
        可以把推理片段显示成"正在推理…"，正文照常逐字输出。
        """
        route = self.route()
        model_name = model or settings.llm_model
        # 流式是全站默认路径，原来一行日志都没有——于是"这次走的是云端还是本地"
        # 只能靠耗时猜。请求级切换上线后，这条日志就是"按钮真的生效了"的唯一凭据。
        log.info("LLM stream route=%s model=%s",
                 route, settings.ollama_model if route == "local" else model_name)
        if route == "mock":
            text = _mock_reply(False)
            for i in range(0, len(text), 12):
                yield "text", text[i : i + 12]
            return

        if route == "local":
            url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
            payload = {
                "model": settings.ollama_model,
                "messages": list(messages),
                "stream": True,
                # 必须显式给 num_predict：不传时 Ollama 会一直生成到上下文上限，
                # 本地 3B 因此出现过 76–102 秒才出答案（云端 4096 被继承过来的后果）。
                "options": {
                    "temperature": settings.llm_temperature,
                    "num_predict": settings.ollama_max_tokens,
                },
            }
            headers = {}
        else:
            url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
            payload = {
                "model": model_name,
                "messages": list(messages),
                "temperature": settings.llm_temperature,
                "stream": True,
            }
            if not want_thinking(thinking):
                payload["enable_thinking"] = False
            headers = {"Authorization": f"Bearer {settings.llm_api_key}"}

        try:
            with self.client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    raise LLMError(f"HTTP {resp.status_code}")
                for line in resp.iter_lines():
                    if not line:
                        continue
                    if route == "local":
                        obj = json.loads(line)
                        msg = obj.get("message") or {}
                        if msg.get("thinking"):
                            yield "reason", msg["thinking"]
                        piece = msg.get("content", "")
                        if piece:
                            yield "text", piece
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    if line.strip() == "[DONE]":
                        break
                    obj = json.loads(line)
                    delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                    if delta.get("reasoning_content"):
                        yield "reason", delta["reasoning_content"]
                    piece = delta.get("content") or ""
                    if piece:
                        yield "text", piece
        except Exception as exc:  # noqa: BLE001
            log.warning("流式调用失败，降级为整体返回: %s", exc)
            try:
                yield "text", self.chat(messages, model=model_name, thinking=thinking).text
            except LLMError as e:
                yield "text", f"[服务异常] {e}"

    # ---------------- Function Calling ----------------
    def function_call(
        self,
        messages: Sequence[dict],
        tools: Sequence[dict],
        *,
        model: str | None = None,
        retries: int = 1,
    ) -> dict | None:
        """让模型按 OpenAI tools 协议选择工具，返回 {name, arguments} 或 None。

        用独立的轻量模型做路由：主模型是推理模型，工具选择这种结构化决策
        交给它既慢又容易把思维链写进 arguments。
        """
        route = self.route()
        if route == "mock":
            return None
        payload = {
            "model": model or settings.trans_model or settings.llm_model,
            "messages": list(messages),
            "tools": list(tools),
            "tool_choice": "auto",
            "temperature": 0.1,
            "max_tokens": 512,
            "stream": False,
        }
        for attempt in range(retries + 1):
            try:
                if route == "local":
                    resp = self.client.post(
                        f"{settings.ollama_base_url.rstrip('/')}/api/chat",
                        json={"model": settings.ollama_model, "messages": list(messages),
                              "tools": list(tools), "stream": False})
                else:
                    resp = self.client.post(
                        f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {settings.llm_api_key}",
                                 "Content-Type": "application/json"},
                        json=payload)
                if resp.status_code >= 400:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                msg = (resp.json().get("choices") or [{}])[0].get("message") or {}
                tc = (msg.get("tool_calls") or [None])[0]
                if not tc:
                    return None
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
                return {"name": fn.get("name"), "arguments": args}
            except Exception as exc:  # noqa: BLE001 由上层降级到关键词路由
                log.warning("Function Calling 决策失败（第 %d 次）：%s", attempt + 1, exc)
                if attempt < retries:
                    time.sleep(0.4)
        return None

    # ---------------- JSON 便捷封装 ----------------
    def chat_json(
        self,
        messages: Sequence[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        thinking: bool | None = None,
    ) -> dict:
        """要求模型输出 JSON，并做容错解析（剥离 ``` 包裹、截取首尾花括号）。"""
        res = self.chat(messages, json_mode=True, temperature=temperature, max_tokens=max_tokens,
                        model=model, thinking=thinking)
        return parse_json_loose(res.text)


def parse_json_loose(text: str) -> dict:
    """宽松 JSON 解析：模型偶尔会包代码块或加前言，这里做兜底抽取。"""
    if not text:
        return {}
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {"data": obj}
    except json.JSONDecodeError:
        pass

    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            return obj if isinstance(obj, dict) else {"data": obj}
        except json.JSONDecodeError:
            pass
    return {"_raw": raw, "_parse_error": True}


gateway = LLMGateway()
