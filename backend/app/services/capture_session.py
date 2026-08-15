"""截图捕获会话 —— 开关式全局 Enter 钩子。

开启后：在「任意窗口」按下 Enter，就把「当前活动窗口」截一张、按时间戳存到所选目录。
关闭即停。不拦截 Enter（``suppress=False``），用户正常使用回车，仅旁路截图。

实现：
- 全局键盘钩子来自 ``keyboard`` 库（Windows 无需管理员）。其回调跑在该库自己的监听
  线程里；这里用锁保护共享状态，截图/存盘在回调线程内同步完成（单张 ~几十毫秒）。
- 去抖：忽略与上次成功截图间隔 < ``_DEBOUNCE_S`` 的连按（含长按 Enter 的自动重复）。
- ``keyboard`` 缺失或注册失败时记录到 ``error``，不影响后端其它功能。
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from pathlib import Path

from . import screenshot

log = logging.getLogger("capture")

_DEBOUNCE_S = 0.4  # 两次截图最小间隔，挡住 Enter 长按/连击

# 邮件 / 即时通讯窗口：写邮件、聊天时频繁按 Enter 发送 / 换行，不应留痕（也涉隐私）。
# 用两条规则判断，命中任一即跳过：
#
# A) 进程可执行名（最可靠）：桌面客户端窗口标题常为空 / 显示会话名，按标题抓不到，
#    但进程名固定。精确匹配 basename（小写），无误伤风险。
_SKIP_PROCESS_NAMES = frozenset((
    "outlook.exe", "olk.exe", "hxoutlook.exe", # Outlook 经典 / 新版 / 旧 UWP 邮件
    "feishu.exe", "lark.exe", "g-space.exe",   # 飞书 / Lark / G-Space（公司白标版飞书）
    "ms-teams.exe", "msteams.exe", "teams.exe", # Microsoft Teams（新/旧）
    "wechat.exe", "weixin.exe", "wxwork.exe",  # 微信 / 新版微信 / 企业微信
    "dingtalk.exe",                            # 钉钉
    "slack.exe",                               # Slack
    "qq.exe", "tim.exe",                       # QQ / TIM（精确名，安全）
))
# B) 窗口标题子串（小写）：主要覆盖「网页版」（进程是浏览器，靠标题识别）。
#    注：未用裸 "tim" 匹配 QQ-TIM —— 子串会误伤 estimate / time / runtime 等普通标题。
#    注：本应用自身窗口「不」跳过（按需可加回 "本地 agent 工作台" / "local agent hub"）。
_SKIP_TITLE_MARKERS = (
    "message (html)", "message (plain text)", "message (rich text)", "outlook",
    "飞书", "lark", "g-space", "microsoft teams",
    "微信", "wechat", "wecom",
    "钉钉", "dingtalk",
    "slack", "qq",
)
# C) 会议白名单（优先级最高，先于 A/B 判断）：飞书 / Teams / 微信等既是聊天工具
#    也是会议工具，只按进程名跳过会把「会议画面」一并跳掉。这里先放行会议窗口，
#    聊天窗口仍按 A/B 跳过——即「聊天不截、会议照截」。
#
#    误伤风险低：聊天客户端的主窗口标题通常只有应用名（"飞书" / "微信"），会话名在
#    界面里而不在标题栏；会议窗口则带明确的会议 / 通话字样。若你的某个会话正好被命名
#    成含下列字样，那次会被截到——可用 CAPTURE_MEETING_MARKERS 反向调整（见下）。
_MEETING_TITLE_MARKERS = (
    # 飞书 / Lark / G-Space（白标飞书）：实测白标版会议窗标题为「普笺G-Space会议」，
    # 聊天主窗口则是「普笺G-Space」——差别就在结尾的「会议」二字。
    "视频会议", "飞书会议", "g-space会议", "g-space 会议",
    "lark meetings", "video meeting", "feishu video",
    # Microsoft Teams：实测会议窗标题为「Microsoft Teams 会议」（中文界面）；
    # 英文界面为「Meeting | Microsoft Teams」。聊天窗是「<会话名> | Microsoft Teams」，不会命中。
    "teams 会议", "teams meeting", "meeting | microsoft",
    # 通用会议中 / 通话中
    "会议中", "meeting in progress", "meeting with", "正在开会",
    # 微信 / 企业微信 / QQ 通话
    "视频通话", "语音通话", "video call", "voice call", "企业微信会议",
    # 钉钉 / 腾讯会议（腾讯会议 wemeet.exe 本就不在跳过名单，这里兜底其网页版/嵌入窗口）
    "钉钉会议", "腾讯会议", "tencent meeting",
    # Slack
    "huddle",
    # 屏幕共享（共享中一定不是在打字聊天）
    "正在共享", "屏幕共享", "sharing your screen", "is sharing",
)
# 通用规律（实测）：各家「会议窗口」的标题都以「会议」结尾，而聊天主窗口只有应用名——
#   普笺G-Space会议 / Microsoft Teams 会议 / 腾讯会议 / 钉钉会议   ← 会议窗
#   普笺G-Space     / Microsoft Teams      / 微信     / 飞书      ← 聊天窗
# 故单列一条后缀规则，新会议软件无需再逐个加白名单。
# 代价：若把某个会话/群命名成以「会议」结尾（如「项目周会议」）并弹成独立窗口，那个窗口
# 会被截到。不接受这个代价就把本元组置空，只保留上面的精确关键词。
_MEETING_TITLE_SUFFIXES = ("会议", "meeting")

# 窗口类名白名单（不受界面语言影响，比标题更准）。留空即不启用；
# 用「最近跳过的窗口」诊断看到会议窗口的真实类名后，可加进来做精确放行。
_MEETING_CLASS_MARKERS: tuple[str, ...] = ()


def _extra_meeting_markers() -> tuple[str, ...]:
    """用户自定义的会议标题关键词（.env 里 CAPTURE_MEETING_MARKERS=逗号分隔）。

    没验证到你实际的会议窗口标题时的兜底口子：加一条就放行，改完重启后端生效。
    """
    try:
        from ..config import settings  # noqa: PLC0415 —— 延迟导入，避免循环依赖
        raw = getattr(settings, "capture_meeting_markers", "") or ""
    except Exception:  # noqa: BLE001
        return ()
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())

_lock = threading.Lock()
_state = {
    "active": False,
    "directory": "",
    "count": 0,
    "last_path": "",
    "last_at": "",
    "error": "",
    "started_at": "",
}
_hook = None              # keyboard 返回的 hook 句柄
_last_shot_mono = 0.0     # 去抖用的单调时钟
_SKIP_LOG_MAX = 8         # 「最近跳过的窗口」保留条数
_skip_log: list[dict] = []  # 仅内存，不落盘（标题可能含会话名）


def _is_meeting_window(title: str, cls: str) -> bool:
    """前台窗口看起来是「会议 / 通话 / 共享」窗口 → 即使属于聊天应用也要截图。"""
    if cls and any(m in cls.lower() for m in _MEETING_CLASS_MARKERS):
        return True
    t = (title or "").strip()
    if t and t.endswith(_MEETING_TITLE_SUFFIXES):
        return True
    return any(m in t for m in _MEETING_TITLE_MARKERS + _extra_meeting_markers())


def _should_skip_window() -> bool:
    """前台窗口是邮件 / 即时通讯窗口 → 跳过截图（本应用自身窗口不跳过）。

    判断顺序：
      C) 先看是不是会议 / 通话窗口 —— 是就放行（聊天应用的会议也要留痕）。
      A) 再按进程名精确匹配（桌面客户端可靠）。
      B) 最后按窗口标题子串匹配（覆盖网页版）。
    """
    title = screenshot.active_window_title().lower()
    cls = screenshot.active_window_class_name()
    if _is_meeting_window(title, cls):
        return False
    if screenshot.active_window_process_name() in _SKIP_PROCESS_NAMES:
        _record_skip(title, cls)
        return True
    if any(m in title for m in _SKIP_TITLE_MARKERS):
        _record_skip(title, cls)
        return True
    return False


def _record_skip(title: str, cls: str) -> None:
    """记下最近被跳过的窗口，供「最近跳过」诊断展示。

    只留内存里最近 _SKIP_LOG_MAX 条、不落盘、不进日志文件——标题可能含会话名。
    用途：会议窗口若被误跳，你能在这里看到它的真实标题 / 类名，据此加白名单。
    """
    if not title and not cls:
        return
    entry = {"title": title[:80], "cls": cls[:60], "at": dt.datetime.now().isoformat(timespec="seconds")}
    with _lock:
        # 同一个窗口连按 Enter 只记一条（更新时间），避免刷屏
        for it in _skip_log:
            if it["title"] == entry["title"] and it["cls"] == entry["cls"]:
                it["at"] = entry["at"]
                return
        _skip_log.insert(0, entry)
        del _skip_log[_SKIP_LOG_MAX:]


def _on_enter(_event=None) -> None:
    global _last_shot_mono
    now = time.monotonic()
    with _lock:
        if not _state["active"]:
            return
        if now - _last_shot_mono < _DEBOUNCE_S:
            return
        directory = _state["directory"]
    # 邮件 / 聊天窗口：跳过（不截、不计数、不占去抖窗口）
    if _should_skip_window():
        return
    with _lock:
        _last_shot_mono = now
    try:
        path = screenshot.capture_to_dir(directory)
        with _lock:
            _state["count"] += 1
            _state["last_path"] = str(path)
            _state["last_at"] = dt.datetime.now().isoformat(timespec="seconds")
            _state["error"] = ""
    except Exception as e:  # noqa: BLE001
        log.warning("capture on Enter failed: %s", e)
        with _lock:
            _state["error"] = f"{type(e).__name__}: {e}"


def start(directory: str) -> dict:
    """开启捕获会话。目录无效或钩子注册失败 → 抛 ValueError / RuntimeError。"""
    global _hook
    d = Path(directory).expanduser()
    if not directory or not d.is_dir():
        raise ValueError("请先选择一个有效的本地目录。")

    try:
        import keyboard  # noqa: PLC0415 —— 延迟导入，缺失不拖垮后端
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"服务端缺少 keyboard 库，无法监听全局快捷键：{e}")

    with _lock:
        already = _state["active"]
    if already:
        stop()  # 切目录/重复开启时先清掉旧钩子

    try:
        # on_press_key：仅在 Enter 按下时触发；suppress=False 不拦截正常回车。
        hook = keyboard.on_press_key("enter", _on_enter, suppress=False)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"注册全局快捷键失败：{e}")

    with _lock:
        _hook = hook
        _state.update({
            "active": True,
            "directory": str(d),
            "count": 0,
            "last_path": "",
            "last_at": "",
            "error": "",
            "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        })
    log.info("capture session started: %s", d)
    return status()


def stop() -> dict:
    """停止捕获会话并卸载钩子。"""
    global _hook
    try:
        import keyboard  # noqa: PLC0415
        if _hook is not None:
            try:
                keyboard.unhook(_hook)
            except Exception:  # noqa: BLE001
                keyboard.unhook_all()
    except Exception:  # noqa: BLE001
        pass
    with _lock:
        _hook = None
        _state["active"] = False
    log.info("capture session stopped")
    return status()


def status() -> dict:
    with _lock:
        out = dict(_state)
        out["recent_skips"] = [dict(it) for it in _skip_log]
    return out
