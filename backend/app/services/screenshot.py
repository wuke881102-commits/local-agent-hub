"""屏幕截图 —— 抓「当前活动窗口」（Windows）。

实现要点：
- 用 ctypes 拿前台窗口句柄与可见边界（优先 DWM 扩展边界，去掉投影/阴影留白；
  失败再退回 GetWindowRect）。
- 用 Pillow 的 ImageGrab 按边界抓图（``all_screens=True`` 覆盖多显示器/负坐标）。
- 进程设为 DPI 感知，避免高分屏下窗口坐标被系统虚拟化导致裁切错位。

仅 Windows 可用；非 Windows 或抓取异常时抛 RuntimeError，由调用方转成可读报错。
"""
from __future__ import annotations

import ctypes
import datetime as dt
import sys
from ctypes import wintypes
from pathlib import Path

try:
    from PIL import Image, ImageGrab
    _PIL_OK = True
except Exception:  # noqa: BLE001
    _PIL_OK = False


_IS_WINDOWS = sys.platform.startswith("win")
_DWMWA_EXTENDED_FRAME_BOUNDS = 9


def _make_dpi_aware() -> None:
    """让进程按物理像素读取窗口坐标（高分屏裁切才准）。只需成功一次。"""
    if not _IS_WINDOWS:
        return
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


if _IS_WINDOWS:
    _make_dpi_aware()


def _active_window_bbox() -> tuple[int, int, int, int] | None:
    """前台窗口的 (left, top, right, bottom)，拿不到返回 None。"""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    # 先试 DWM 扩展边界（与肉眼看到的窗口边一致）
    try:
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(_DWMWA_EXTENDED_FRAME_BOUNDS),
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
    except Exception:  # noqa: BLE001
        hr = -1
    if hr != 0:
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
    box = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return None
    return box


def active_window_title() -> str:
    """前台窗口标题（拿不到返回空串）。用于判断当前是否是本应用自身窗口。"""
    if not _IS_WINDOWS:
        return ""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = int(user32.GetWindowTextLengthW(hwnd))
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value or ""
    except Exception:  # noqa: BLE001
        return ""


def active_window_process_name() -> str:
    """前台窗口所属进程的可执行文件名（小写，如 'feishu.exe'）；拿不到返回空串。

    比窗口标题可靠：聊天/邮件客户端的标题常为空或显示会话名，但进程名固定。
    用 GetWindowThreadProcessId + QueryFullProcessImageNameW，无需提权。
    """
    if not _IS_WINDOWS:
        return ""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            kernel32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
            ]
            kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            path = buf.value or ""
        finally:
            kernel32.CloseHandle(handle)
        return path.rsplit("\\", 1)[-1].lower()
    except Exception:  # noqa: BLE001
        return ""


def capture_active_window() -> "Image.Image":
    """抓当前活动窗口；拿不到窗口边界时退回整张（虚拟）桌面。"""
    if not _IS_WINDOWS:
        raise RuntimeError("截图当前仅支持 Windows。")
    if not _PIL_OK:
        raise RuntimeError("服务端缺少 Pillow，无法截图。")
    bbox = _active_window_bbox()
    try:
        if bbox:
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
        else:
            img = ImageGrab.grab(all_screens=True)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"截图失败：{type(e).__name__}: {e}")
    if img is None:
        raise RuntimeError("截图失败：未获取到图像。")
    return img


def timestamp_name(now: dt.datetime | None = None) -> str:
    """时间戳文件名：shot-YYYYMMDD-HHMMSS-mmm.png（毫秒避免同秒覆盖）。"""
    now = now or dt.datetime.now()
    return f"shot-{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}.png"


def capture_to_dir(directory: str | Path) -> Path:
    """抓当前活动窗口并按时间戳存到目录下，返回文件路径。"""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    img = capture_active_window()
    path = d / timestamp_name()
    img.save(path, format="PNG")
    return path
