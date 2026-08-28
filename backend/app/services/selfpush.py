"""把「总结」定时推到你自己的飞书 —— 目前唯一的去处是你的自己会话。

## 为什么单独一个模块

「自动化提炼」现在要用它，「本地邮箱」将来也要用。发的是同一形状的东西、去同一个
地方。要是写在各自的 service 里，身份解析 / 失败分类 / 去重键会有三份实现，而这
三件恰恰是最容易悄悄错的：身份错了发给别人，失败分类错了静默停摆，去重键错了
重复轰炸。

## 发送身份：默认机器人，用户身份是退路

内置应用**两个都有**：``im:message.send_as_bot``（应用身份）和
``im:message.send_as_user``（用户身份）。

我一开始判断错了，以为只有后者 —— 因为 ``auth status`` 输出的 scope 列表是
**用户身份**的清单，应用身份权限根本不在里面。实测 ``--as bot`` 直接就通。
所以：**别拿 auth status 的 scope 列表去判断应用身份能做什么。**

选机器人是因为令牌寿命完全不同：

  bot   应用级令牌（tenant_access_token，由 app id/secret 自己换）—— **永不过期**
  user  refresh token **7 天滚动**：任何一次 user API 调用都会把窗口往后推，
        但连续七天没产生过调用就失效，必须重新登录

定时任务无人值守，恰恰最容易撞上后者 —— 实测就撞上了：用户连续几天没开应用，
摘记直接推送失败。所以默认 auto = 能用机器人就用机器人（见 config.selfpush_identity）。

代价是飞书里显示为机器人发来的，不是你自己发的；落在机器人和你的私聊里，
而不是你的自己会话。

## 所以这里绝不吞掉认证失败

静默停摆是这类功能最糟的失败方式 —— 你以为它在替你盯着，其实它上周就死了。
``needs_login`` 单独分类、单独记住，并一路暴露到页面上。

而 ``needs_refresh`` **不算失败**：实测它会在下一次 user API 调用时自动刷好，而
本模块的发送本身就是那次调用。把它当失败处理会得到一个「第一次总是失败、
第二次就好」的诡异功能。

## open_id 不查通讯录

``lark-cli auth status`` 本地就返回当前用户的 open_id，不必调 contact 接口 ——
少一次网络往返，也少一个会失败的环节。顺带也不需要 ``contact:user:search``。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from ..config import DATA_ROOT, settings
from ..feishu import LarkCLIError, get_lark

log = logging.getLogger("selfpush")

# 单条消息的字符上限。超了截断并在末尾说明 —— 宁可少说，也不要因为超长被飞书拒收
# 而整条丢掉（那样你什么都收不到，还不知道为什么）。
MAX_CHARS = 3500

_self: dict[str, str] = {"open_id": "", "name": "", "identity": ""}

# 最近一次推送的结果。给页面看的：这个功能的失败必须是可见的。
_last: dict = {
    "at": "",              # 最近一次尝试的时间
    "ok": None,            # None = 还没推过
    "error": "",
    "needs_login": False,  # True = 要重新登录，不是暂时性故障
    "sent": 0,             # 累计成功推送条数（本进程内）
}


class PushError(RuntimeError):
    """``needs_login=True`` 表示要用户重新授权，重试没有意义。"""

    def __init__(self, message: str, *, needs_login: bool = False):
        super().__init__(message)
        self.needs_login = needs_login


def last() -> dict:
    """最近一次推送结果的快照，直接塞进各场景的 status 响应。

    带上 ``identity``：页面要能看出现在是机器人在发（永不过期）还是用户身份
    （7 天滚动）—— 这直接决定了「要不要担心它哪天悤声停掉」。
    """
    out = dict(_last)
    out["identity"] = _self.get("identity") or ""
    return out


def _note(ok: bool, *, error: str = "", needs_login: bool = False) -> None:
    _last["at"] = dt.datetime.now().isoformat(timespec="seconds")
    _last["ok"] = ok
    _last["error"] = error
    _last["needs_login"] = needs_login
    if ok:
        _last["sent"] = int(_last["sent"]) + 1


def _target_file() -> Path:
    """open_id 的落盘缓存位置（应用私有数据目录）。

    为什么要落盘：机器人身份**发消息**不需要用户登录，但仍然需要知道「发给谁」，
    而那个 open_id 只能从用户登录态里读出来。不缓存的话会出现一个荒谬的结果 ——
    用户授权过期后，即便用的是永不过期的应用级令牌，也因为「不知道发给谁」而废掉。

    存的是 open_id 和显示名，都不是凭据（open_id 本来就出现在诊断和日志里）。
    """
    # 用 DATA_ROOT 而不是 settings.log_path.parent：后者在开发态被 .env 里的
    # LOG_DIR=./data 带偏，.parent 会变成 backend/ 而不是数据目录。
    # DATA_ROOT 在开发态是 backend/data，打包后是 %LOCALAPPDATA%\Feishu Agent Hub。
    return DATA_ROOT / "selfpush_target.json"


def _load_target() -> dict:
    try:
        d = json.loads(_target_file().read_text(encoding="utf-8"))
        if str(d.get("open_id") or "").startswith("ou_"):
            return {"open_id": d["open_id"], "name": str(d.get("name") or "")}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_target(oid: str, name: str) -> None:
    try:
        _target_file().write_text(
            json.dumps({"open_id": oid, "name": name}, ensure_ascii=False),
            encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("selfpush target cache write failed: %s", e)


def _want_identity(bot_ready: bool) -> str:
    """这次用哪个身份。auto = 能用机器人就用机器人（永不过期），否则退回用户身份。"""
    want = str(getattr(settings, "selfpush_identity", "auto") or "auto").strip().lower()
    if want in ("bot", "user"):
        return want
    return "bot" if bot_ready else "user"


async def target(*, refresh: bool = False) -> dict:
    """解析「我自己」→ ``{open_id, name, identity}``。

    ## 用户授权失效**不一定**是致命的

    只有 ``identity == "user"`` 时才需要用户登录有效。机器人身份用应用级令牌，
    授权过期照样能发 —— 所以这里先定身份，再决定要不要因为 needs_login 而放弃。
    这是把「7 天滚动过期」从硬故障降级成「只影响用户身份」的关键一步。

    ``needs_refresh`` 不算失效，但**它不等于 ready** —— 这行注释以前写的是
    「auth_status 内部已经把它归到 ready」，那是错的，而正因为写着这句，谁都没去
    核实，于是 needs_refresh 一路被当成登录失效误报。真实语义见 cli.auth_status
    的阶段 2.5：refresh token 还有效，发一次调用就自动续期。判断要用
    ``user_usable``，不能用 ``authenticated``。
    """
    lark = await get_lark()
    try:
        st = await lark.auth_status()
    except LarkCLIError as e:
        raise PushError("读取飞书登录状态失败：%s" % e) from e

    identity = _want_identity(bool(st.get("bot_ready")))

    # open_id：内存 → auth status → 落盘缓存。三者任一命中即可。
    oid = _self.get("open_id") or ""
    name = _self.get("name") or ""
    if not oid or refresh:
        oid = str(st.get("user_id") or "")
        name = str(st.get("user_name") or "")
        if oid.startswith("ou_"):
            _save_target(oid, name)
        else:
            cached = _load_target()
            oid, name = cached.get("open_id", ""), cached.get("name", "")

    # user_usable 而非 authenticated：needs_refresh 时后者为 False，但推送照样能发
    # （发出去还会顺带把 7 天窗口续上），拿 authenticated 拦就是误报。
    usable = bool(st.get("user_usable", st.get("authenticated")))
    if identity == "user" and not usable:
        raise PushError(
            "飞书登录已失效（%s）。用户身份的授权 7 天滚动过期，重新登录即可恢复推送。"
            "（应用若开通 im:message:send_as_bot，可改用机器人身份，就不受此限。）"
            % (st.get("stage") or "unknown"),
            needs_login=True,
        )
    if not oid.startswith("ou_"):
        # 拿不到 open_id 就不要瞎发：没有兜底目标可选，发错人比不发严重得多。
        raise PushError(
            "没能拿到你的 open_id（auth status 返回 %r，本地缓存也没有），不推送。"
            "先在工作台完成一次飞书登录即可。" % (st.get("user_id") or ""),
            needs_login=not usable,
        )

    _self.update({"open_id": oid, "name": name, "identity": identity})
    return dict(_self)


def _clip(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[: MAX_CHARS - 40].rstrip() + "\n\n…（内容过长已截断，完整记录在应用里）"


async def send(markdown: str, *, key: str = "") -> dict:
    """把一段 Markdown 推给自己。返回 ``{ok, message_id, ...}``。

    ``key`` 是幂等键：同一个 key 重复发不会产生第二条消息。定时任务必须传 —— 重试
    或进程重启导致的重复触发，代价是你的飞书被同一条总结轰炸。
    """
    text = (markdown or "").strip()
    if not text:
        raise PushError("推送内容为空，不发。")
    try:
        me = await target()
    except PushError as e:
        # **认证类失败也必须记进 last()。** 这条路径最容易被漏掉：它在真正调用
        # im_send **之前**就抛了，而原先只有 im_send 失败那一支会调 _note()。
        # 漏掉的后果是页面上那条「授权已过期，去重新登录」的横幅永远不出现
        # （它的条件是 push.ok === false），用户只看到一句干巴巴的「推送失败」——
        # 而这恰恰是最常见的失败原因（user 身份授权 7 天滚动过期）。
        _note(False, error=str(e), needs_login=e.needs_login)
        raise
    lark = await get_lark()
    try:
        res = await lark.im_send(
            user_id=me["open_id"], text=_clip(text),
            markdown=True, idempotency_key=key,
            identity=me.get("identity") or "user",
        )
    except LarkCLIError as e:
        msg = str(e)
        # missing_scope / 登录失效都会走到这里。needs_login 要单独认出来，因为它
        # 重试无用 —— 页面上得让用户看到「去重新登录」而不是「稍后自动重试」。
        needs_login = any(m in msg for m in ("needs_login", "invalid_token", "token expired", "20005"))
        _note(False, error=msg, needs_login=needs_login)
        raise PushError(msg, needs_login=needs_login) from e
    _note(True)
    log.info("selfpush ok: %s", (res or {}).get("data", {}).get("message_id", ""))
    return res


async def try_send(markdown: str, *, key: str = "") -> dict:
    """``send`` 的不抛版本，给后台循环用 —— 推送失败绝不能把提炼循环带崩。

    失败已经记进 ``last()`` 并会显示在页面上，所以这里只吞异常、不吞信息。
    """
    try:
        await send(markdown, key=key)
        return {"ok": True}
    except PushError as e:
        log.warning("selfpush failed: %s", e)
        return {"ok": False, "error": str(e), "needs_login": e.needs_login}
    except Exception as e:  # noqa: BLE001
        _note(False, error=f"{type(e).__name__}: {e}")
        log.warning("selfpush crashed: %s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "needs_login": False}
