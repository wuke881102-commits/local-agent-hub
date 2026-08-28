"""本地邮箱（场景名）—— 只读数据层，底层走 Outlook COM。

注意用词：面向用户的文案里这个功能叫「本地邮箱」；但凡是指引用户去操作
Outlook 那个**应用**的句子（重建 profile、开启缓存模式…）必须保留 "Outlook"，
否则操作说明就没法照着做了。

## 为什么走 COM，不走 Graph / 不走第三方 CLI

COM 直接跟**桌面上已登录的 Outlook** 说话：不需要 API key、不需要注册 Entra 应用、
不需要租户管理员同意、数据不出这台机器。Graph 要企业审批且是云端调用；第三方邮件
CLI（porteden 之类）要注册账号、把公司邮件过一层外部服务，跟「本机 Agent」的前提冲突。

## 只读

本模块**只读**。没有 send / reply / delete / move / 改标记的代码路径，一行都没有。
误发一封对外邮件不可撤回，而"省下点一次发送按钮"不值这个风险。
将来若要加"写草稿箱"，只能是 MailItem.Save()，且必须单独开关 + 前端二次确认。

## 关键设计都来自真机探测（scripts/outlook_probe.ps1），不是猜的

实测环境两处，结论有差异，代码要同时扛住：
  A. 目标机（用户笔记本）：Outlook 16.0.0.19029，**缓存模式**，收件箱 13,800 封。
  B. 开发机：同一邮箱但**在线模式**（无 .ost），收件箱 52,100 封。在线模式下每读一个
     属性都是一次网络往返 —— 取满字段实测约 6 秒/封，比缓存模式慢两个数量级。
     所以时间预算（见"墙钟预算"）不是可选项，是必需品。

1. **不用 Items.Restrict，用 Sort + 从新往旧走 + 提前 break。**
   探测里 DASL `datereceived >= '2026-08-14T00:00:00Z'` 匹配到 13,798/13,800 —— 带
   T/Z 的 ISO 写法不被 urn:schemas:httpmail:datereceived 认，比较退化成恒真。Jet 语法
   （`[ReceivedTime] >= '8/14/2026'`）又跟系统区域设置绑定，中文环境常年踩坑。
   Sort 实测 21ms，语义无歧义，13,800 封里只碰最新几百封。别改回 Restrict。

2. **不用"未读"当信号。** 实测该收件箱 13,800 封、未读 0 —— 用户把邮件都点开过。
   以未读做筛选会返回空列表。真正的信号是「这个会话我回过没有」：拿收件箱的
   ConversationID 去已发送邮件里找有没有更晚的同会话邮件。零大模型，纯本地。

3. **发件人地址要走降级链，单靠一个属性拿不到。** 探测显示内部邮件的
   SenderEmailAddress 是 128 字符的 X500 DN（/O=EXCHANGELABS/...），不是邮箱地址。
   于是我改读 PR_SMTP_ADDRESS —— 但在真实邮箱上实测（在线模式、26 封样本）
   **它一条都没取到**。所以降级链必须够长：
     PR_SMTP_ADDRESS → PR_SENDER_SMTP_ADDRESS → PR_SENT_REPRESENTING_SMTP_ADDRESS
     → SenderEmailAddress（仅当它含 @ 且不是 DN）
   全都拿不到就如实标 x500_only、地址留空。显示上有 SenderName 就够，
   别硬塞一个 DN 当邮箱地址糊弄前端。

4. **「我在收件人还是抄送」首选 MAPI 属性，但必须有降级。**
   PR_MESSAGE_TO_ME / PR_MESSAGE_CC_ME 是存储算好的布尔值，一次读取、不碰通讯簿
   （所以不会触发「程序化访问」提示）—— 但真实邮箱上实测**约 1/3 的邮件读不到**
   （26 封里 9 封为空）。不做降级的话，这 1/3 全落进「无法确认」，而「需我回复」
   列表里三成是猜的 —— 一个分类列表的全部价值就是可信，三成靠猜就没人再看它。
   降级：比对 To/CC 显示名字符串（先比地址再比显示名，全小写子串匹配）。
   两边都没命中且字段非空 → 说明收件人栏里根本没有我（通讯组/规则投递），
   这本身就是「不需要我回」的有力信号，标成 not_addressed，不是「未知」。

5. **附件要滤掉内嵌图。** 实测有封邮件 Attachments=35，全是 png —— 签名图和内嵌截图。
   不滤的话「有附件」这个标记等于每封 HTML 邮件都命中，毫无意义。

6. **正文必须硬截断。** 实测单封 Body 28,463 / HTMLBody 333,763 字符。喂给大模型
   既贵又是不必要的外发。优先纯文本 Body，截断到 settings.outlook_body_max。

7. **MessageClass 要过滤。** 实测最新 200 封里 191 普通邮件、4 会议邀请、2 退信、
   1 自动回复、1 会议回执。退信和自动回复剔除，会议邀请单独成类（它确实需要你回应）。

## 线程模型

Outlook COM 是单线程的，且每个线程都要自己 CoInitialize。所以全部 COM 操作串到一个
**独占单线程 executor** 上跑：apartment 一致、对 Outlook 的访问天然串行化。
FastAPI 的 async 端点 await 它即可。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..config import DATA_ROOT, settings
# 纯函数层（把邮件列表重组成欠账表）。它不 import 本模块，所以没有循环依赖；
# 也因此真实取数和演示数据能共用同一套分区/抽取逻辑。
from . import outlook_graph, outlook_tags, outlook_view

log = logging.getLogger(__name__)


class OutlookError(Exception):
    """message 是给用户看的中文，路由层直接透出。"""


# ─────────────────────────────────────────────────────── MAPI 属性标签
# PropertyAccessor 用的是这种 URL 形式的 proptag。数字部分是 MAPI 属性 ID + 类型：
#   0x39FE001E = PR_SMTP_ADDRESS        (001E = PT_UNICODE 字符串)
#   0x0057000B = PR_MESSAGE_TO_ME       (000B = PT_BOOLEAN)
#   0x0058000B = PR_MESSAGE_CC_ME
#   0x0E1D001F = PR_NORMALIZED_SUBJECT  (去掉 "RE:"/"答复:" 前缀后的主题)
#   0x3FDE0003 = PR_INTERNET_CPID       (代码页，用来判断乱码来源)
_P = "http://schemas.microsoft.com/mapi/proptag/"
PROP_SMTP = _P + "0x39FE001E"
PROP_TO_ME = _P + "0x0057000B"
PROP_CC_ME = _P + "0x0058000B"
PROP_NORM_SUBJECT = _P + "0x0E1D001F"
# 发件人地址的另外两个来源。实测（在线模式、真实邮箱、26 封样本）PR_SMTP_ADDRESS
# **一条都没取到** —— 内部发件人只有 X500 DN，外部的才退回 SenderEmailAddress。
# 这两个属性是标准答案，通常有值：
#   0x5D01001F = PR_SENDER_SMTP_ADDRESS
#   0x5D02001F = PR_SENT_REPRESENTING_SMTP_ADDRESS（代发时是"真正的作者"）
PROP_SENDER_SMTP = _P + "0x5D01001F"
PROP_SENT_REPR_SMTP = _P + "0x5D02001F"
# 「这封邮件投递给了谁」——也就是我。存在**邮件自己**的属性表里，不查目录，
# 和上面的 PROP_TO_ME 属于同一类安全属性。这是唯一一条能拿到自己**显示名**
# 的安全路径：_me() 的注释列了所有常规途径（CurrentUser / Accounts.UserName /
# 自己发出邮件的 SenderName），实测全部会因为要查目录而无限期阻塞。
# 拿不到显示名的后果见 _me_in —— To/CC 里装的是显示名，没得比就只能答「判不了」。
PROP_RECEIVED_BY_NAME = _P + "0x0040001F"
PROP_RECEIVED_BY_SMTP = _P + "0x5D07001F"
# 学显示名最多试多少封。命中一次就不再试；一直不命中（例如共享邮箱、属性缺失）
# 也要停手，否则每封白搭两次属性读取。
_ME_NAME_TRIES = 20

# 直接剔除：退信、投递报告、自动回复模板、会议取消通知。
# 留在列表里只会把「需我回复」污染成一堆机器邮件。
SKIP_CLASS_PREFIXES = (
    "REPORT.",                          # NDR / 已读回执 / 投递报告
    "IPM.Note.Rules.OofTemplate",       # 别人的自动回复（外出）
    "IPM.Note.Rules.ReplyTemplate",
    "IPM.Schedule.Meeting.Canceled",    # 会议已取消
    "IPM.Schedule.Meeting.Resp",        # 会议回执（接受/拒绝/暂定）
    "IPM.Outlook.Recall",               # 撤回请求
)
# 会议邀请单独成类：它确实需要你回应，但不是"回邮件"。
MEETING_CLASS_PREFIX = "IPM.Schedule.Meeting.Request"
# 普通邮件
NOTE_CLASS_PREFIX = "IPM.Note"

# olFolder* 常量（只列我们用到的，避免整表照抄）
OL_FOLDER_INBOX = 6
OL_FOLDER_SENT = 5

# 判排序方向时，从每一头最多往里探几封。首尾可能是 ReportItem（退信/回执）——
# 那类对象没有 ReceivedTime/SentOn 属性，读出来是 AttributeError。见 _newest_first。
# 10 是拍的，但有依据：实测踩到的是连续 2 封；留一个数量级的余量，
# 而每次探测失败几乎不花时间（属性在类型上不存在，不产生网络往返）。
END_PROBE_TRIES = 10

# ─────────────────────────────────────────────────────── 单线程 COM executor

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def _com_thread_init() -> None:
    """executor 线程启动时初始化 COM apartment。少这一步，Dispatch 直接抛。"""
    try:
        import pythoncom  # type: ignore

        pythoncom.CoInitialize()
    except Exception as e:  # noqa: BLE001
        log.warning("CoInitialize failed: %s", e)


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            # max_workers=1 是刻意的：Outlook COM 对象不是线程安全的，也不能跨
            # apartment 随便传。串行化反而是我们想要的。
            _pool = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="outlook-com",
                initializer=_com_thread_init,
            )
        return _pool


# 上一次丢进去的 COM 任务。用来判断"Outlook 还卡在上一个请求里"。
# 上一次丢进去的 COM 任务，以及它开始的时刻。
# 时刻是必需的：只有它才能区分「正忙」和「真卡死」—— 见 run_com 里的说明。
_inflight: Any = None
_inflight_since: float = 0.0
# 在跑那个调用**自己的**超时预算。判「卡死」必须拿它比，不能拿等待方的 timeout 比：
# 等待方可能只愿意等 2 秒，而对方有 45 秒预算、跑到第 3 秒完全正常。
# 拿等待方的预算去判，会把健康的慢调用判成卡死 —— 正是这次要修的那个误判。
_inflight_budget: float = 0.0
# 串行闸门。**不是**为了并发安全（那靠单 worker 线程池），而是为了让并发请求
# **排队**而不是互相报错。
_com_gate = asyncio.Lock()


def _gate_release() -> None:
    """future 真正结束时释放闸门。用 done_callback 而不是 finally —— 理由见 run_com。"""
    if _com_gate.locked():
        try:
            _com_gate.release()
        except RuntimeError:      # 非持有者/已释放，正常路径不会走到
            pass


async def run_com(fn: Callable[..., Any], *args: Any,
                  timeout: float | None = None) -> Any:
    """把一个同步 COM 调用丢到专属线程上执行，**并带硬超时**。

    ## 为什么必须有超时，而不是"挑不会阻塞的属性"

    实测下来，Outlook COM 的属性访问会**无限期阻塞**，而且哪一个会阻塞**不稳定**：
    同一句 ns.CurrentUser.Name，昨晚跑两次都正常返回，今早同一台机器同一个邮箱
    直接挂死 90 秒以上不返回。凡是要碰通讯簿/目录的属性都有这个风险。

    所以"选安全属性"只能减少概率，不能消除。而且同线程里**没有办法给 COM 调用加
    超时或取消它** —— 线程会一直卡在那里。

    ## 「正忙」不等于「卡死」—— 这里曾经把两者混为一谈

    早先的写法是：只要 _inflight 未完成就立刻抛「上一次读取还卡在 Outlook 里」。
    那是错的，而且**错得很频繁**：页面加载时会并发打三个用 COM 的接口
    （probe / stores / inbox），SWR 还会在窗口重新获得焦点时重新验证。谁抢输了就
    吃一个写着「卡住了」的错误 —— 而实际上另一个调用只是**正常地在跑**。
    用户看到的症状很怪：数据明明显示出来了，上面却挂着一条「读取失败」。

    现在的做法：
      · 正常并发 → **排队**等闸门（最多等 timeout 秒），拿到就照常跑；
      · 等不到，且在跑的那个已经超过它自己的预算 → 才报「卡死」；
      · 等不到但对方还在预算内 → 报「正忙」，措辞完全不同。

    闸门用 done_callback 释放、**不用 finally**：我们放弃等待时，卡住的 COM 线程
    仍然占着那个唯一的 worker。此时释放闸门会让下一个请求排到卡住的任务后面，
    于是它也超时 —— 一个卡死变成无限连锁超时。
    """
    global _inflight, _inflight_since, _inflight_budget
    if timeout is None:
        timeout = float(getattr(settings, "outlook_request_timeout_s", 45))

    try:
        await asyncio.wait_for(_com_gate.acquire(), timeout=timeout)
    except asyncio.TimeoutError as e:
        busy_for = (_now().timestamp() - _inflight_since) if _inflight_since else 0.0
        # 「卡死」= 在跑的那个**超过它自己的预算**还没返回。
        if (_inflight is not None and not _inflight.done()
                and _inflight_budget > 0 and busy_for > _inflight_budget):
            raise OutlookError(
                "上一次读取已经卡在 Outlook 里 %d 秒没有返回。Outlook 的 COM 调用无法"
                "从外部取消，只能等它自己结束 —— 或者重启 Outlook（这会立刻释放）。"
                % int(busy_for)) from e
        raise OutlookError(
            "正在读取本地邮箱（已进行 %d 秒），排队等了 %d 秒还没轮到。"
            "稍等一下再试，或者把时间范围缩小到「当天」。"
            % (int(busy_for), int(timeout))) from e

    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(_get_pool(), fn, *args)
    _inflight = fut
    _inflight_since = _now().timestamp()
    _inflight_budget = timeout
    fut.add_done_callback(lambda _f: _gate_release())
    try:
        # shield：wait_for 超时会取消它等的那个东西，但线程池任务取消不了；
        # 不 shield 的话只会得到一个更难看的状态，任务照样在跑。
        return await asyncio.wait_for(asyncio.shield(fut), timeout)
    except asyncio.TimeoutError as e:
        raise OutlookError(
            "读取本地邮箱超过 %d 秒没有响应。常见原因：邮箱是在线模式（没有本地"
            "缓存），或某个属性访问卡在了通讯簿查询上。缩小时间范围，或在 Outlook 里"
            "开启「缓存的 Exchange 模式」。" % int(timeout)
        ) from e


# ─────────────────────────────────────────────────────── COM 会话

class _Session:
    """缓存 Outlook.Application / MAPI namespace。

    缓存是为了快（每次 Dispatch 都要跟 Outlook 建连接）。但用户随时可能关掉
    Outlook，缓存的引用就成了野指针 —— 所以所有取用都过 _ns()，出错就丢弃重连一次。
    """

    def __init__(self) -> None:
        self._app: Any = None
        self._ns: Any = None

    def reset(self) -> None:
        self._app = None
        self._ns = None

    def _dispatch(self) -> Any:
        try:
            import win32com.client  # type: ignore
        except ImportError as e:
            raise OutlookError(
                "缺少 pywin32，无法访问本地邮箱。请在后端环境执行：pip install pywin32"
            ) from e
        try:
            # 用 Dispatch 而不是 DispatchEx：Dispatch 会附着到**已在运行**的 Outlook
            # 实例（GetActiveObject 优先）。DispatchEx 强开新实例，在 Outlook 已开着
            # 的机器上会拿到一个空壳会话。
            app = win32com.client.Dispatch("Outlook.Application")
            ns = app.GetNamespace("MAPI")
            # 摸一下 Stores 逼它真正登录；否则错误会延后到第一次取数才冒出来，
            # 那时的报错信息毫无指向性。
            _ = ns.Stores.Count
            return app, ns
        except Exception as e:  # noqa: BLE001
            raise OutlookError(_explain_com_error(e)) from e

    def ns(self) -> Any:
        if self._ns is None:
            self._app, self._ns = self._dispatch()
        return self._ns

    def ns_retry(self) -> Any:
        """取 namespace，失败就重连一次（应对"用户中途关了 Outlook"）。"""
        try:
            ns = self.ns()
            _ = ns.Stores.Count      # 探活
            return ns
        except OutlookError:
            raise
        except Exception:  # noqa: BLE001
            self.reset()
            return self.ns()


_session = _Session()


def _explain_com_error(e: Exception) -> str:
    """把 COM 的天书错误翻译成能照着做的中文。

    直接把 pywin32 的 com_error 抛给用户毫无意义（一串 HRESULT 和空 excepinfo），
    而这几种情况恰恰是最常见的、也都有明确的处理办法。
    """
    txt = str(e)
    low = txt.lower()
    if "information store" in low or "0x8004010f" in low:
        return ("Outlook 打得开，但邮箱存储打不开。通常是 profile 半途创建失败："
                "完全退出 Outlook，运行 outlook.exe /manageprofiles 新建一个 profile 并设为默认。")
    if "call was rejected" in low or "0x80010001" in low:
        return ("Outlook 正忙（可能有对话框在等你，或正在同步），暂时不接受调用。"
                "切到 Outlook 窗口看看有没有弹窗，处理完再试。")
    if "server execution failed" in low or "0x80080005" in low:
        return ("启动 Outlook 失败。若 Outlook 是以管理员身份运行、而本程序不是"
                "（或反之），两者不在同一个权限级别，COM 无法互通 —— 让两者权限一致。")
    if "class not registered" in low or "0x80040154" in low:
        return "这台机器上没有安装桌面版 Outlook（COM 类未注册）。本场景需要 Outlook 桌面版。"
    if "rpc" in low and "unavailable" in low:
        return "与 Outlook 的连接中断了（Outlook 可能已退出）。重新打开 Outlook 后再试。"
    return "访问本地邮箱失败：%s" % txt[:300]


# ─────────────────────────────────────────────────────── 小工具

def _s(v: Any) -> str:
    """COM 拿回来的值可能是 None / 非字符串，统一成裁剪过的 str。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return str(v).strip()


def _int_or(v: Any, default: int) -> int:
    """安全转 int。不要用 `int(v or default)` —— 那会把合法的 0 换成 default，
    而 0 在 COM 这边往往是最常见的取值（ExchangeStoreType=0 主邮箱、Importance=0 低）。"""
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _prop(item: Any, tag: str, default: Any = None) -> Any:
    """读 MAPI 属性。属性不存在是常态（不是错误），静默返回默认值。"""
    try:
        return item.PropertyAccessor.GetProperty(tag)
    except Exception:  # noqa: BLE001
        return default


def _dt(v: Any) -> datetime | None:
    """COM 的 pywintypes 时间 → 带本地时区的 datetime。

    ## 必须**丢掉** pywin32 给的 tzinfo，只取字段值

    这是 pywin32 的一个经典陷阱：COM 的 VARIANT 日期里存的是**本地挂钟时间**，
    但 pywin32 会给它贴上 **UTC** 的 tzinfo。于是 `v.astimezone()` 把本地时间当成
    UTC 再换算一次，在东八区就整整多出 8 小时。

    实测（这台机器，Outlook 收件箱最新一封）::

        原始 COM 值      2026-06-12 12:03:17+00:00   ← 被标成 UTC
        v.astimezone()   2026-06-12 20:03:17+08:00   ← 错，+8 小时
        字段值 + 本地时区  2026-06-12 12:03:17+08:00   ← 对

    这个 bug 曾经让信息矩阵里出现「08-25 00:39」而当天是 08-24 —— 收到的邮件
    落在未来，这是它最容易被发现的症状。

    所以这里**只用 v.year/month/day/hour/minute/second**，然后补本地时区。
    补时区（而不是留 naive）是因为下游要和 now() 比大小，naive 会抛 TypeError。
    """
    if v is None:
        return None
    try:
        d = datetime(v.year, v.month, v.day, v.hour, v.minute, v.second)
    except Exception:  # noqa: BLE001
        return None
    return d.replace(tzinfo=datetime.now().astimezone().tzinfo)


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso_or_empty(ts: float | None) -> str:
    """epoch 秒 → 本地 ISO 串；给不出来就空串。用来给承诺伪会话补一个可排序的时间。"""
    try:
        return datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds") if ts else ""
    except Exception:  # noqa: BLE001
        return ""


def short_dt(d: datetime | None) -> str:
    """列表里显示用的短时间。今天只显示时分，其余带日期。"""
    if d is None:
        return ""
    n = _now()
    if d.date() == n.date():
        return d.strftime("%H:%M")
    if d.year == n.year:
        return d.strftime("%m-%d %H:%M")
    return d.strftime("%Y-%m-%d")


def _skip_class(cls: str) -> bool:
    return any(cls.startswith(p) for p in SKIP_CLASS_PREFIXES)


# ─────────────────────────────────────────────────────── 附件

# 这些扩展名基本都是签名图/内嵌截图，不是"发给你的文件"。
_INLINE_EXTS = {"png", "gif", "jpg", "jpeg", "bmp", "svg", "ico", "webp"}


def _attachments(item: Any) -> list[dict]:
    """真附件列表。滤掉内嵌图片。

    判定顺序（任一命中即视为内嵌）：
      1. 有 PR_ATTACH_CONTENT_ID —— HTML 正文用 cid: 引用它，就是内嵌图
      2. Type == olOLE(6)      —— 嵌入对象
      3. 扩展名是图片 且 体积 < 64KB —— 兜底。签名图基本都很小；
         真的要发给你的截图一般更大。这条是启发式，所以放最后。
    """
    out: list[dict] = []
    try:
        n = item.Attachments.Count
    except Exception:  # noqa: BLE001
        return out
    for i in range(1, min(n, 60) + 1):
        try:
            a = item.Attachments.Item(i)
            name = _s(getattr(a, "FileName", "")) or _s(getattr(a, "DisplayName", ""))
            size = int(getattr(a, "Size", 0) or 0)
            atype = int(getattr(a, "Type", 0) or 0)
            cid = _prop(a, _P + "0x3712001F", "")     # PR_ATTACH_CONTENT_ID
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            inline = bool(cid) or atype == 6 or (ext in _INLINE_EXTS and size < 64 * 1024)
            if inline:
                continue
            out.append({"name": name, "ext": ext, "size": size})
        except Exception:  # noqa: BLE001
            continue
    return out


# ─────────────────────────────────────────────────────── 正文

_WS = re.compile(r"[ \t　]+")
_NL = re.compile(r"\n{3,}")
# 引用历史的分隔线。截断时优先在这里切，免得把整条历史邮件都算进正文长度。
_QUOTE_MARKS = (
    "\n发件人:", "\n發件人:", "\nFrom:", "\n-----Original Message-----",
    "\n________________________________", "\n在 20", "\nOn 20", "\n> ",
)


# 切掉引用后至少要剩这么多字符，才认为"新写的内容"真实存在。
# 低于它 → 整封基本都是引用（典型是无附言的纯转发），此时保留原文更有用：
# 对一封纯转发来说，历史**就是**内容，切干净只会得到一个空正文。
_MIN_NEW_TEXT = 12


def clean_body(raw: str, limit: int | None = None) -> tuple[str, bool]:
    """正文归一化 + 截断。返回 (文本, 内容是否不完整)。

    第二个返回值在两种情况下为 True：切掉了引用历史，或超长被硬截断。
    对前端来说这两者是同一件事——「你看到的不是全文」。

    先在引用分隔线处切掉历史往来 —— 一封 28,000 字符的邮件里，通常只有开头两三百
    字是这次新写的，剩下全是重复引用。切掉它比单纯截断保留的信息多得多。

    注意别加"从第 N 字符后才开始找引用标记"这种偏移：最常见的情况恰恰是新写的内容
    很短（"见附件" + 三千行历史），引用标记就在第 20 来个字符上，偏移会把它跳过去。
    要处理的"开头就是引用头"用 _MIN_NEW_TEXT 判断，而不是用偏移。
    """
    if not raw:
        return "", False
    txt = raw.replace("\r\n", "\n").replace("\r", "\n")
    cut_at = len(txt)
    for mark in _QUOTE_MARKS:
        i = txt.find(mark)
        if 0 <= i < cut_at:
            cut_at = i
    if cut_at < _MIN_NEW_TEXT:
        cut_at = len(txt)               # 整封都是引用 → 不切，交给下面的硬截断收口
    quoted = cut_at < len(txt)
    txt = txt[:cut_at]
    txt = _WS.sub(" ", txt)
    txt = _NL.sub("\n\n", txt).strip()
    lim = limit if limit is not None else int(getattr(settings, "outlook_body_max", 4000))
    if len(txt) > lim:
        return txt[:lim].rstrip() + " …", True
    return txt, quoted


# ─────────────────────────────────────────────────────── 截止日期线索

# 只做「线索」，不做「事实」。抽到了就在卡片上标一句，抽不到就不标 —— 不猜。
_DEADLINE_PATTERNS = [
    re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})\s*日?"),
    re.compile(r"(?<!\d)(\d{1,2})[/月](\d{1,2})\s*日?(?!\d)"),
]
_URGENCY_WORDS = (
    "截止", "deadline", "由此为准", "今天之前", "今日内", "尽快", "asap", "紧急",
    "eod", "cob", "本周内", "明天之前", "逾期", "过期", "请于", "最晚",
)


def deadline_hint(subject: str, body: str) -> dict | None:
    """从主题+正文开头找截止日期线索。

    只在**出现催办词**的附近取日期。否则每封带日期的邮件（"附上 8 月报表"）
    都会被标成有截止日期，噪音比信号多。
    """
    hay = (subject + "\n" + body[:1200]).lower()
    hit_word = next((w for w in _URGENCY_WORDS if w in hay), None)
    if not hit_word:
        return None
    now = _now()
    for pat in _DEADLINE_PATTERNS:
        for m in pat.finditer(subject + "\n" + body[:1200]):
            g = m.groups()
            try:
                if len(g) == 3:
                    y, mo, d = int(g[0]), int(g[1]), int(g[2])
                else:
                    y, mo, d = now.year, int(g[0]), int(g[1])
                if not (1 <= mo <= 12 and 1 <= d <= 31):
                    continue
                when = datetime(y, mo, d, tzinfo=now.tzinfo)
                # 只认「今天或以后」。往前的日期通常是在说过去的事。
                if when.date() < now.date():
                    continue
                return {"date": when.strftime("%Y-%m-%d"),
                        "days_left": (when.date() - now.date()).days,
                        "trigger": hit_word, "text": m.group(0)}
            except Exception:  # noqa: BLE001
                continue
    # 有催办词但没抽到日期，也值得标一下
    return {"date": "", "days_left": None, "trigger": hit_word, "text": ""}


# ─────────────────────────────────────────────────────── 消息归一化

def _me(ns: Any, *, sender_name: str = "") -> dict:
    """「我是谁」—— 用于 PR_MESSAGE_TO_ME 读不到时比对 To/CC 字符串。

    ## 这里的属性白名单是实测出来的，不要"顺手改回去"

    取自己的身份看着是件小事，实际是这个模块里最危险的一步。**下面三个调用会
    无限期阻塞**（实测：在线模式、profile 有点旧的真实邮箱，各挂 90 秒以上不返回）：

        ns.CurrentUser              —— 要解析 AddressEntry，等于一次目录查询
        ns.CurrentUser.Name
        ns.Accounts.Item(i).UserName
        ns.Accounts.Item(i).SmtpAddress

    而 Outlook COM 是单线程的：任意一句阻塞，整个功能跟着卡死，而且**同线程里没法
    给 COM 调用加超时**，只能事先避开。所以这里只允许用实测 0.0 秒返回的：

        ns.Accounts.Item(i).DisplayName     —— 本地 profile 配置，不查目录

    我一开始想从**自己发出去的邮件的 SenderName** 取显示名（免费、看着安全）——
    实测**同样挂死**：已发送邮件的发件人就是我自己，解析它照样要查目录。所以放弃，
    sender_name 只保留成一个入参，由调用方在别处拿到了才传进来。

    结论：这里只能拿到邮箱地址（DisplayName），拿不到显示名。这会削弱降级判断的
    能力（To/CC 里放的是显示名），所以 _me_in 在"没有显示名可比"时必须返回
    「判不了」而不是「不是发给我的」—— 见 _me_in。
    """
    out: dict = {"name": _s(sender_name), "smtp": "", "aliases": []}
    aliases: list[str] = []
    try:
        for i in range(1, int(getattr(ns.Accounts, "Count", 0) or 0) + 1):
            # 只读 DisplayName。别加 SmtpAddress / UserName —— 见上面的白名单说明。
            dn = _s(getattr(ns.Accounts.Item(i), "DisplayName", ""))
            if dn:
                aliases.append(dn.lower())
    except Exception:  # noqa: BLE001
        pass
    out["smtp"] = next((a for a in aliases if "@" in a), "")
    out["aliases"] = aliases
    return out


def _learn_me_name(item: Any, me: dict) -> None:
    """从邮件自身的「投递给谁」属性里学出**我的显示名**，就地写进 ``me``。

    为什么非得这么绕：``_me()`` 的注释列了所有能给出显示名的常规属性，实测全部会
    因为要查目录而无限期阻塞。而 PR_RECEIVED_BY_NAME 存在邮件自己的属性表里，和
    已经在用的 PR_MESSAGE_TO_ME 同一类 —— 不查目录，实测安全。

    **交叉校验是必须的**：只有当同一封的 PR_RECEIVED_BY_SMTP_ADDRESS 命中我们已知
    的地址别名时才采信这个名字。共享邮箱、代收规则的场合，「投递给谁」可能压根不是
    我；不校验就会把别人的显示名当成自己的，然后把一批别人的邮件判成「发给我的」
    —— 那比没有显示名糟得多（现在是漏报成「不确定」，那样是**误报**）。

    命中一次就固定下来（一个邮箱的显示名不会变），所以整趟取数只花几次属性读取。
    """
    if me.get("name") or me.get("name_tries", 0) >= _ME_NAME_TRIES:
        return
    me["name_tries"] = me.get("name_tries", 0) + 1

    nm = _s(_prop(item, PROP_RECEIVED_BY_NAME, ""))
    if not nm:
        return
    addrs = [a for a in (me.get("aliases") or []) if "@" in a]
    smtp = _s(_prop(item, PROP_RECEIVED_BY_SMTP, "")).lower()
    if addrs:
        if not smtp:
            return                       # 校验不了，换下一封再试
        if smtp not in addrs:
            return                       # 确定不是我这个邮箱，别采信
        me["name"], me["name_source"] = nm, "received_by"
        return
    # 一个 "@" 别名都没有 —— 无从校验。仍然采信（否则等于没修），但把来源标成
    # unverified，诊断里看得见，出问题时能一眼定位到这里。
    me["name"], me["name_source"] = nm, "received_by_unverified"


def _me_in(field: str, me: dict) -> bool | None:
    """我是不是出现在这个 To/CC 字符串里。True / False / **None = 判不了**。

    三态而不是两态，是这里最要紧的一点。To/CC 装的是**显示名**列表
    （"张三; 李四; 项目组"），而在这台机器上我们**拿不到自己的显示名**
    （所有能给出显示名的属性都会挂死，见 _me），手里只有邮箱地址。

    这种情况下"没匹配上"完全说明不了问题 —— 如果把它当成"不是发给我的"，
    真正需要我回复的邮件会被静悄悄埋进「只需知道」。**漏报比说"不知道"糟得多**：
    说不知道用户会自己去看，漏报他压根不知道有东西被藏了。
    所以只有在确实有东西可比对时才敢返回 False，否则返回 None。
    """
    if not field:
        # 空字段里不可能有我 —— 这个 False 是确定的，不是猜的。
        return False
    low = field.lower()
    for a in me.get("aliases") or []:
        if a and a in low:
            return True
    name = (me.get("name") or "").lower()
    if name and name in low:
        return True
    if name:
        return False                    # 有显示名可比，且 To/CC 装的就是显示名
    if "@" in low and (me.get("aliases") or []):
        return False                    # 字段是地址形态，而我们手里有地址
    return None                         # 只有地址、字段是显示名 → 判不了


def _sender_smtp(item: Any, raw_addr: str) -> tuple[str, str]:
    """发件人 SMTP 地址 + 它是从哪来的。

    降级链（实测在线模式下前一个常常是空的，所以链条必须够长）：
      PR_SMTP_ADDRESS → PR_SENDER_SMTP_ADDRESS → PR_SENT_REPRESENTING_SMTP_ADDRESS
      → SenderEmailAddress（仅当它看起来像地址，而不是 X500 DN）
    """
    for tag, src in ((PROP_SMTP, "smtp_prop"),
                     (PROP_SENDER_SMTP, "sender_smtp_prop"),
                     (PROP_SENT_REPR_SMTP, "sent_repr_prop")):
        v = _s(_prop(item, tag, ""))
        if v and "@" in v:
            return v, src
    if raw_addr and "@" in raw_addr and not raw_addr.startswith("/"):
        return raw_addr, "sender_field"
    # 内部发件人常常只有 X500 DN。没有地址不影响显示（有 SenderName），
    # 如实标出来即可，别硬塞一个 DN 当邮箱地址糊弄前端。
    return "", "x500_only"


def norm_message(item: Any, *, need_body: bool, me: dict | None = None) -> dict | None:
    """一封邮件 → 稳定的 dict。读不动就返回 None（跳过，不让整次取数失败）。

    每个 COM 属性访问都可能抛（权限、损坏项、同步冲突），所以全都包起来。
    实测该邮箱「同步问题」文件夹里有 71 条，损坏项是常态不是意外。
    """
    try:
        cls = _s(getattr(item, "MessageClass", ""))
    except Exception:  # noqa: BLE001
        return None
    if _skip_class(cls):
        return None
    is_meeting = cls.startswith(MEETING_CLASS_PREFIX)
    if not (cls.startswith(NOTE_CLASS_PREFIX) or is_meeting):
        return None      # 联系人、任务、日历项之类，不属于邮件列表

    try:
        entry_id = _s(getattr(item, "EntryID", ""))
        subject = _s(getattr(item, "Subject", "")) or "（无主题）"
        received = _dt(getattr(item, "ReceivedTime", None))
        conv_id = _s(getattr(item, "ConversationID", ""))
        conv_topic = _s(getattr(item, "ConversationTopic", ""))
    except Exception:  # noqa: BLE001
        return None

    # settings.outlook_skip_sender 打开时，**一个地址信息属性都不能碰** ——
    # 不是"读失败就算了"，而是根本不发起：这类读取会把 Outlook 整个卡死（见 config 注释）。
    skip_addr = bool(getattr(settings, "outlook_skip_sender", False))
    if skip_addr:
        sender_name, smtp, addr_source = "", "", "skipped"
    else:
        sender_name = _s(getattr(item, "SenderName", ""))
        raw_addr = _s(getattr(item, "SenderEmailAddress", ""))
        smtp, addr_source = _sender_smtp(item, raw_addr)

    # 「我在收件人还是只是抄送」。首选存储算好的布尔属性，但实测它有约 1/3 的邮件
    # 读不到（在线模式、26 封样本里 9 封为空）。读不到就退回比对 To/CC 字符串 ——
    # 这一步很关键：不做降级的话，那 1/3 全会落进"无法确认"，而"需我回复"这个列表
    # 里三成是猜的，用户就不会再信它。
    to_me = _prop(item, PROP_TO_ME, None)
    cc_me = _prop(item, PROP_CC_ME, None)
    # To/CC 的显示名串。除了下面的降级判定，「关系网图谱」也要靠它们才能
    # 知道谁跟谁在同一封邮件里。在线模式下每封会多两次网络往返，
    # 缓存模式下可忽略 —— 这是关系网这个功能的固定成本。
    #
    # **但 skip_addr 打开时必须连读都不读。** 这里曾经是无条件读、只在输出时
    # 清空，那是错的：To/CC **也是地址信息**，Outlook 的 Object Model Guard
    # 一样会拦它们。结果是这个专门为「碰到地址信息就卡死」准备的逆生阀
    # 根本逃不掉 —— 开了开关照样卡死。实测环境：Windows Server（无
    # root\SecurityCenter2，Outlook 拿不到"杀软有效"的答案 → 每次弹模态框）。
    if skip_addr:
        to_field = cc_field = ""
    else:
        to_field = _s(getattr(item, "To", ""))
        cc_field = _s(getattr(item, "CC", ""))
    if isinstance(to_me, bool):
        me_source = "mapi"
    elif me:
        # MAPI 的布尔位读不到，要退回比对字符串了 —— 先确保手里有显示名可比。
        # 不然内部邮件的 To/CC（装的是显示名）只能得到「判不了」，实测这是
        # 「不确定」占满整张矩阵的唯一原因。
        _learn_me_name(item, me)
        hit_to, hit_cc = _me_in(to_field, me), _me_in(cc_field, me)
        if hit_to is True or hit_cc is True:
            to_me, cc_me, me_source = hit_to is True, hit_cc is True, "display_match"
        elif hit_to is False and hit_cc is False:
            # 两边都**确定**没有我：通讯组、订阅或规则投递。
            # 这是有信息量的结论，不是"未知"。
            to_me, cc_me, me_source = False, False, "not_addressed"
        else:
            # 至少一边判不了 —— 老实说不知道，让 triage 走"不确定"那条分支。
            to_me, cc_me, me_source = None, None, "unavailable"
    else:
        me_source = "unavailable"

    # **确定**既不在收件人也不在抄送（通讯组 / 规则投递）时跳过正文。
    #
    # 这个跳过是免费的：收件方式的判定就在上面几行，本来就早于正文读取。而正文
    # 是取数耗时的大头 —— 长回复链里每封都带着全部引用历史，实测单封能到 28,000
    # 字符，跨进程搬一次的代价远超其余十几个属性之和。
    #
    # 只在 False/False（确定）时跳，None（判不了）照读 —— 宁可白读，也不要因为
    # 判不准而丢掉一封真需要你回的邮件的正文。
    #
    # 代价要说清楚：local_kind 的退订标记检测拿不到这些邮件的正文，智能看板对
    # 群组邮件的类型判定会变粗（仍有主题和发件人可用）。而它们本来就不进信息
    # 矩阵和时间图谱，所以损失局限在看板的分类精度上。
    skip_body = need_body and to_me is False and cc_me is False
    body_txt, truncated = "", False
    if need_body and not skip_body:
        # 优先纯文本。HTMLBody 实测能到 33 万字符，取它纯属浪费。
        try:
            body_txt, truncated = clean_body(_s(getattr(item, "Body", "")))
        except Exception:  # noqa: BLE001
            body_txt, truncated = "", False

    atts = _attachments(item)
    unread = bool(getattr(item, "UnRead", False))
    try:
        flag = int(getattr(item, "FlagStatus", 0) or 0)   # 2 = olFlagMarked
    except Exception:  # noqa: BLE001
        flag = 0
    try:
        # 别写 `int(x or 1)`：Importance 的合法值 0 表示"低"，被 `or` 吃掉会变成 1（普通）。
        iv = getattr(item, "Importance", 1)
        importance = int(iv) if iv is not None else 1   # 2 = High
    except Exception:  # noqa: BLE001
        importance = 1

    return {
        "id": entry_id,
        "class": cls,
        "is_meeting": is_meeting,
        "subject": subject,
        "sender_name": sender_name,
        "sender_addr": smtp,
        "addr_source": addr_source,
        "received": received.isoformat() if received else "",
        "received_ts": received.timestamp() if received else 0.0,
        "received_label": short_dt(received),
        "conv_id": conv_id or ("topic:" + conv_topic if conv_topic else ""),
        "conv_topic": conv_topic,
        "to_me": bool(to_me) if isinstance(to_me, bool) else None,
        "cc_me": bool(cc_me) if isinstance(cc_me, bool) else None,
        "me_source": me_source,
        # 收件人/抄送的**显示名字符串**（不查通讯簿，实测安全）。给「关系网图谱」
        # 用：谁跟谁在同一封邮件里出现过，是画参与者关系的唯一依据。
        # 截断到 300 字：群发邮件的收件人栏能有几千字，全存进响应没意义。
        # skip_addr 时 to_field/cc_field 本来就是空串（根本没读），不必再判一次。
        "to": to_field[:300],
        "cc": cc_field[:300],
        "unread": unread,
        "flagged": flag == 2,
        "high_importance": importance == 2,
        "attachments": atts,
        "att_count": len(atts),
        "body": body_txt,
        "body_truncated": truncated,
        # 正文是被「确定是群组邮件」跳掉的，不是这封没正文。诊断要能区分这两件事，
        # 否则看到一堆空正文会以为取数坏了。
        "body_skipped": skip_body,
    }


# ─────────────────────────────────────────────────────── 取数：排序遍历

def _newest_first(items: Any, sort_expr: str, time_attr: str) -> tuple[Any, Any, str]:
    """让遍历真的按「新 → 旧」走。返回 (第一项, 取下一项的函数, 方向判定结果)。

    实测教训（在线模式、52,100 封的收件箱）：
      items.Sort("[ReceivedTime]", True) **不抛异常但也不生效**。于是 GetFirst() 拿到的
      是整个邮箱里最老的一封，"遇到早于时间窗就 break" 立刻触发，结果一封都取不到。
      `Sort()` 没抛 ≠ 排序生效了 —— 拿这个当依据去提前 break，是在信一个撒谎的标志位。

    所以这里实测方向：读首项和末项的时间比一下。哪一头更新，就从那头走。
    代价是几次额外属性读取（在线模式下约 1 秒），换来 break 的正确性，值。

    **但不能只看端点那一封。** 实测某个委派邮箱的收件箱，首尾各两封
    都是 REPORT.IPM.Note.NDR（退信）。它们在 Outlook 对象模型里是 ReportItem，
    **类型上就没有 ReceivedTime 属性**，读出来是 AttributeError。于是方向判定拿到
    两个 None，标成 unverified、不敢提前 break，扫满 400 封上限还一封都没取到 ——
    可排序其实完全正常（往下第 3 封 06-30 22:00、第 6 封 06-30 17:13，标准降序）。
    判定失败的唯一原因是取样点恰好落在两封没有时间属性的退信上。

    所以往里多探几封，跳过读不出时间的。这几乎不额外花时间：属性在类型上不存在，
    读失败是本地 dispatch 的事，不是一次网络往返。而判错方向的代价是整窗口取空。
    """
    try:
        items.Sort(sort_expr, True)
    except Exception:  # noqa: BLE001
        pass
    try:
        probe = items.GetFirst()
    except Exception:  # noqa: BLE001
        return None, None, "walk_failed"
    if probe is None:
        return None, None, "empty"

    def _end_time(seed: str, step_name: str) -> datetime | None:
        """从一头往里走，返回第一个能读出时间的项的时间。"""
        try:
            it = getattr(items, seed)()
            step = getattr(items, step_name)
        except Exception:  # noqa: BLE001
            return None
        for _ in range(END_PROBE_TRIES):
            if it is None:
                return None
            t = _dt(getattr(it, time_attr, None))
            if t is not None:
                return t
            try:
                it = step()
            except Exception:  # noqa: BLE001
                return None
        return None

    t_first = _end_time("GetFirst", "GetNext")
    t_last = _end_time("GetLast", "GetPrevious")
    if t_first is None or t_last is None:
        # 探了这么多封还是拿不到时间，那就真没法判方向。当作降序处理（原样从头走），
        # 并如实标出来 —— 调用方看到 "unverified" 就知道不能信任提前 break。
        return items.GetFirst(), items.GetNext, "unverified"
    if t_first >= t_last:
        return items.GetFirst(), items.GetNext, "descending"
    return items.GetLast(), items.GetPrevious, "reversed"


def _walk_folder(folder: Any, *, since: datetime | None, until: datetime | None,
                 limit: int, need_body: bool, hard_scan_cap: int,
                 budget_s: float, me: dict | None = None) -> tuple[list[dict], dict]:
    """从最新往旧遍历一个文件夹。

    为什么不用 Restrict：见模块头注 1。这里靠 Sort 降序 + 遇到早于 since 就 break，
    在 13,800 封的**缓存模式**收件箱里只会真正碰到最新的几百封。

    hard_scan_cap 是防御性的：万一 Sort 没生效（某些非 Exchange store 上会），
    没有它就会把整个文件夹走完。宁可少给几条，也不要把 Outlook 卡死。

    budget_s 是**墙钟预算**，比 hard_scan_cap 更重要，实测教训：
      在**非缓存（在线模式）**的 store 上，每读一个属性都是一次网络往返，实测约
      300ms/封 —— 400 封就是 2 分钟，请求直接挂死。条数上限拦不住这种情况，因为
      问题不在条数而在单条耗时。所以必须有时间闸门：到点就带着已取到的部分返回，
      并在 stopped_by 里说明，让页面能诚实地显示「只取到了一部分」。
    """
    out: list[dict] = []
    stat = {"scanned": 0, "skipped_class": 0, "stopped_by": "end", "order": "?"}
    t_start = _now()
    try:
        items = folder.Items
    except Exception as e:  # noqa: BLE001
        raise OutlookError("读取文件夹失败：%s" % _explain_com_error(e)) from e

    try:
        it, step, order = _newest_first(items, "[ReceivedTime]", "ReceivedTime")
    except Exception as e:  # noqa: BLE001
        raise OutlookError("遍历文件夹失败：%s" % _explain_com_error(e)) from e
    stat["order"] = order
    # 只有确认了方向，才敢靠"遇到早于时间窗就 break"提前退出。
    # 方向未知时必须全扫（由条数上限和时间预算兜底），否则会像之前那样一封都取不到。
    trust_break = order in ("descending", "reversed")

    while it is not None:
        stat["scanned"] += 1
        if stat["scanned"] > hard_scan_cap:
            stat["stopped_by"] = "scan_cap"
            break
        # 时间闸门放在每轮开头：在线模式下单封就能花掉几百毫秒，
        # 只在末尾检查会先超支一整封的代价。
        if (_now() - t_start).total_seconds() > budget_s:
            stat["stopped_by"] = "time_budget"
            break
        try:
            rt = _dt(getattr(it, "ReceivedTime", None))
        except Exception:  # noqa: BLE001
            rt = None
        # until 是上界：我们从新往旧走，比上界更新的那些先出现，跳过（不计入 limit）。
        # 注意这里必须 continue 而不是 break —— break 会在还没进入区间时就退出。
        if until is not None and rt is not None and rt > until:
            stat["above_range"] = stat.get("above_range", 0) + 1
            try:
                it = step()
            except Exception:  # noqa: BLE001
                break
            continue
        if since is not None and rt is not None and rt < since:
            if trust_break:
                stat["stopped_by"] = "older_than_window"
                break
            try:
                it = step()
            except Exception:  # noqa: BLE001
                break
            continue
        m = norm_message(it, need_body=need_body, me=me)
        if m is None:
            stat["skipped_class"] += 1
        else:
            out.append(m)
            if len(out) >= limit:
                stat["stopped_by"] = "limit"
                break
        try:
            it = step()
        except Exception:  # noqa: BLE001
            break
    # 方向未知时是全扫后再排序 —— 保证返回给页面的始终是「新在前」。
    out.sort(key=lambda m: m.get("received_ts") or 0.0, reverse=True)
    return out, stat


def _sent_conversations(ns: Any, *, days: int, cap: int, budget_s: float,
                        need_promises: bool = False, promise_cap: int = 40,
                        ) -> tuple[dict[str, float], dict[str, list[float]], list[dict], dict]:
    """已发送邮件里每个会话最后一次发出的时间，顺带抽出我自己许下的承诺。

    这是「需我回复」判定的另一半：收件箱里某个会话的最后一封是别人发的，而我在
    已发送里没有更晚的同会话邮件 → 我还没回。
    只读 ConversationID 和 SentOn 两个属性，1,447 封走下来很便宜。

    ## 承诺扫描为什么放在这里，而不是单独走一趟

    「我答应过但没做」需要读已发送邮件的**正文**，比上面两个属性贵得多。但它要的
    恰好是「每个会话里我最后说的那句话」—— 而这趟遍历是**从新往旧**走的，所以
    某个 ConversationID **第一次**出现时，那就是我在这个会话里的最新一封。
    于是每个会话只需要读一次正文，天然就是最省的做法；单独再走一趟只会翻倍。

    正文是不可信输入：抽出来的句子只当字符串显示，不解释不执行。
    """
    seen: dict[str, float] = {}
    # 每个会话里我发出过的所有时间戳（时间轴用）。只存时间，不存内容。
    sent_log: dict[str, list[float]] = {}
    promises: list[dict] = []
    stat = {"scanned": 0, "with_conv": 0, "stopped_by": "end", "me_name": "",
            "promise_bodies_read": 0, "promises_found": 0}
    t_start = _now()
    try:
        # 必须和收件箱同一个 store，否则回复判断整个反掉（见 _folder_of 头注）。
        sent = _folder_of(ns, OL_FOLDER_SENT)
        items = sent.Items
    except Exception:  # noqa: BLE001
        return seen, sent_log, promises, stat
    # 和收件箱同一个坑：Sort 可能静默失效，那就会去扫**最老**的几封已发送邮件 ——
    # 对"最近这个会话我回过没有"完全没用。所以同样实测方向。
    cutoff = _now() - timedelta(days=days)
    try:
        it, step, order = _newest_first(items, "[SentOn]", "SentOn")
    except Exception:  # noqa: BLE001
        return seen, sent_log, promises, stat
    stat["order"] = order
    trust_break = order in ("descending", "reversed")
    while it is not None and stat["scanned"] < cap:
        stat["scanned"] += 1
        if (_now() - t_start).total_seconds() > budget_s:
            # 已发送扫不完不是致命问题：sent_map 少几个会话，最坏情况是某封
            # 「我其实回过了」的邮件被误判成「需我回复」。比整页挂死好得多。
            stat["stopped_by"] = "time_budget"
            break
        try:
            when = _dt(getattr(it, "SentOn", None)) or _dt(getattr(it, "ReceivedTime", None))
            cid = _s(getattr(it, "ConversationID", ""))
            if not cid:
                topic = _s(getattr(it, "ConversationTopic", ""))
                cid = ("topic:" + topic) if topic else ""
            if when is not None and when < cutoff and trust_break:
                stat["stopped_by"] = "older_than_window"
                break
            if cid:
                stat["with_conv"] += 1
                # cid 首次出现 = 我在这个会话里最新的一封。承诺只看这一封：
                # 更早那些即使有承诺，也已经被我后来的话覆盖了。
                first_time = cid not in seen
                ts = when.timestamp() if when else 0.0
                if ts > seen.get(cid, 0.0):
                    seen[cid] = ts
                # 时间轴要的是「我每一次回话」，不只是最后一次。只多记一个时间戳，
                # 不额外读任何属性 —— 这一趟本来就在遍历已发送。
                if ts:
                    log = sent_log.setdefault(cid, [])
                    if len(log) < 30:
                        log.append(ts)
                if (need_promises and first_time
                        and len(promises) < promise_cap
                        and stat["promise_bodies_read"] < promise_cap * 4):
                    stat["promise_bodies_read"] += 1
                    body, _ = clean_body(_s(getattr(it, "Body", "")), 1500)
                    text = outlook_view.promise_hint(body)
                    if text:
                        stat["promises_found"] += 1
                        promises.append({
                            "conv_id": cid,
                            "subject": _s(getattr(it, "Subject", "")) or "(无主题)",
                            # To 是显示名字符串，不查通讯簿，安全（实测过）。
                            "to": _s(getattr(it, "To", ""))[:120],
                            "text": text,
                            "sent_ts": ts,
                        })
        except Exception:  # noqa: BLE001
            pass
        try:
            it = step()
        except Exception:  # noqa: BLE001
            break
    return seen, sent_log, promises, stat


# ─────────────────────────────────────────────────────── 本地分类（零大模型）

BUCKETS = [
    {"id": "need_reply", "label": "需我回复", "hint": "会话里最后一封是别人发的，而我还没回"},
    {"id": "need_action", "label": "需我回应", "hint": "待处理的会议邀请，或带催办的截止日期"},
    {"id": "fyi", "label": "只需知道", "hint": "抄送我的、通知类、系统告警"},
]


def triage(messages: list[dict], sent_map: dict[str, float]) -> dict:
    """给每封邮件打上分类和理由。**纯本地规则，不发任何内容出去。**

    分类顺序是有意的：先判会议邀请（它不是"回邮件"），再判需回复，剩下归 FYI。
    每条都带 reason —— 用户能看懂为什么被这么分，才会信这个列表。
    """
    for m in messages:
        reasons: list[str] = []
        bucket = "fyi"

        replied_ts = sent_map.get(m["conv_id"], 0.0)
        replied = replied_ts > (m.get("received_ts") or 0.0)

        if m["is_meeting"]:
            bucket = "need_action"
            reasons.append("会议邀请待回应")
        elif m.get("to_me") is True and not replied:
            bucket = "need_reply"
            reasons.append("直接发给我，尚未回复")
        elif m.get("to_me") is None and m.get("cc_me") is None and not replied:
            # MAPI 属性读不到时的退路：不敢断言，但也不能一律当 FYI 埋掉。
            bucket = "need_reply"
            reasons.append("尚未回复（无法确认我是否在收件人栏）")
        elif replied:
            reasons.append("我已在此会话回过")
        elif m.get("cc_me") is True:
            reasons.append("只是抄送我")
        elif m.get("to_me") is False and m.get("cc_me") is False:
            # 收件人和抄送里都没有我 —— 通常是通讯组、订阅或规则投递进来的。
            # 这本身就是「不需要我回」的有力信号，别留一条没有理由的卡片。
            reasons.append("收件人栏里没有我（通讯组或规则投递）")

        dl = m.get("deadline")
        if dl:
            if dl.get("date"):
                left = dl.get("days_left")
                reasons.append("提到截止 %s%s" % (
                    dl["date"], "（还剩 %d 天）" % left if isinstance(left, int) else ""))
            else:
                reasons.append("出现催办词「%s」" % dl.get("trigger", ""))
            if bucket == "fyi":
                bucket = "need_action"

        if m.get("flagged"):
            reasons.append("我给它加过标记")
            if bucket == "fyi":
                bucket = "need_action"
        if m.get("high_importance"):
            reasons.append("发件人标为高重要性")
        if m.get("att_count"):
            reasons.append("%d 个附件" % m["att_count"])

        m["bucket"] = bucket
        m["replied"] = replied
        m["reasons"] = reasons

    counts = {b["id"]: 0 for b in BUCKETS}
    for m in messages:
        counts[m["bucket"]] = counts.get(m["bucket"], 0) + 1
    return counts


# ─────────────────────────────────────────────────────── 对外：同步实现

def _stores_sync() -> list[dict]:
    """profile 里的 store 列表。

    列出来有两个用途：页面明确显示「正在读哪个邮箱」，以及给切换用的下拉当选项。
    没选过时读默认邮箱；选了就只读选中那一个，**不会把多个邮箱合并读进来**。
    """
    ns = _session.ns_retry()
    out: list[dict] = []
    try:
        n = ns.Stores.Count
    except Exception as e:  # noqa: BLE001
        raise OutlookError(_explain_com_error(e)) from e
    for i in range(1, n + 1):
        try:
            st = ns.Stores.Item(i)
            out.append({
                "index": i,
                "display_name": _s(getattr(st, "DisplayName", "")),
                # 同上：ExchangeStoreType 的 0 是"主 Exchange 邮箱"，也就是最常见的
                # 那一种。写成 `or -1` 会把它显示成 -1（实测就踩了）。
                "exchange_type": _int_or(getattr(st, "ExchangeStoreType", None), -1),
                "cached": bool(getattr(st, "IsCachedExchange", False)),
                "is_data_file": bool(getattr(st, "IsDataFileStore", False)),
            })
        except Exception:  # noqa: BLE001
            continue
    return out


def _store_pref_file():
    """选中的邮箱记在这里（只存一个显示名，不存任何邮件内容）。

    为什么记在后端而不是浏览器 localStorage：读邮箱的不只有那个页面。「个人摘记」
    （memo.py）是后台按间隔自己跑的，它身边没有浏览器。选择只存在前端的话，摘记
    会永远读默认邮箱 —— 页面显示一个邮箱、推送来自另一个，而且没有任何地方看得
    出来。存后端两边才是同一个事实。
    """
    return DATA_ROOT / "outlook_store.json"


def _read_store_pref() -> str:
    """返回选中的邮箱显示名；没选过 / 文件坏了都返回空串（= 用默认邮箱）。"""
    try:
        d = json.loads(_store_pref_file().read_text(encoding="utf-8"))
        return str(d.get("display_name") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _write_store_pref(name: str) -> None:
    _store_pref_file().write_text(
        json.dumps({"display_name": (name or "").strip()}, ensure_ascii=False),
        encoding="utf-8")


def _find_store(ns: Any, name: str) -> Any:
    """按显示名找 store。找不到返回 None —— 调用方负责回落，别在这里抛。"""
    try:
        n = ns.Stores.Count
    except Exception:  # noqa: BLE001
        return None
    for i in range(1, n + 1):
        try:
            st = ns.Stores.Item(i)
            if _s(getattr(st, "DisplayName", "")) == name:
                return st
        except Exception:  # noqa: BLE001
            continue
    return None


def _folder_of(ns: Any, folder_id: int) -> Any:
    """按当前选中的邮箱取收件箱 / 已发送。没选过就是默认邮箱，行为和以前一样。

    **收件箱和已发送必须走同一个 store。** 只切收件箱的话，「这个会话我回过没有」
    会拿 A 邮箱的收件箱去比 B 邮箱的已发送 —— B 里每一封其实回过的邮件都会被判成
    「等你回」，看板的主结论整个反掉，比不做这个功能还糟。所以两处共用这一个入口，
    而不是各自去 GetDefaultFolder。

    实测（本机 profile，两个 store）：委派邮箱（ExchangeStoreType=1）上
    ``Store.GetDefaultFolder(6/5)`` 可用，且返回的文件夹确实属于该 store
    （收件箱 7773 封 / 已发送 450 封）。所以不需要按文件夹名去猜 —— 那条路要赌
    「收件箱」还是 "Inbox"，跟 Outlook 界面语言绑定，是个长期的坑。
    """
    want = _read_store_pref()
    if want:
        st = _find_store(ns, want)
        if st is not None:
            try:
                return st.GetDefaultFolder(folder_id)
            except Exception as e:  # noqa: BLE001
                raise OutlookError(
                    "邮箱「%s」里打不开这个文件夹：%s"
                    % (want, _explain_com_error(e))) from e
        # 选的那个邮箱不在了（profile 改过、共享邮箱权限被收回）。这里**回落到默认
        # 邮箱而不是报错**：报错会让整页打不开，而用户多半已经不记得自己选过谁。
        # 但必须记一条日志，否则"为什么读的不是我选的那个"无从查起。
        log.warning("选中的邮箱 %r 不在当前 profile 里，回落到默认邮箱", want)
    try:
        return ns.GetDefaultFolder(folder_id)
    except Exception as e:  # noqa: BLE001
        raise OutlookError(_explain_com_error(e)) from e


def _default_inbox(ns: Any) -> Any:
    """名字保留：调用方语义就是「该读的那个收件箱」，现在它认选择。"""
    return _folder_of(ns, OL_FOLDER_INBOX)


def _parse_day(v: str, *, end: bool) -> datetime | None:
    """YYYY-MM-DD → 本地时区的边界时刻。end=True 取当天 23:59:59（含当天）。"""
    v = (v or "").strip()
    if not v:
        return None
    try:
        y, m, d = (int(x) for x in v.replace("/", "-").split("-")[:3])
    except Exception as e:  # noqa: BLE001
        raise OutlookError("日期格式应为 YYYY-MM-DD，收到：%s" % v) from e
    tz = _now().tzinfo
    if end:
        return datetime(y, m, d, 23, 59, 59, tzinfo=tz)
    return datetime(y, m, d, 0, 0, 0, tzinfo=tz)


def _assemble_views(view: dict, msgs: list[dict],
                    sent_log: dict[str, list[float]]) -> tuple[dict, dict, dict]:
    """把欠账表的会话折叠结果，摊成三个视图需要的形状。**纯本地，无 COM 无 AI。**

    三个视图共用同一份会话列表，不各自重算：
      · 智能看板  —— 会话 + 四个维度标签（责任维度直接复用 build_view 的分区结论，
                    那套判定有 52 个断言守着，重算只会产生两套打架的口径）
      · 信息矩阵  —— 会话 + 字段单元格（正则列本地算，语义列留空等 AI）
      · 时间图谱  —— 往来 ≥2 封的会话，确定性时间轴 + 参与者共现图
    图谱在这里就算好、不做成第二个接口：再来一次就要么重跑 COM（在线模式下几十秒），
    要么维护第二份缓存。而 ≤12 个会话的图谱是纯计算，顺手算完最省事。
    """
    by_conv: dict[str, list[dict]] = {}
    for m in msgs:
        key = (m.get("conv_id") or "").strip() or ("id:" + str(m.get("id") or ""))
        by_conv.setdefault(key, []).append(m)
    for v in by_conv.values():
        v.sort(key=lambda x: x.get("received_ts") or 0.0, reverse=True)

    threads = list(view["due"]) + list(view["waiting"]) + list(view["quiet"]["threads"])
    promised = {p.get("conv_id") for p in view.get("promises") or []}
    outlook_tags.tag_threads(threads, by_conv, promised_convs=promised)

    # 排除规则（群组邮件 + 噪音类型）在这里先按**本地判出的 kind** 跑一遍；
    # AI 写回之后 inbox() 会再调 apply_exclusions 重跑，把被判成「订阅资讯」那类摘掉。
    # 见 outlook_tags.apply_exclusions 的说明。
    projects = outlook_graph.pick_projects(threads, by_conv)
    graphs: dict[str, dict] = {}
    for p in projects:
        conv = p["conv_id"]
        conv_msgs = by_conv.get(conv) or []
        # 时间轴上每一步的「他在要什么」。只给要画图谱的这几个会话算，不是全量。
        for m in conv_msgs:
            if "ask_hint" not in m:
                m["ask_hint"] = outlook_view.extract_ask(
                    m.get("subject") or "", m.get("body") or "")
        t = next((x for x in threads if x.get("conv_id") == conv), {})
        graphs[conv] = outlook_graph.build_graph(t, conv_msgs, sent_log.get(conv) or [])

    # 「我答应过」这一维必须进来，但它的来源是**我自己的已发送邮件**，不是收件箱
    # 会话 —— 如果只用 threads，这个功能就整个消失了（而它恰好是这个面板相对
    # Outlook 唯一不可替代的能力）。所以把承诺包成同形的伪会话塞进来。
    #
    # 注意顺序：伪会话必须在**建矩阵行之前**塞进来。矩阵现在是页面上唯一的列表
    # （分面格子只负责筛选，不再另外渲染一遍卡片），按「我答应过」筛出一张空表
    # 就等于这个功能又没了。matrix_row 里对 promise 有专门的取值分支。
    for p in view.get("promises") or []:
        threads.append({
            "conv_id": p.get("conv_id") or "",
            "open_id": "", "subject": p.get("subject") or "",
            "people": [p.get("to") or "(收件人未知)"],
            "msg_count": 0, "chase_count": 0, "ask": "",
            "waiting": False, "waiting_days": 0, "waiting_label": "",
            # 承诺来自我自己的已发送邮件。这里填上发出时间：矩阵要按时间倒序排，
            # 没有这个键它会被排到最末（空串最小），而「我答应过」恰恰是最该看的。
            "last_received": _iso_or_empty(p.get("sent_ts")),
            "last_received_label": "",
            "replied_before": True, "replied_days_ago": p.get("age_days"),
            "att_count": 0, "high_importance": False, "flagged": False,
            "is_meeting": False, "urgent_word": "",
            "to_me": None, "cc_me": None, "deadline": None,
            "body_preview": p.get("text") or "", "message_ids": [],
            "section": "promise",
            "why": "%s 天前你答应过：%s" % (p.get("age_days"), p.get("text") or ""),
            "promise": p,
            "tags": {"duty": "promised", "urgency": "none",
                     "weight": "mid", "kind": "", "kind_from": ""},
        })

    # 矩阵行在这里才建：上面刚把承诺伪会话追加进 threads。
    # 承诺是我自己发出的，addressing 是「我发出」，不会被群组邮件过滤掉。
    rows = [outlook_tags.matrix_row(t, by_conv.get(t.get("conv_id") or "") or [])
            for t in threads]

    # 矩阵按时间**倒序**：最近的在第一行。看板那三段各有自己的排序口径
    # （到期近的优先 / 等得久的优先），表格不跟它们一致是刻意的 —— 表格是拿来扫的。
    rows.sort(key=lambda r: r.get("sort_key") or "", reverse=True)

    board = {
        "dimensions": outlook_tags.DIMENSIONS,
        "counts": {},          # 占位，下面 apply_exclusions 之后才算
        "threads": threads,
    }
    matrix = {"fields": outlook_tags.MATRIX_FIELDS, "rows": rows}
    graph = {"projects": projects, "graphs": graphs}
    # 排除必须**报出来**：矩阵少了几行、为什么少，用户有权知道。
    outlook_tags.apply_exclusions(threads, matrix, graph)
    # counts **必须在 apply_exclusions 之后**：分面只统计进了矩阵的会话，否则
    # 看板上「广告推广 12」点下去矩阵是空的。调用方在 AI 填完 kind 后还要重算一次。
    board["counts"] = outlook_tags.dimension_counts(threads)
    return board, matrix, graph


def _inbox_sync(*, window_days: int, limit: int,
                need_body: bool, with_reply_check: bool,
                since_day: str = "", until_day: str = "") -> dict:
    """收件箱一次完整取数 + 本地分类。这是「打开即看」页面的唯一后端动作。

    **只读收件箱根目录，不看任何子文件夹。** 早先有个 folder 参数能指向
    「收件箱/某个子文件夹」，去掉了：规则分流出来的子文件夹（实测该收件箱下有
    Alerts 1710 封 / Microsoft 6722 封这类噪音桶）本来就是用户主动分走的东西，
    读进来只会稀释信号。而且把入口拆掉比「靠调用方不传参」可靠 ——
    现在「只看收件箱」是结构性的保证，不是约定。

    注意 _walk_folder 本来就不递归子文件夹（它只走 folder.Items），
    所以这里换成 _default_inbox 就是完整的保证，不需要额外过滤。

    「只读收件箱根目录」和「读哪个邮箱」是两件事：前者写死不给选（子文件夹是噪音），
    后者可选（见 _folder_of）。一个 profile 里挂着别人的委派邮箱是常态，而那里的
    邮件同样是用户自己要处理的。
    """
    t0 = _now()
    ns = _session.ns_retry()
    target = _default_inbox(ns)

    # 缓存模式 vs 在线模式决定了取数快慢，差两个数量级（实测在线模式约 300ms/封）。
    # 页面要能显示这一点，否则用户只会觉得"这功能好慢"，不知道原因也不知道怎么改。
    cached_store: bool | None = None
    try:
        cached_store = bool(target.Store.IsCachedExchange)
    except Exception:  # noqa: BLE001
        cached_store = None

    # 显式日期区间优先于「最近 N 天」。两者都给时以区间为准。
    since = _parse_day(since_day, end=False)
    until = _parse_day(until_day, end=True)
    if since is None:
        since = _now() - timedelta(days=max(1, window_days))
    scan_cap = int(getattr(settings, "outlook_scan_limit", 400))
    budget = float(getattr(settings, "outlook_time_budget_s", 10))

    # 顺序是刻意的：**先扫已发送邮件，再走收件箱**。
    # 已发送这一趟本来就要做（判断"这个会话我回过没有"），而它顺手就能给出我的
    # 显示名（我自己发的邮件的 SenderName）—— 收件箱归一化时正需要它来做
    # 「我在收件人栏吗」的降级判断。反过来的话就得去查通讯簿，而那会挂死（见 _me）。
    sent_map: dict[str, float] = {}
    promises: list[dict] = []
    sstat: dict = {}
    want_promises = False          # 先定义：下面的诊断块无条件读它
    if with_reply_check:
        # 承诺扫描要读已发送邮件的正文，在**在线模式**下每封都是一次网络往返
        # （实测约 6 秒/封），会把预算全吃掉。所以在线模式下直接关掉：
        # 少一栏「你答应过」，好过整页超时。cached_store 读不到（None）时照常开，
        # 保守但不至于在正常机器上白白丢功能。
        want_promises = (bool(getattr(settings, "outlook_promise_scan", True))
                         and cached_store is not False)
        sent_map, sent_log, promises, sstat = _sent_conversations(
            ns,
            days=int(getattr(settings, "outlook_sent_scan_days", 180)),
            cap=int(getattr(settings, "outlook_sent_scan_limit", 800)),
            # 已发送扫描给一半预算：它只是为了判断"回过没有"，
            # 不该跟主列表抢时间。扫不完的后果是有限的（见函数内注释）。
            budget_s=max(3.0, budget / 2),
            need_promises=want_promises,
            promise_cap=int(getattr(settings, "outlook_promise_cap", 40)),
        )
    me = _me(ns, sender_name=sstat.get("me_name", ""))

    msgs, wstat = _walk_folder(
        target, since=since, until=until, limit=limit, need_body=need_body,
        hard_scan_cap=max(scan_cap, limit * 3),
        budget_s=budget, me=me,
    )

    # 去重：实测最新 5 封里有两封完全相同（重复投递）。
    seen: set[tuple] = set()
    deduped: list[dict] = []
    dropped_dupes = 0
    for m in msgs:
        key = (m["subject"], m["sender_name"], round(m.get("received_ts") or 0.0))
        if key in seen:
            dropped_dupes += 1
            continue
        seen.add(key)
        deduped.append(m)
    msgs = deduped

    for m in msgs:
        m["deadline"] = deadline_hint(m["subject"], m.get("body") or "")

    counts = triage(msgs, sent_map)
    # 按「欠着什么」而不是「什么到了」重组。三个视图都建在它的会话折叠结果上。
    view = outlook_view.build_view(msgs, sent_map, promises=promises, now=t0)
    board, matrix, graph = _assemble_views(view, msgs, sent_log)

    # 诊断块：这些字段是我在没有真实邮箱的开发机上验不了的东西（MAPI 属性可用性、
    # 排序是否生效、SMTP 地址取得到几个）。让第一次真机运行自己把答案报出来，
    # 比再来一轮探测脚本快。
    addr_src: dict[str, int] = {}
    me_src: dict[str, int] = {}
    for m in msgs:
        addr_src[m["addr_source"]] = addr_src.get(m["addr_source"], 0) + 1
        me_src[m["me_source"]] = me_src.get(m["me_source"], 0) + 1

    return {
        "folder": "收件箱",
        "window_days": window_days,
        "range": {"since": since.strftime("%Y-%m-%d") if since else "",
                  "until": until.strftime("%Y-%m-%d") if until else "",
                  "explicit": bool(since_day or until_day)},
        "generated_at": t0.isoformat(),
        "elapsed_ms": int((_now() - t0).total_seconds() * 1000),
        "buckets": BUCKETS,
        "counts": counts,
        "view": view,
        # 三个视图。board.threads 和 matrix.rows 指向同一批会话对象，
        # AI 层会原地补字段（见 outlook_ai.apply_to），所以两边会同步生效。
        "board": board,
        "matrix": matrix,
        "graph": graph,
        "total": len(msgs),
        "messages": msgs,
        # partial=True 时页面必须显示「只取到一部分」。悄悄给个短列表，
        # 用户会以为"就这几封"，而真相是超时了 —— 那比慢更糟。
        "partial": wstat.get("stopped_by") in ("time_budget", "scan_cap"),
        "cached_store": cached_store,
        "sender_skipped": bool(getattr(settings, "outlook_skip_sender", False)),
        "diagnostics": {
            "scanned": wstat.get("scanned"),
            "above_range": wstat.get("above_range", 0),
            "skipped_non_mail": wstat.get("skipped_class"),
            "stopped_by": wstat.get("stopped_by"),
            "order": wstat.get("order"),
            "time_budget_s": budget,
            "cached_store": cached_store,
            "dropped_duplicates": dropped_dupes,
            "sent_scanned": sstat.get("scanned", 0),
            "sent_stopped_by": sstat.get("stopped_by"),
            "sent_conversations": len(sent_map),
            # 承诺扫描的成本和产出都要报出来：读了多少封正文、抽到几条。
            # 这一栏是启发式的，用户得能判断它到底在不在工作。
            "promise_scan": want_promises,
            "promise_bodies_read": sstat.get("promise_bodies_read", 0),
            "promises_found": sstat.get("promises_found", 0),
            "sender_addr_source": addr_src,
            "to_me_source": me_src,
            # 「我是谁」解析到了没有，是「不确定」占满矩阵时的第一个要看的地方：
            # 没有显示名 → To/CC（装的是显示名）就只能答判不了 → 全是不确定。
            # name_from 取值：received_by（从邮件属性学到，正常情况）/
            # received_by_unverified（学到了但没有地址可交叉校验）/
            # sent_sender_name（调用方传进来的，实测基本拿不到）/ none。
            "me_resolved": {"has_name": bool(me.get("name")),
                            "name_from": me.get("name_source") or (
                                "sent_sender_name" if me.get("name") else "none"),
                            "name_tries": me.get("name_tries", 0),
                            "alias_count": len(me.get("aliases") or [])},
            # 确定是群组邮件而跳过的正文数。这是取数耗时的主要节省项，
            # 也是判断「为什么这次比上次快」的依据。
            "bodies_skipped": sum(1 for m in msgs if m.get("body_skipped")),
        },
    }


# ─────────────────────────────────────────────────────── 对外：异步包装

async def stores() -> list[dict]:
    return await run_com(_stores_sync, timeout=15)


async def store_choice() -> dict:
    """当前选了哪个邮箱。空 display_name = 跟随默认邮箱。"""
    return {"display_name": _read_store_pref()}


async def set_store_choice(name: str) -> dict:
    """切换要读的邮箱。空串 = 回到默认邮箱。

    **切完必须清缓存。** 收件箱结果有 5 分钟内存 TTL，不清的话切换后原样返回上一个
    邮箱的快照，看起来就是"切了没反应"，而用户下一步多半是再点一次、再等一次。
    """
    name = (name or "").strip()
    if name:
        avail = [s.get("display_name") for s in await stores()]
        if name not in avail:
            raise OutlookError(
                "这个 profile 里没有名为「%s」的邮箱。现在能选的是：%s"
                % (name, "、".join(x for x in avail if x) or "（一个都没读到）"))
    _write_store_pref(name)
    cache_clear()
    return {"display_name": name}


# ─────────────────────────────────────────────────────── 内存缓存
#
# 为什么缓存：在线模式下取满字段约 6 秒/封（实测），一页 40 封就是 4 分钟。
# 没有缓存的话每次切标签、改筛选都要重扛一遍，页面根本没法用。
#
# 为什么**只放内存、不落盘**：缓存的是真实邮件的主题和正文。落盘就等于在磁盘上
# 多存一份邮件副本 —— 这个功能的前提是"数据不出这台机器"，但"不出机器"不等于
# "可以到处写副本"。进程退出即消失，这个取舍是刻意的，别改成磁盘缓存。
_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: tuple, ttl: float) -> dict | None:
    with _cache_lock:
        hit = _cache.get(key)
    if not hit:
        return None
    ts, data = hit
    age = _now().timestamp() - ts
    if age > ttl:
        return None
    out = dict(data)
    out["cached"] = True
    out["cache_age_s"] = int(age)
    return out


def _cache_put(key: tuple, data: dict) -> None:
    with _cache_lock:
        # 上限很小：这是给"同一个视图反复刷新"用的，不是给全邮箱做索引。
        if len(_cache) > 24:
            _cache.clear()
        _cache[key] = (_now().timestamp(), data)


def cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


async def inbox(*, window_days: int = 3, limit: int = 60,
                need_body: bool = True, with_reply_check: bool = True,
                since_day: str = "", until_day: str = "",
                refresh: bool = False) -> dict:
    # 邮箱名进缓存键：否则切换邮箱后 TTL 内会命中上一个邮箱的结果。set_store_choice
    # 已经清过缓存，这里是第二道 —— 手改 outlook_store.json 之类的路径绕不过去。
    key = (window_days, limit, need_body, with_reply_check, since_day, until_day,
           _read_store_pref())
    ttl = float(getattr(settings, "outlook_cache_ttl_s", 300))
    if not refresh:
        hit = _cache_get(key, ttl)
        if hit is not None:
            return hit

    def _job() -> dict:
        return _inbox_sync(window_days=window_days, limit=limit,
                           need_body=need_body, with_reply_check=with_reply_check,
                           since_day=since_day, until_day=until_day)

    data = await run_com(_job)

    # ── AI 语义层。**必须在 COM 线程之外**：它是网络 IO，丢进那个单线程
    # executor 会把 Outlook 的访问也一起堵住。
    # 放在写缓存之前，这样缓存里存的是带 AI 结论的完整结果，命中缓存时不再外发。
    # 失败/未配置只会少几个字段，不抛异常（见 outlook_ai 头注）。
    from . import outlook_ai
    board = data.get("board") or {}
    by_conv: dict[str, list[dict]] = {}
    for m in data.get("messages") or []:
        k = (m.get("conv_id") or "").strip() or ("id:" + str(m.get("id") or ""))
        by_conv.setdefault(k, []).append(m)
    ai = await outlook_ai.enrich(board.get("threads") or [], by_conv)
    outlook_ai.apply_to(board.get("threads") or [],
                        (data.get("matrix") or {}).get("rows") or [], ai["items"])
    # 必须在 apply_to 之后重算：AI 刚把一批会话的 kind 从空串改成了 decision/
    # business/admin，而计数是在那之前算的。不重算的话「类型」这一维的标签上永远
    # 写着「未分类 N」，点进去却有内容 —— 一个自相矛盾的筛选器。
    if board:
        # 顺序不能颠倒：排除规则要看 kind，而「订阅资讯」这类是 AI 刚填上的；
        # 而分面计数又只统计「进了矩阵的会话」，所以必须**先排除、再计数**。
        outlook_tags.apply_exclusions(board.get("threads") or [],
                                      data.get("matrix") or {},
                                      data.get("graph") or {})
        board["counts"] = outlook_tags.dimension_counts(board.get("threads") or [])
    # 把外发情况如实报给前端：发了几批、多少字、缓存命中多少、有没有失败。
    # 用户同意了全量外发，但他有权随时看见这一次到底发出去了多少。
    data["ai"] = ai["stat"]

    data["cached"] = False
    data["cache_age_s"] = 0
    # 真正去读 Outlook 的时刻。**它会随缓存一起被保留** —— 从 5 分钟内存缓存
    # 命中时，页面该显示的是「上次真取数的时间」，而不是这次 HTTP 请求的时间。
    # 后者会让用户以为刚刚刷过，实际看的是几分钟前的快照。
    data["fetched_at"] = _now().isoformat(timespec="seconds")
    # 只缓存"取全了"的结果。partial 说明是超时截断的，缓存它等于把一个不完整的
    # 列表钉住 TTL 那么久 —— 用户刷新也刷不出更多，还以为就这些。
    if not data.get("partial"):
        _cache_put(key, data)
    return data


async def probe() -> dict:
    """轻量连通性检查。页面加载时先问这个，好在取数前给出人话诊断。"""
    def _job() -> dict:
        try:
            ns = _session.ns_retry()
            inbox_f = _default_inbox(ns)
            # 缓存模式和收件箱规模都在这里一并给出：页面要能在**取数之前**判断
            # 「这次大概率会超时」，而不是让用户转圈 45 秒再收到一句 COM 报错。
            # 这两个读取都不是「地址信息」，不会触发 Object Model Guard —— 这也是
            # probe 在会挂死的机器上依然能秒回的原因。
            cached: bool | None
            store_name = ""
            try:
                st = inbox_f.Store
                cached = bool(st.IsCachedExchange)
                store_name = _s(getattr(st, "DisplayName", ""))
            except Exception:  # noqa: BLE001
                cached = None
            return {
                "ok": True,
                "stores": ns.Stores.Count,
                "inbox_items": int(inbox_f.Items.Count or 0),
                "inbox_unread": int(inbox_f.UnReadItemCount or 0),
                "cached": cached,
                "store_name": store_name,
            }
        except OutlookError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _explain_com_error(e)}

    return await run_com(_job)
