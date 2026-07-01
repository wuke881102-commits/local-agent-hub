"""LLM 客户端 — 同时支持两种 provider：

- ``openai_compatible``：直接用 ``AsyncOpenAI``，``base_url`` 指向 OpenAI 兼容端点。
  典型场景：阿里百炼 / DashScope（``https://dashscope.aliyuncs.com/compatible-mode/v1``），
  vLLM 自托管，硅基流动，OpenAI 官方。
- ``azure``：用 ``AsyncAzureOpenAI``，需要 ``azure_endpoint``、``api_version``、``api_key``。
  ``model=`` 传 deployment 名（不是模型名）。

文本与视觉两个客户端独立配置（PRD §11.2 — qwen 与 gpt 通常分别托管）。
任一 provider 配置缺失或仍是占位值，对应客户端自动进入 mock 模式。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from typing import AsyncIterator

from ..config import settings

log = logging.getLogger("llm.client")

try:
    from openai import AsyncOpenAI, AsyncAzureOpenAI
    _OPENAI_AVAILABLE = True
except Exception:  # noqa: BLE001
    _OPENAI_AVAILABLE = False
    AsyncOpenAI = None  # type: ignore[assignment]
    AsyncAzureOpenAI = None  # type: ignore[assignment]


class LLMError(RuntimeError):
    def __init__(self, message: str, model: str | None = None, retriable: bool = False):
        super().__init__(message)
        self.model = model
        self.retriable = retriable


def _is_placeholder(s: str) -> bool:
    if not s:
        return True
    s_lower = s.lower()
    placeholders = ("your-", "your_", "sk-your", "...", "<", "replace", "todo", "xxx", "example")
    return any(p in s_lower for p in placeholders)


def _build_client(provider: str, *, api_key: str, base_url: str, azure_endpoint: str, api_version: str, timeout: float):
    """根据 provider 实例化对应的 async 客户端，或返回 None 表示走 mock。"""
    if not _OPENAI_AVAILABLE:
        return None
    if _is_placeholder(api_key):
        return None

    # max_retries=0 disables the OpenAI SDK's built-in retry loop; we own retries
    # at a higher level so users see a hard failure instead of a 5-minute hang.
    if provider == "azure":
        if _is_placeholder(azure_endpoint):
            return None
        return AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=azure_endpoint,
            api_version=api_version or "2024-12-01-preview",
            timeout=timeout,
            max_retries=0,
        )

    # openai_compatible（含 OpenAI 官方、DashScope、vLLM 等）
    if _is_placeholder(base_url):
        return None
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=0,
    )


class LLMClient:
    def __init__(self) -> None:
        self.text_provider = settings.text_model_provider
        self.text_model = settings.text_model
        self.text_model_fast = settings.text_model_fast
        self.text_model_best = settings.text_model_best
        self.vision_provider = settings.vision_model_provider
        self.vision_model = settings.vision_model
        self.image_provider = settings.image_model_provider
        self.image_model = settings.image_model

        self._text_client = _build_client(
            self.text_provider,
            api_key=settings.text_model_api_key,
            base_url=settings.text_model_base_url,
            azure_endpoint=settings.text_model_azure_endpoint,
            api_version=settings.text_model_api_version,
            timeout=60,
        )
        self._vision_client = _build_client(
            self.vision_provider,
            api_key=settings.vision_model_api_key,
            base_url=settings.vision_model_base_url,
            azure_endpoint=settings.vision_model_azure_endpoint,
            api_version=settings.vision_model_api_version,
            timeout=90,
        )
        # 生图未单独配 key 时，自动复用 vision 的 key（GPT-Image-1 常与 vision 同处一个
        # Azure 资源；Azure key 是资源级的，跨 deployment 通用）。仅在 endpoint 相同时复用。
        _image_key = settings.image_model_api_key
        if _is_placeholder(_image_key) and self.image_provider == "azure" and \
           settings.image_model_azure_endpoint.rstrip("/") == settings.vision_model_azure_endpoint.rstrip("/"):
            _image_key = settings.vision_model_api_key
        self._image_client = _build_client(
            self.image_provider,
            api_key=_image_key,
            base_url=settings.image_model_base_url,
            azure_endpoint=settings.image_model_azure_endpoint,
            api_version=settings.image_model_api_version,
            timeout=180,
        )

        # 任一为 None 即对应能力降级到 mock；text mock 决定整体行为
        self._text_mock = self._text_client is None
        self._vision_mock = self._vision_client is None
        self._image_mock = self._image_client is None
        self._mock = self._text_mock and self._vision_mock

        log.info(
            "LLMClient: text=%s/%s (fast=%s best=%s mock=%s)  vision=%s/%s (mock=%s)  image=%s/%s (mock=%s)",
            self.text_provider, self.text_model, self.text_model_fast, self.text_model_best, self._text_mock,
            self.vision_provider, self.vision_model, self._vision_mock,
            self.image_provider, self.image_model, self._image_mock,
        )

    @property
    def image_available(self) -> bool:
        """生图是否已配置（未配置时调用方应走占位降级，而不是报错）。"""
        return self._image_client is not None

    @property
    def mock(self) -> bool:
        # 暴露给诊断：只要文本走 mock 就显示 mock（HTML Agent 主要靠文本）
        return self._text_mock

    async def ping(self) -> dict:
        result = {
            "text":   {"ok": False, "model": self.text_model,   "provider": self.text_provider,   "mock": self._text_mock},
            "vision": {"ok": False, "model": self.vision_model, "provider": self.vision_provider, "mock": self._vision_mock},
            # 生图只报配置状态，不做 live 调用（images.generate 会真扣费）。
            "image":  {"ok": not self._image_mock, "model": self.image_model, "provider": self.image_provider, "mock": self._image_mock},
            "mock":   self._text_mock,
        }

        async def _one(client, model, key):
            if client is None:
                result[key]["ok"] = True
                result[key]["latency_ms"] = 0
                return
            t = time.perf_counter()
            try:
                # 探活超时：qwen3.7-plus 这类大模型即使回 "ping" 也常要 5–7s，
                # 太短（如 8s）会在它偶发抖动时误判「异常」。给到 20s 留足余量，
                # 仍远小于正常任务超时（60/90s），模型真不可达时也不会拖死诊断页。
                resp = await client.with_options(timeout=20).chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=4,
                )
                result[key]["ok"] = True
                result[key]["latency_ms"] = int((time.perf_counter() - t) * 1000)
                result[key]["sample"] = (resp.choices[0].message.content or "")[:40]
            except Exception as e:  # noqa: BLE001
                result[key]["ok"] = False
                result[key]["error"] = str(e)[:240]

        await asyncio.gather(
            _one(self._text_client,   self.text_model,   "text"),
            _one(self._vision_client, self.vision_model, "vision"),
        )
        return result

    async def text_complete(
        self,
        prompt: str,
        system: str | None = None,
        *,
        json_mode: bool = False,
        temperature: float = 0.4,
        max_tokens: int = 4096,
        timeout: float | None = None,
        retries: int = 2,
        model: str | None = None,
    ) -> str:
        """Run a text completion.

        ``model`` overrides the default ``text_model``. Use this to route
        latency-sensitive calls (clustering, governance suggestions) to a
        faster, cheaper model like ``qwen3.6-flash`` configured via
        ``TEXT_MODEL_FAST``.
        """
        if self._text_client is None:
            return _mock_text(prompt, json_mode)

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        client = self._text_client.with_options(timeout=timeout) if timeout else self._text_client
        chosen_model = model or self.text_model

        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                kwargs: dict = {
                    "model": chosen_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = await client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("text_complete[%s] attempt %d/%d failed: %s", chosen_model, attempt + 1, retries + 1, e)
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise LLMError(f"text_complete failed after retries: {last_err}", model=chosen_model)

    async def text_stream(
        self,
        prompt: str,
        system: str | None = None,
        *,
        temperature: float = 0.4,
    ) -> AsyncIterator[str]:
        if self._text_client is None:
            for chunk in _mock_text(prompt).split():
                yield chunk + " "
                await asyncio.sleep(0.02)
            return

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        stream = await self._text_client.chat.completions.create(
            model=self.text_model, messages=messages, temperature=temperature, stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def vision_describe(
        self,
        image_url: str,
        prompt: str = "请用一句中文描述这张图片的核心信息。",
        *,
        max_tokens: int = 200,
    ) -> str:
        """识别 / 描述一张图片。``max_tokens`` 默认 200（图示说明够用）；
        整页 OCR 这类需要长输出的场景，调用方应调大（如 1500）。"""
        if self._vision_client is None:
            return f"（mock 图片说明）{prompt}"
        try:
            resp = await self._vision_client.chat.completions.create(
                model=self.vision_model,
                messages=[
                    {"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ]}
                ],
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001
            log.warning("vision_describe failed: %s", e)
            return f"（图片说明生成失败：{type(e).__name__}）"

    async def vision_complete(
        self,
        images: list[str],
        prompt: str,
        system: str | None = None,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.4,
        timeout: float | None = 180,
        retries: int = 1,
    ) -> str:
        """多图 + 指令 → 内容生成（读图内容生产）。

        ``images`` 是 data URI 列表（``data:image/png;base64,...``）或可访问的图片 URL。
        与 ``vision_describe`` 的区别：支持多张图、可带 system、输出更长，用于把若干截图
        重组成结构化文档 / HTML。视觉模型未配置时回退 mock 文本。
        """
        if self._vision_client is None:
            return _mock_text(prompt)

        content: list[dict] = [{"type": "text", "text": prompt}]
        for url in images:
            if url:
                content.append({"type": "image_url", "image_url": {"url": url}})
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        client = self._vision_client.with_options(timeout=timeout) if timeout else self._vision_client
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await client.chat.completions.create(
                    model=self.vision_model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                return resp.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("vision_complete[%s] attempt %d/%d failed: %s",
                            self.vision_model, attempt + 1, retries + 1, e)
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise LLMError(f"vision_complete failed after retries: {last_err}", model=self.vision_model)

    async def image_generate(
        self,
        prompt: str,
        *,
        size: str | None = None,
        n: int = 1,
        timeout: float | None = None,
    ) -> list[bytes]:
        """用 GPT-Image-1 出图，返回 PNG 字节列表。

        未配置生图模型时返回 [] —— 调用方据此走占位降级（不要把它当异常）。
        真正的调用失败才抛 LLMError。Azure / OpenAI 的 gpt-image-1 默认返回 b64_json。
        """
        if self._image_client is None:
            return []
        client = self._image_client.with_options(timeout=timeout) if timeout else self._image_client
        try:
            resp = await client.images.generate(
                model=self.image_model,
                prompt=prompt,
                size=size or settings.image_size,
                n=max(1, min(n, 4)),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("image_generate[%s] failed: %s", self.image_model, e)
            raise LLMError(f"image_generate failed: {e}", model=self.image_model)

        out: list[bytes] = []
        for d in getattr(resp, "data", None) or []:
            b64 = getattr(d, "b64_json", None)
            if b64:
                try:
                    out.append(base64.b64decode(b64))
                except (binascii.Error, ValueError):
                    continue
        return out


def _mock_text(prompt: str, json_mode: bool = False) -> str:
    """离线模式返回结构化样例。"""
    if json_mode:
        return json.dumps({
            "title": "Q3 营销战役复盘",
            "summary": "Q3 整合营销战役整体达成目标，CTR 与留资转化双升；直播 GMV 与私域承接需要改进。",
            "outline": [
                {"level": 1, "text": "背景与目标"},
                {"level": 1, "text": "战役回顾"},
                {"level": 2, "text": "投放节奏"},
                {"level": 2, "text": "创意素材"},
                {"level": 1, "text": "关键指标"},
                {"level": 1, "text": "问题与归因"},
                {"level": 1, "text": "下一步建议"},
            ],
            "sections": [
                {"heading": "背景与目标", "body": "Q3 围绕新品发布与渠道渗透，开展为期 8 周的整合营销战役。"},
                {"heading": "战役回顾", "body": "投放分三阶段：预热、上线高峰、长尾承接。视频素材 CTR 领先 1.8pt。"},
                {"heading": "问题与归因", "body": "直播 GMV 未达预期、私域承接断点、区域差异显著。"},
                {"heading": "下一步建议", "body": "锁定核心 SKU、企微 SOP、差异化预算分配。"},
            ],
            "metrics": [
                {"label": "触达用户", "value": "3.42M", "delta": "+18% vs Q2"},
                {"label": "CTR",      "value": "4.6%",  "delta": "+0.7pt"},
                {"label": "CPA",      "value": "¥18.4", "delta": "−12%"},
                {"label": "留资转化", "value": "11.8%", "delta": "+2.1pt"},
            ],
            "highlights": ["CTR / CPA 双向优化", "视频素材效率显著", "私域承接断点亟待修复"],
            "next_steps": ["供应链 SKU 提前锁定", "企微 SOP 标准化", "区域差异化投放"],
            "tags": ["复盘", "营销", "Q3"],
            "page_type": "project",
        }, ensure_ascii=False)
    return "（mock 文本输出）" + prompt[:120]


_instance: LLMClient | None = None


def get_llm() -> LLMClient:
    global _instance
    if _instance is None:
        _instance = LLMClient()
    return _instance
