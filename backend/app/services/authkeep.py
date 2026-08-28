"""飞书用户授权保活 —— 定期做一次极轻的用户身份调用，把 7 天滚动窗口往后推。

## 为什么需要

飞书的用户身份令牌是两层：access token 2 小时（自动续、无感），refresh token
**7 天滚动** —— 每次**真实的 user API 调用**都会换一个新的、窗口顺延 7 天。
连续七天没有产生过任何 user 调用，授权就失效，必须重新登录。

而这个应用是托盘程序、不是服务：关掉、重启、机器关机期间一次调用都没有。实测就
撞上了 —— 用户几天没开应用，个人摘记直接推送失败。

## 它治什么、不治什么

**治**：「开着应用但七天没碰过需要飞书的功能」。这是最常见的情形，因为大部分场景
（本地邮箱、自动化提炼、个人摘记走机器人身份）根本不调用户身份接口。

**不治**：「机器关了一周」。进程不在，什么都做不了。所以这是**减少发生概率**，
不是根治。真正的根治是应用身份（见 selfpush 的 identity=bot），但那只覆盖推送；
飞书文档 / 知识治理 / 协作分发都必须用用户身份。

## 间隔为什么是 12 小时

access token 有 2 小时寿命，**只有它过期时那次调用才会触发刷新**。间隔小于 2 小时
的话大多数轮次是空转（调用发生了，但令牌没换、窗口没动）。12 小时既保证每次都真的
触发刷新，又留出足够余量（7 天窗口 ÷ 12 小时 = 14 次机会）。

## 用哪个调用

``im_chat_list(page_size=1)`` —— 已有的封装，用户身份、只读、单条返回。
实测 2.5 秒、把窗口从 08-31 推到了 09-01。

选它而不是 ``auth status`` 是关键：``auth status`` **只读本地状态、不触发刷新**
（实测确认过）。保活必须是真的调一次接口。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from ..config import settings
from ..feishu import get_lark

log = logging.getLogger("authkeep")

# 启动后先等一会儿再做第一次：不拖慢启动，也避开启动时那一堆并发检查。
_FIRST_DELAY_S = 90
_MIN_HOURS = 3          # 低于 3 小时基本是空转（access token 2 小时），直接夹上来

_state: dict = {
    "enabled": False,
    "every_hours": 0,
    "last_at": "",
    "last_ok": None,        # None = 还没跑过
    "last_error": "",
    "runs": 0,
    "refresh_expires_at": "",   # 最近一次观察到的 refresh token 到期时间
    "days_left": None,          # 距到期还有几天（负数 = 已过期）
}
_task: asyncio.Task | None = None


def status() -> dict:
    return dict(_state)


def _note_expiry(st: dict) -> None:
    """把观察到的到期时间记下来。有了它，诊断页才能显示「授权还剩几天」——
    应用其实一直知道这个数，之前只是没往外说。"""
    raw = str(st.get("refresh_expires_at") or "")
    _state["refresh_expires_at"] = raw
    _state["days_left"] = None
    if not raw:
        return
    try:
        exp = dt.datetime.fromisoformat(raw)
        now = dt.datetime.now(exp.tzinfo) if exp.tzinfo else dt.datetime.now()
        _state["days_left"] = round((exp - now).total_seconds() / 86400, 1)
    except Exception:  # noqa: BLE001
        pass


async def touch() -> dict:
    """做一次保活调用。**绝不抛异常** —— 它只是后台维护，不该影响任何前台功能。"""
    lark = await get_lark()
    try:
        st = await lark.auth_status()
    except Exception as e:  # noqa: BLE001
        _state.update({"last_at": dt.datetime.now().isoformat(timespec="seconds"),
                       "last_ok": False, "last_error": "读取登录状态失败：%s" % e})
        return status()
    _note_expiry(st)

    # 看 user_usable 而不是 authenticated：needs_refresh 时 authenticated=False，
    # 但那恰恰是**最该保活的时刻** —— refresh token 还有效，发一次调用就能把 7 天
    # 窗口推到 now+7d；跳过它就等着窗口走完、用户被迫重新登录。
    # 这里曾经写 `if not st.get("authenticated")`，于是保活在唯一有用的场景里空转。
    if not st.get("user_usable", st.get("authenticated")):
        # 真的没登录（missing / expired）才跳过。**不要在这里尝试登录** —— 那要用户
        # 在浏览器里完成设备码流程，后台静默触发只会让人莫名其妙。
        _state.update({"last_at": dt.datetime.now().isoformat(timespec="seconds"),
                       "last_ok": False,
                       "last_error": "未登录，跳过（需要用户重新授权）：%s"
                                     % (st.get("user_status") or "unknown")})
        return status()

    try:
        await lark.im_chat_list(page_size=1)
        _state.update({"last_at": dt.datetime.now().isoformat(timespec="seconds"),
                       "last_ok": True, "last_error": "",
                       "runs": int(_state["runs"]) + 1})
        # 调用之后再读一次，把顺延后的到期时间记下来（这才是保活是否生效的证据）。
        try:
            _note_expiry(await lark.auth_status())
        except Exception:  # noqa: BLE001
            pass
        log.info("auth keepalive ok, refresh window now %s", _state["refresh_expires_at"])
    except Exception as e:  # noqa: BLE001
        _state.update({"last_at": dt.datetime.now().isoformat(timespec="seconds"),
                       "last_ok": False, "last_error": "%s: %s" % (type(e).__name__, e)})
        log.warning("auth keepalive failed: %s", e)
    return status()


async def _loop(every_hours: int) -> None:
    try:
        await asyncio.sleep(_FIRST_DELAY_S)
        while True:
            await touch()
            await asyncio.sleep(every_hours * 3600)
    except asyncio.CancelledError:
        pass


def start() -> None:
    """在应用启动时调用。``AUTH_KEEPALIVE_HOURS=0`` 可关掉。"""
    global _task
    hours = int(getattr(settings, "auth_keepalive_hours", 12) or 0)
    if hours <= 0:
        _state.update({"enabled": False, "every_hours": 0})
        log.info("auth keepalive disabled")
        return
    hours = max(_MIN_HOURS, hours)
    _state.update({"enabled": True, "every_hours": hours})
    if _task and not _task.done():
        _task.cancel()
    _task = asyncio.create_task(_loop(hours))
    log.info("auth keepalive started: every %dh", hours)


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None
    _state["enabled"] = False
