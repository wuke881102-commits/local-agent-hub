"""本地邮箱 —— 给「打开即看」的三视图页面用（智能看板 / 信息矩阵 / 时间图谱）。

和 services 层的分工：outlook.py 负责 COM 取数 + 归一化，outlook_view.py 把邮件列表
折叠成会话并算「谁在等我 / 我答应过什么 / 什么到期」，outlook_tags.py 出类型标签和
矩阵字段，outlook_graph.py 出时间轴和关系网，outlook_ai.py 补语义字段（唯一外发）。
本模块只做参数校验和 HTTP 形状。

## 只读收件箱根目录

**不看任何子文件夹。** 早先有个 folder 参数能指向「收件箱/某个子文件夹」，
以及一个 /folders 端点做选择器，都去掉了：规则分流出来的子文件夹（实测该收件箱下有
Alerts 1710 封 / Microsoft 6722 封这类噪音桶）本来就是用户主动分走的东西。
把入口拆掉比「靠调用方不传参」可靠 —— 现在「只看收件箱」是结构性的，不是约定。

## 但「读哪个邮箱」是可以选的

别把上面那条读成「不给选任何东西」。子文件夹不给选，是因为那是用户自己分走的噪音；
邮箱给选，是因为一个 profile 里挂着委派/共享邮箱是常态，那里的邮件同样要处理。
选择存在后端（DATA_ROOT/outlook_store.json），因为「个人摘记」是后台跑的，
存前端它就跟着不了。收件箱和已发送**一起**跟随选择，理由见 services 里 _folder_of。

## 副作用只有一处

取数接口全部是 GET 且**完全只读**：不发送、不删除、不移动、不改标记。
唯一的例外是 `POST /open` —— 按用户点击把某封邮件在 Outlook 里打开（Outlook 自己
可能因此把它标成已读）。这条设计是刻意的：面板负责让你知道欠了什么，**回复这件事
留在 Outlook 里做**，这个程序不代写、不代发。

将来若要加"写草稿箱"，那是另一个模块 + 独立开关 + 前端二次确认，不要往这里塞。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query

from ..config import settings
from ..services import outlook, outlook_demo, outlook_open

router = APIRouter(prefix="/api/outlook", tags=["outlook"])


def _err(e: Exception) -> HTTPException:
    """OutlookError 的 message 已经是能照着做的中文，直接透出。"""
    if isinstance(e, outlook.OutlookError):
        return HTTPException(status_code=502, detail=str(e))
    return HTTPException(status_code=502, detail="访问本地邮箱失败：%s" % type(e).__name__)


def _filtered(data: dict, *, bucket: str, q: str) -> dict:
    """桶 / 关键词过滤。真实数据和演示数据共用，保证两条路径的行为完全一致。"""
    msgs = data["messages"]
    if bucket.strip():
        want = bucket.strip()
        msgs = [m for m in msgs if m.get("bucket") == want]
    if q.strip():
        kw = q.strip().lower()
        msgs = [m for m in msgs
                if kw in (m.get("subject") or "").lower()
                or kw in (m.get("sender_name") or "").lower()
                or kw in (m.get("sender_addr") or "").lower()]
    # counts 始终是**过滤前**的全量口径 —— 否则点进某一类之后所有数字都变了，
    # 用户会以为其他类的邮件消失了。
    data["messages"] = msgs
    data["shown"] = len(msgs)
    data["filter"] = {"bucket": bucket, "q": q}
    return data


@router.get("/probe")
async def probe() -> dict:
    """连通性检查。页面先问这个 —— 连不上就直接给人话诊断，别让用户对着空列表猜。

    刻意不抛 502：连不上本身就是要展示给用户的正常结果，不是接口错误。
    """
    return await outlook.probe()


@router.get("/stores")
async def stores() -> dict:
    """profile 里的邮箱/存储列表 + 当前选中哪个。

    实测一个 profile 里可能有多个邮箱（本机两个：主邮箱 + 一个委派邮箱）。用户
    有权知道我们读的是哪一个，也有权换一个 —— ``selected`` 为空表示跟随默认邮箱。
    """
    try:
        items = await outlook.stores()
        sel = (await outlook.store_choice())["display_name"]
        return {"stores": items, "selected": sel}
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e


@router.post("/store")
async def set_store(name: str = Body("", embed=True)) -> dict:
    """换一个邮箱来读。空串 = 回到默认邮箱。

    **这不是写 Outlook。** 写的是本程序自己的一个偏好文件（DATA_ROOT/
    outlook_store.json，内容只有一个显示名），Outlook 那边依然一个字都不改。
    收件箱和已发送会一起跟着走 —— 分开会让「我回过没有」的判断整个反掉。
    """
    try:
        return await outlook.set_store_choice(name)
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e


@router.get("/inbox")
async def inbox(
    window_days: int = Query(None, ge=1, le=30,
                             description="回看多少天。留空用配置里的默认值"),
    since: str = Query("", description="起始日期 YYYY-MM-DD（含）。给了就以区间为准，忽略 window_days"),
    until: str = Query("", description="结束日期 YYYY-MM-DD（含）"),
    limit: int = Query(60, ge=1, le=200),
    bucket: str = Query("", description="只看某一类：need_reply / need_action / fyi"),
    q: str = Query("", description="在主题和发件人里过滤"),
    include_body: bool = Query(True, description="是否取正文（用于截止日期和语义抽取）"),
    refresh: bool = Query(False, description="忽略内存缓存重新去 Outlook 读"),
    demo: bool = Query(False, description="返回编造的演示数据，完全不碰 Outlook"),
) -> dict:
    """收件箱根目录一次取数 + 三个视图。页面打开就渲染这个。

    为什么要有 since/until 而不只是「最近 N 天」：window_days 在服务层是
    `now - N 天`，也就是滚动的 N×24 小时窗口，早上九点点「近 3 天」会把三天前
    上午的邮件切掉一半。since 解析到**当天零点**，所以 N 天就是干净的 N 个自然日。
    前端固定用 since。
    """
    days = window_days or int(getattr(settings, "outlook_window_days", 7))
    if demo:
        # 演示模式：**一次 COM 调用、一次模型调用都不发**。这是它存在的全部意义 ——
        # 在 Outlook 会弹「程序正在访问地址信息」模态框而挂死的机器上，这条路径
        # 照样能看界面。所以这个分支必须在任何 outlook.* 调用之前返回。
        # 响应里带 demo=true，前端据此挂横幅，不会把假数据当真数据展示。
        data = outlook_demo.inbox_demo(since_day=since, until_day=until)
        return _filtered(data, bucket=bucket, q=q)
    try:
        data = await outlook.inbox(
            window_days=days, limit=limit,
            need_body=include_body, with_reply_check=True,
            since_day=since, until_day=until, refresh=refresh,
        )
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e

    return _filtered(data, bucket=bucket, q=q)


@router.post("/open")
async def open_item(entry_id: str = Body(..., embed=True,
                                        description="要打开的邮件 EntryID")) -> dict:
    """在 Outlook 里打开这封邮件。**本接口的唯一动作就是打开一个窗口。**

    这是整个功能设计的枢纽：面板告诉你欠了什么，回复留给 Outlook。所以这里不写
    草稿、不预填正文、不发送 —— 发信这件事不该由这个程序碰。

    entry_id 只接受前端从我们自己刚返回的列表里取到的值，不接受邮件正文里的任何内容。
    """
    try:
        return await outlook_open.open_item(entry_id)
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e
