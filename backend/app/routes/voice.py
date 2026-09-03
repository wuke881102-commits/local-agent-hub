"""语音速记接口：选设备、开录、停录、看已有记录。

## 这个场景会把语音发到云端，所以接口层有三条硬约束

1. **``mode`` 每次都必须显式传**，没有默认值、没有"记住上次"。录系统声音
   意味着录到会议里其他人的声音，这件事不该靠一个被记住的默认值悄悄发生。
2. **没有任何接口能开启定时/自动录音。** 只有 ``POST /start`` 这一个入口，
   而它只能被点出来。个人摘记可以把语音记录当来源，但它只读**已经录好的文字**
   （services/voice.py 的 ``notes_in_window``），永远不触发录音。
3. **没有"下载音频"接口，因为音频压根不落盘。** 能取到的只有转写文字和提炼结果。

改动这里前请先读 services/voice.py 和 services/voice_asr.py 的头注 —— 尤其是
「会话必须配成只转写不对话」那一节，那是花了几轮实测才定下来的，改配置会静默
产生费用。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..config import settings
from ..services import voice

router = APIRouter(prefix="/api/voice", tags=["voice"])


class StartBody(BaseModel):
    # 无默认值：必须显式选。见模块头注第 1 条。
    mode: str
    # 留空 = 用系统默认设备。传具体 index 时来自 GET /devices。
    mic_device: int | None = None
    loopback_device: int | None = None


@router.get("/devices")
async def devices() -> dict:
    return voice.devices()


@router.get("/status")
async def status() -> dict:
    return voice.status()


@router.post("/start")
async def start(body: StartBody) -> dict:
    try:
        return await voice.start(body.mode, body.mic_device, body.loopback_device)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/stop")
async def stop() -> dict:
    """停录并出一条记录。

    没转出文字时**明确说清为什么**（没说话 / 系统没在放声音 / 某一路开不起来），
    而不是返回一个空成功 —— 录了半天什么都没有，最需要的就是一句解释。
    """
    return await voice.stop()


@router.post("/transcribe-file")
async def transcribe_file(request: Request, filename: str = "") -> dict:
    """把一个音频 / 视频文件转成一条记录。文件名走查询串，**文件本体是原始请求体**。

    ## 为什么不用 multipart / UploadFile

    FastAPI 的 ``UploadFile`` 底层是 ``SpooledTemporaryFile``：超过 1 MB 就落到
    系统临时目录。也就是说一段会议录音会先在磁盘上完整存在一份，而这个场景对
    用户的承诺是「音频不落盘」—— 我们能删自己写的，删不掉框架替我们写的那份。

    所以这里收裸的请求体：字节进内存、解码进内存、转写完出作用域。前端直接
    ``fetch(url, {method:'POST', body: file})`` 就行，File 本身就是 Blob。

    代价是一次只能传一个文件、拿不到 multipart 里的其它字段 —— 这个场景不需要。
    """
    limit = max(1, int(settings.voice_file_max_mb)) * 1024 * 1024
    # 先看 Content-Length 就拒，别等把 500 MB 读进内存之后再说超限。
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        raise HTTPException(413, "文件 %.1f MB，超过上限 %d MB。" % (
            declared / 1024 / 1024, settings.voice_file_max_mb))

    data = await request.body()
    if not data:
        raise HTTPException(400, "请求体是空的，没收到文件。")
    return await voice.transcribe_file(data, filename)


@router.get("/notes")
async def notes(limit: int = 50) -> dict:
    return {"notes": voice.list_notes(limit)}


@router.get("/notes/{note_id}")
async def note(note_id: str) -> dict:
    n = voice.get_note(note_id)
    if n is None:
        raise HTTPException(404, "没有这条记录。")
    return n


@router.post("/notes/{note_id}/distill")
async def distill(note_id: str) -> dict:
    """对一条已有记录重新提炼。

    逐字稿已经存着，提炼失败（超时、模型抖动）不该让人重录 —— 音频早就没了。
    """
    try:
        return await voice.redistill(note_id)
    except ValueError as e:
        raise HTTPException(404 if "没有这条" in str(e) else 400, str(e))


@router.delete("/notes/{note_id}")
async def delete(note_id: str) -> dict:
    if not voice.delete_note(note_id):
        raise HTTPException(404, "没有这条记录。")
    return {"ok": True}
