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


def _should_skip_window() -> bool:
    """前台窗口是邮件 / 即时通讯窗口 → 跳过截图（本应用自身窗口不跳过）。

    先按进程名精确匹配（桌面客户端可靠），再按窗口标题子串匹配（覆盖网页版）。
    """
    if screenshot.active_window_process_name() in _SKIP_PROCESS_NAMES:
        return True
    title = screenshot.active_window_title().lower()
    return any(m in title for m in _SKIP_TITLE_MARKERS)


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
        return dict(_state)
