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
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence

import httpx

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)


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

    def route(self) -> str:
        """决定本次走 cloud / local / mock。"""
        if settings.llm_route == "local" and settings.ollama_enabled:
            return "local"
        if settings.llm_mock or not settings.llm_api_key:
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
        retries: int = 2,
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
                    res = self._chat_cloud(messages, json_mode, temperature, max_tokens)
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
    ) -> LLMResult:
        payload: dict = {
            "model": settings.llm_model,
            "messages": list(messages),
            "temperature": settings.llm_temperature if temperature is None else temperature,
            "max_tokens": settings.llm_max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        resp = self.client.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code >= 400:
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
                "num_predict": settings.llm_max_tokens if max_tokens is None else max_tokens,
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
    def chat_stream(self, messages: Sequence[dict]) -> Iterator[str]:
        """逐段产出文本，用于前端 SSE 的"流式思考"。"""
        route = self.route()
        if route == "mock":
            text = _mock_reply(False)
            for i in range(0, len(text), 12):
                yield text[i : i + 12]
            return

        if route == "local":
            url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
            payload = {
                "model": settings.ollama_model,
                "messages": list(messages),
                "stream": True,
                "options": {"temperature": settings.llm_temperature},
            }
            headers = {}
        else:
            url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
            payload = {
                "model": settings.llm_model,
                "messages": list(messages),
                "temperature": settings.llm_temperature,
                "stream": True,
            }
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
                        piece = (obj.get("message") or {}).get("content", "")
                    else:
                        if line.startswith("data: "):
                            line = line[6:]
                        if line.strip() == "[DONE]":
                            break
                        obj = json.loads(line)
                        piece = (obj.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                    if piece:
                        yield piece
        except Exception as exc:  # noqa: BLE001
            log.warning("流式调用失败，降级为整体返回: %s", exc)
            try:
                yield self.chat(messages).text
            except LLMError as e:
                yield f"[服务异常] {e}"

    # ---------------- JSON 便捷封装 ----------------
    def chat_json(
        self,
        messages: Sequence[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """要求模型输出 JSON，并做容错解析（剥离 ``` 包裹、截取首尾花括号）。"""
        res = self.chat(messages, json_mode=True, temperature=temperature, max_tokens=max_tokens)
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
