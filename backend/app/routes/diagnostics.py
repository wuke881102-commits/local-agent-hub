from __future__ import annotations

import asyncio

from fastapi import APIRouter

from ..config import settings
from ..feishu import LarkCLI, MockLarkCLI, get_lark
from ..llm import get_llm
from ..services import index_service, audit

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


async def _llm_ping_safe() -> dict:
    """LLM 探活，最多等 26 秒；超时返回降级结果，避免拖死诊断页。

    必须 > 单模型探活超时（client.ping 用 20s），否则大模型（qwen3.7-plus 常要 6s+）
    偶发抖动时这层会先超时，把本来正常的模型也误判成「异常」。
    """
    llm = get_llm()
    try:
        return await asyncio.wait_for(llm.ping(), timeout=26)
    except asyncio.TimeoutError:
        def degraded(model: str, provider: str, mock: bool) -> dict:
            return {"ok": False, "model": model, "provider": provider, "mock": mock,
                    "error": "ping 超时（模型响应慢或端点不可达）"}
        return {
            "text": degraded(llm.text_model, llm.text_provider, llm._text_mock),
            "vision": degraded(llm.vision_model, llm.vision_provider, llm._vision_mock),
            "mock": llm._text_mock,
        }


@router.get("")
async def diagnostics() -> dict:
    lark = await get_lark()
    cli_available = await lark.ping() if isinstance(lark, LarkCLI) else True
    cli_version = lark.version if hasattr(lark, "version") else None

    if isinstance(lark, LarkCLI) and not cli_available and settings.enable_mock_fallback:
        lark_for_auth = MockLarkCLI()
        cli_mode = "mock-fallback"
    elif isinstance(lark, MockLarkCLI):
        lark_for_auth = lark
        cli_mode = "mock"
    else:
        lark_for_auth = lark
        cli_mode = "live"

    # 并发跑各项检查，LLM ping 已自带 12s 超时兜底，避免任一慢检查拖死整页。
    auth_status, llm_ping, index_stats, recent_audit = await asyncio.gather(
        lark_for_auth.auth_status(),
        _llm_ping_safe(),
        index_service.stats(),
        audit.recent(limit=10),
    )

    return {
        "cli": {
            "available": cli_available or cli_mode != "live",
            "version": cli_version,
            "mode": cli_mode,
            "bin": settings.lark_cli_bin,
        },
        "auth": auth_status,
        "llm": llm_ping,
        "index": index_stats,
        "audit_recent": recent_audit,
        "env": {
            "text_provider": settings.text_model_provider,
            "text_model": settings.text_model,
            "text_model_fast": settings.text_model_fast,
            "text_model_best": settings.text_model_best,
            "text_endpoint": settings.text_model_azure_endpoint or settings.text_model_base_url,
            "vision_provider": settings.vision_model_provider,
            "vision_model": settings.vision_model,
            "vision_endpoint": settings.vision_model_azure_endpoint or settings.vision_model_base_url,
            "mock_fallback": settings.enable_mock_fallback,
        },
    }
