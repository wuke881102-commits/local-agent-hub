"""个人摘记接口：开/停定时推送、立即推一条、看状态。

## 这是全应用唯一「无逐次确认就外发」的地方

其余所有写操作（发群消息、建任务、写文档）都要用户在确认弹窗里勾一次。摘记不同：
用户在这个场景里显式开启之后，后台按频率自己发。之所以敢这么做，是因为两条约束：

- **收件人恒为用户自己**（services/selfpush.py 从 lark-cli 的登录态里取自己的
  open_id，不接受任何外部传入的收件人）。接口层不提供任何指定收件人的入口。
- **默认关闭**，且停就真停。

改动这里前请先读 services/memo.py 的头注。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import memo

router = APIRouter(prefix="/api/memo", tags=["memo"])


class StartBody(BaseModel):
    every_min: int = 240
    # 来源开关。故意用显式字典而不是列表：前端加来源时后端不用改。
    sources: dict[str, bool] = {"digest": True, "mail": True, "voice": True}


@router.get("/status")
async def status() -> dict:
    return memo.status()


@router.post("/start")
async def start(body: StartBody) -> dict:
    try:
        return await memo.start(body.every_min, body.sources)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/stop")
async def stop() -> dict:
    return await memo.stop()


@router.post("/run")
async def run() -> dict:
    """立即推一条（不改变定时器）。

    没有新内容时**明确告诉用户**「没有新内容」，而不是静悄悄返回成功 —— 手动点
    一下没反应，最容易让人以为功能坏了。
    """
    return await memo.run_once(manual=True)
