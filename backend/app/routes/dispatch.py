"""协作分发的辅助接口：列出可分发的目标群、直接发送一条群消息。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..feishu import LarkCLI, MockLarkCLI, get_lark
from ..services import audit
from .writeback import _explain_error

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])


async def _resolve_lark():
    lark = await get_lark()
    if isinstance(lark, LarkCLI) and not await lark.ping():
        if settings.enable_mock_fallback:
            return MockLarkCLI()
    return lark


class SendBody(BaseModel):
    chat_id: str
    text: str
    markdown: bool = True


@router.post("/send")
async def send(body: SendBody) -> dict:
    """直接把一段文本发到指定群（跳过草稿 / 确认弹窗）。

    由用户在前端点「直接发送」并二次确认后调用——按钮点击即为本次发送的授权；
    本接口不做任何自动发送。
    """
    if not body.chat_id.strip():
        raise HTTPException(400, "请选择目标群。")
    if not body.text.strip():
        raise HTTPException(400, "发送内容为空。")
    lark = await _resolve_lark()
    try:
        result = await lark.im_send(chat_id=body.chat_id, text=body.text,
                                    markdown=body.markdown, dry_run=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"发送失败：{_explain_error(e)}")
    await audit.write(actor=None, agent_id="collab-dispatch", action="direct_send",
                      target=body.chat_id, outcome="executed",
                      details={"chars": len(body.text)})
    return {"ok": True, "result": result}


@router.get("/chats")
async def list_chats() -> dict:
    """当前用户所在的飞书群（供「协作分发」选目标群）。失败返回空列表，不阻塞页面。"""
    try:
        lark = await _resolve_lark()
        items = await lark.im_chat_list(page_size=80)
    except Exception:  # noqa: BLE001
        items = []
    return {"items": items}
