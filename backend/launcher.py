"""Tray-mode entry point for the Local Agent Hub backend.

Boot sequence:
  1. Single-instance check (named mutex on Windows). If a second copy
     starts, it just opens the browser and exits — avoiding duplicate
     tray icons and uvicorn port-bind failures.
  2. Inject bundled Node + lark-cli into PATH and LARK_CLI_BIN.
  3. Set up file logging (no console attached in frozen build).
  4. Start uvicorn in a background daemon thread.
  5. Open the user's browser once the server is responsive.
  6. Show a tray icon in the main thread; menu offers open/quit.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path


# ── Path resolution ─────────────────────────────────────────────────


def _install_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent
    return Path(__file__).resolve().parent.parent


def _data_root() -> Path:
    if getattr(sys, "frozen", False):
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "Feishu Agent Hub"
    return _install_root() / "backend" / "data"


def _resource(rel: str) -> Path:
    """Locate a bundled data file (PyInstaller --add-data target)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / rel
    return _install_root() / "build" / rel


# ── PATH setup for bundled lark-cli ─────────────────────────────────


def _prepare_runtime_path() -> None:
    root = _install_root()
    node_dir = root / "runtime" / "node"
    lark_bin = root / "runtime" / "lark-cli" / "node_modules" / ".bin"
    extras = [str(p) for p in (node_dir, lark_bin) if p.exists()]
    if extras:
        os.environ["PATH"] = os.pathsep.join(extras) + os.pathsep + os.environ.get("PATH", "")
    bundled = lark_bin / "lark-cli.cmd"
    if bundled.exists() and "LARK_CLI_BIN" not in os.environ:
        os.environ["LARK_CLI_BIN"] = str(bundled)


# ── File logging ────────────────────────────────────────────────────


def _setup_logging() -> Path:
    log_dir = _data_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "backend.log"
    handler = RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # Also tame uvicorn's own loggers so their lines land in the file.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.propagate = True
    return log_file


# ── Uvicorn supervisor ──────────────────────────────────────────────


class BackendSupervisor:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.server = None  # type: ignore[assignment]
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        import uvicorn
        from app.main import app

        # access_log=True so requests still appear in backend.log
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="info",
            access_log=True,
            use_colors=False,
            workers=1,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="uvicorn")
        self.thread.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True


# ── Browser open after ready ────────────────────────────────────────


def _acquire_single_instance(host: str, port: int) -> bool:
    """Return True if this is the first instance, False if another is already running.

    Uses a named Windows mutex when available (covers the "another instance is
    still starting up but hasn't bound the port yet" race). Falls back to a
    port-busy probe, which catches the common case where the previous instance
    is already serving on (host, port).
    """
    # Probe the port first — cheap and handles 90% of cases.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                # Something is already listening on the target port.
                return False
    except OSError:
        pass

    # Best-effort named mutex on Windows; ignore failure on other platforms.
    if sys.platform == "win32":
        try:
            import ctypes

            ERROR_ALREADY_EXISTS = 183
            kernel32 = ctypes.windll.kernel32
            # Bring the handle into module scope so the OS keeps the mutex alive
            # for the lifetime of this process.
            global _MUTEX_HANDLE  # noqa: PLW0603
            _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, "Global\\LocalAgentHub-SingleInstance")
            if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                return False
        except Exception:  # noqa: BLE001
            pass
    return True


_MUTEX_HANDLE = None  # type: ignore[var-annotated]


def _open_browser_when_ready(url: str, timeout: float = 15.0) -> None:
    import urllib.request

    def _wait() -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(url + "/api/health", timeout=1).read()
                break
            except Exception:
                time.sleep(0.3)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_wait, daemon=True, name="browser-opener").start()


# ── Tray icon ───────────────────────────────────────────────────────


def _tray_image():
    from PIL import Image

    icon_png = _resource("icon.png")
    if icon_png.exists():
        return Image.open(str(icon_png))
    # Fallback: solid brand green square
    return Image.new("RGB", (64, 64), (0, 170, 79))


def _run_tray(url: str, supervisor: BackendSupervisor, log_file: Path) -> None:
    import pystray
    from pystray import MenuItem as Item, Menu

    img = _tray_image()

    def _open_web(icon, item) -> None:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def _open_logs(icon, item) -> None:
        try:
            os.startfile(str(log_file))  # type: ignore[attr-defined]
        except Exception:
            pass

    def _open_data(icon, item) -> None:
        try:
            os.startfile(str(_data_root()))  # type: ignore[attr-defined]
        except Exception:
            pass

    def _quit(icon, item) -> None:
        try:
            supervisor.stop()
        finally:
            icon.stop()
            os._exit(0)

    menu = Menu(
        Item("打开本地 Agent 工作台", _open_web, default=True),
        Menu.SEPARATOR,
        Item("查看日志文件", _open_logs),
        Item("打开数据目录", _open_data),
        Menu.SEPARATOR,
        Item("退出", _quit),
    )
    icon = pystray.Icon(
        name="LocalAgentHub",
        icon=img,
        title="本地 Agent 工作台 · 运行中",
        menu=menu,
    )
    icon.run()


# ── Main ────────────────────────────────────────────────────────────


def main() -> None:
    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8787"))
    url = f"http://{host}:{port}"

    # If another copy is already running, just bring up the browser and exit
    # so we don't end up with duplicate tray icons or port-bind errors.
    if not _acquire_single_instance(host, port):
        try:
            webbrowser.open(url)
        except Exception:
            pass
        sys.exit(0)

    _prepare_runtime_path()
    log_file = _setup_logging()

    logging.info("Local Agent Hub starting on %s", url)
    logging.info("Log file: %s", log_file)
    logging.info("Data root: %s", _data_root())

    supervisor = BackendSupervisor(host, port)
    try:
        supervisor.start()
    except Exception:
        logging.exception("Failed to start backend")
        _show_fatal("后端启动失败，请查看日志文件。")
        sys.exit(1)

    _open_browser_when_ready(url)
    _run_tray(url, supervisor, log_file)


def _show_fatal(message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "本地 Agent 工作台", 0x10)
    except Exception:
        pass


if __name__ == "__main__":
    main()
