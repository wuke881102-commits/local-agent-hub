"""Feishu CLI (lark-cli) 子进程适配层。

策略：
- 优先用 `+shortcut` 层（agent-optimized、stable）。
- 默认 `--format json`，解析 stdout；失败抛 LarkCLIError(stderr)。
- 启动时 `lark-cli --version` ping，失败则标记 cli_available=False。
- 上层服务在 cli_available=False 时回退到 MockLarkCLI（如果 settings.enable_mock_fallback）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from ..config import settings


_URL_RE = re.compile(r"https?://(?:open|accounts)\.(?:feishu\.cn|larksuite\.com)/[^\s\"'<>]+")
_CODE_RE = re.compile(r"user_code=([A-Za-z0-9_-]+)")

log = logging.getLogger("feishu.cli")

# ── 飞书最小权限集（最小权限原则）──────────────────────────────────────
# 由本文件实际调用面审计得出，并用 `lark-cli auth check --scope "..."` 逐个验证为
# 合法且够用（ok:true）。登录默认只请求这些，用户在飞书同意页只会看到这一最小集，
# 而不是 --recommend 拉来的一大堆写/删/分享/邮箱权限。
# 维护：新增飞书调用时同步更新此处，并用 `auth check` 复核确切 scope ID。
_MIN_LOGIN_SCOPES_LIST = [
    # 只读 · 文档 / 云盘
    "docx:document:readonly",          # 读文档正文（docx）
    "docs:document.content:read",      # 读文档正文
    "docs:document.media:download",    # 文档内图片/媒体
    "drive:drive.metadata:readonly",   # 云盘文件元信息
    "space:document:retrieve",         # 云盘文件列表（drive files list / 多维表格枚举）← 索引必需
    "search:docs:read",                # 云盘搜索（drive +search，含共享给我的）← 索引必需
    "drive:file:download",             # 下载云盘文件（PDF 等）
    # 只读 · 知识库 / 表格 / 多维表格 / 幻灯片
    "wiki:space:read", "wiki:node:read",
    "wiki:space:retrieve",             # 知识库空间列表（wiki spaces list）← 索引必需
    "wiki:node:retrieve",              # 知识库节点列表（wiki nodes list）← 索引必需
    "sheets:spreadsheet:read", "sheets:spreadsheet.meta:read",
    "base:app:read", "base:table:read", "base:record:read",
    "slides:presentation:read",
    # 只读 · 日历 / 会议纪要 / 通讯录 / 群
    "calendar:calendar:read", "calendar:calendar.event:read",
    "vc:record:readonly", "minutes:minutes.search:read",
    "contact:user.base:readonly", "contact:user:search",
    "im:chat:read",
    # 写 · 均需用户在产品内确认后才触发
    "docx:document:create",            # 写回：创建文档
    "im:message",                      # 协作分发：发群消息（基础 scope）
    "im:message.send_as_user",         # 协作分发：以用户身份发消息。lark-cli `auth check --dry-run` 只校验
                                       # im:message 即放行，但真正发送时飞书强制要求此更细 scope；缺它的用户
                                       # 会在确认分发时报 missing_scope（500）。故二者都请求，确保新授权用户可用。
    "task:task:write",                 # 创建任务
    # 会话
    "offline_access",                  # 刷新令牌，持久登录
]
MIN_LOGIN_SCOPES = " ".join(_MIN_LOGIN_SCOPES_LIST)

# On Windows lark-cli ships as a .cmd batch file; subprocess.Popen would briefly
# flash a console window every call. CREATE_NO_WINDOW (0x08000000) suppresses it.
if sys.platform == "win32":
    _NO_WINDOW_FLAGS = 0x08000000  # subprocess.CREATE_NO_WINDOW
else:
    _NO_WINDOW_FLAGS = 0


class LarkCLIError(RuntimeError):
    def __init__(self, message: str, exit_code: int = -1, stderr: str = "", cmd: list[str] | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr
        self.cmd = cmd or []


def _resolve_bin(bin_path: str) -> str:
    """解析 lark-cli 二进制路径。

    搜索顺序：
      1. 若用户指定了路径（含 / 或 \\），直接使用
      2. shutil.which 在 PATH 中查找（含各扩展名）
      3. 兜底：%APPDATA%\\npm（npm 全局安装默认位置，可能不在当前进程 PATH）
         以及 ~/.npm-global、/usr/local/bin 等常见位置
    """
    import os
    if any(sep in bin_path for sep in ("/", "\\")):
        return bin_path
    found = shutil.which(bin_path)
    if found:
        return found
    if sys.platform == "win32":
        for ext in (".cmd", ".exe", ".bat"):
            f = shutil.which(bin_path + ext)
            if f:
                return f
    # 兜底常见 npm global bin 位置
    candidates: list[Path] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            for ext in (".cmd", ".exe", ".bat", ""):
                candidates.append(Path(appdata) / "npm" / f"{bin_path}{ext}")
    else:
        home = Path.home()
        for base in (home / ".npm-global" / "bin",
                     Path("/usr/local/bin"),
                     Path("/opt/homebrew/bin")):
            candidates.append(base / bin_path)
    for c in candidates:
        if c.exists():
            return str(c)
    return bin_path  # let subprocess fail with a clear error


class LarkCLI:
    def __init__(self, bin_path: str | None = None):
        self.bin = _resolve_bin(bin_path or settings.lark_cli_bin)
        self._version: str | None = None
        self._available: bool | None = None
        self._stderr_log = settings.log_path / "lark-cli.log"

    # ── Low-level ────────────────────────────────────────────────────

    async def ping(self) -> bool:
        """Probe CLI. Cache *positive* results forever; re-probe on negatives so a
        slow first-call timeout doesn't permanently flip the app into mock mode.
        """
        if self._available is True:
            return True
        try:
            proc = await asyncio.create_subprocess_exec(
                self.bin, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_NO_WINDOW_FLAGS,
            )
            # 12s — lark-cli cold start on first call can hit ~2.5–8s depending
            # on disk + AV scanner; 5s used to flake during PyInstaller boot.
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=12)
            if proc.returncode == 0:
                self._version = stdout.decode("utf-8", errors="replace").strip()
                self._available = True
                return True
            self._available = False
            log.warning("lark-cli --version returned %s: %s", proc.returncode, stderr.decode(errors="replace"))
        except (FileNotFoundError, asyncio.TimeoutError) as e:
            self._available = False
            log.warning("lark-cli not available: %s", e)
        return False

    @property
    def version(self) -> str | None:
        return self._version

    async def _exec(self, *args: str, timeout: float = 60, cwd: str | None = None) -> tuple[int, str, str]:
        """底层执行，返回 (returncode, stdout, stderr)。

        ``cwd`` 用于 ``docs +media-download`` 这类要求 ``--output`` 为相对路径
        （沙箱限制：只能写当前目录）的命令。
        """
        cmd = [self.bin, *args]
        log.debug("lark-cli exec: %s", " ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_NO_WINDOW_FLAGS,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except FileNotFoundError as e:
            raise LarkCLIError(f"lark-cli not found: {self.bin}", cmd=cmd) from e
        except asyncio.TimeoutError as e:
            raise LarkCLIError(f"lark-cli timed out after {timeout}s", cmd=cmd) from e

        stderr_text = stderr.decode("utf-8", errors="replace")
        if stderr_text:
            try:
                with open(self._stderr_log, "a", encoding="utf-8") as f:
                    f.write(f"$ {' '.join(cmd)}\n")
                    f.write(stderr_text)
                    f.write("\n")
            except OSError:
                pass

        return proc.returncode or 0, stdout.decode("utf-8", errors="replace"), stderr_text

    async def run(self, *args: str, timeout: float = 60, parse_json: bool = True) -> Any:
        """运行 lark-cli 子进程。

        策略：
        - parse_json=True 时若调用方未显式指定 ``--json``，自动追加。
        - 若 CLI 报 "unknown flag: --json"（该子命令默认就输出 JSON），自动剥掉 ``--json`` 重试一次。
        - non-zero 退出码 → 抛 LarkCLIError。
        """
        cmd_args = list(args)
        added_json = False
        if parse_json and not any(a == "--json" or a == "--format" or a.startswith("--format=") for a in args):
            cmd_args.append("--json")
            added_json = True

        code, stdout, stderr = await self._exec(*cmd_args, timeout=timeout)

        # 兼容：若该子命令不识别 --json，剥掉重试
        if code != 0 and added_json and "unknown flag" in stderr and "--json" in stderr:
            log.debug("retry without --json: %s", args)
            code, stdout, stderr = await self._exec(*args, timeout=timeout)

        if code != 0:
            raise LarkCLIError(
                f"lark-cli exited {code}",
                exit_code=code,
                stderr=stderr,
                cmd=[self.bin, *args],
            )

        text = stdout.strip()
        if not parse_json:
            return text
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            log.debug("lark-cli output is not JSON. args=%s", args)
            return {"_raw": text, "_json_error": str(e)}

    # ── Auth ─────────────────────────────────────────────────────────

    async def auth_status(self) -> dict:
        """查询授权状态，区分 3 个阶段：

        ── 阶段 1：未配置（needs_init=True）
            lark-cli 还没装好 app 凭据。``auth status`` exit 3，stderr 含
            ``{ok:false, error:{type:'config', ...}}``。要跑 ``config init --new``。

        ── 阶段 2：app 已配置，但用户未登录（needs_login=True）
            ``auth status`` stdout 含
            ``{appId, identities:{bot:{status:'ready'}, user:{status:'missing'}}}``。
            需要跑 ``auth login --no-wait --recommend``。

        ── 阶段 3：完全已授权（authenticated=True）
            ``identities.user.status == 'ready'``。
        """
        code, stdout, stderr = await self._exec("auth", "status", timeout=15)
        raw: Any = None
        for stream in (stdout, stderr):
            text = (stream or "").strip()
            if not text:
                continue
            try:
                raw = json.loads(text)
                break
            except json.JSONDecodeError:
                continue

        if not isinstance(raw, dict):
            return {"authenticated": False, "error": stderr.strip() or stdout.strip() or f"exit {code}"}

        # 阶段 1：not configured
        if raw.get("ok") is False:
            err = raw.get("error") or {}
            return {
                "authenticated": False,
                "needs_init": err.get("type") == "config",
                "stage": "needs_init",
                "error": err.get("message"),
                "hint": err.get("hint"),
            }

        identities = raw.get("identities") or {}
        user_iden = identities.get("user") or {}
        bot_iden = identities.get("bot") or {}
        user_ready = user_iden.get("status") == "ready"
        bot_ready = bot_iden.get("status") == "ready"

        # lark-cli 1.0.43 uses camelCase keys (openId / userName); older versions
        # used snake_case. Check both.
        info: dict[str, Any] = {
            "authenticated": user_ready,
            "user_id":   (user_iden.get("openId") or user_iden.get("user_id")
                          or user_iden.get("open_id") or raw.get("user_id")),
            "user_name": (user_iden.get("userName") or user_iden.get("user_name")
                          or user_iden.get("name") or raw.get("user_name")),
            "scopes":    (user_iden.get("scope") or user_iden.get("scopes")
                          or raw.get("scopes") or []),
            "app_id":    raw.get("appId") or raw.get("app_id"),
            "brand":     raw.get("brand"),
            "bot_ready": bot_ready,
            "user_status_message": user_iden.get("message"),
        }
        if user_ready:
            info["stage"] = "authenticated"
        elif bot_ready:
            info["stage"] = "needs_login"
            info["needs_login"] = True
            info["hint"] = user_iden.get("hint") or raw.get("note") or "run `lark-cli auth login`"
        else:
            info["stage"] = "needs_init"
            info["needs_init"] = True
        return info

    async def _stream_verification_url(self, *cmd_args: str, deadline: float = 20.0) -> dict:
        """启动一个会输出 verification URL 的长驻 lark-cli 子进程，
        在 deadline 内从 stdout 流式提取 URL + user_code 后立刻返回。

        子进程不被等待——它会持续运行直到用户在浏览器完成授权后自动退出。
        进程句柄保存在 self._pending_init / self._pending_login。
        """
        proc = await asyncio.create_subprocess_exec(
            self.bin, *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_NO_WINDOW_FLAGS,
        )

        found_url: str | None = None
        found_code: str | None = None
        accumulated: list[str] = []
        done = asyncio.Event()

        async def reader(stream: asyncio.StreamReader | None):
            nonlocal found_url, found_code
            if stream is None:
                return
            while True:
                chunk = await stream.readline()
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                accumulated.append(text)
                if not found_url:
                    m = _URL_RE.search(text)
                    if m:
                        found_url = m.group(0)
                if not found_code:
                    m = _CODE_RE.search(text)
                    if m:
                        found_code = m.group(1)
                if found_url and found_code:
                    done.set()
                    return

        tasks = [
            asyncio.create_task(reader(proc.stdout)),
            asyncio.create_task(reader(proc.stderr)),
        ]
        try:
            await asyncio.wait_for(done.wait(), timeout=deadline)
        except asyncio.TimeoutError:
            pass
        for t in tasks:
            t.cancel()

        # 子进程继续后台运行；记录句柄以便后续清理
        slot = "_pending_init" if cmd_args[:2] == ("config", "init") else "_pending_login"
        prev = getattr(self, slot, None)
        if prev is not None and prev.returncode is None:
            try:
                prev.kill()
            except ProcessLookupError:
                pass
        setattr(self, slot, proc)

        out = "".join(accumulated)
        # 把 QR 码 ASCII 部分剥掉以减少响应体积
        out_clean = "\n".join(line for line in out.splitlines() if "█" not in line and "▀" not in line and "▄" not in line)
        return {
            "verification_uri": found_url,
            "user_code": found_code,
            "raw_excerpt": out_clean[-800:] if out_clean else "",
            "command": " ".join(cmd_args),
        }

    async def config_init_with_secret(self, app_id: str, secret: str, *, brand: str = "feishu", timeout: float = 30) -> bool:
        """非交互写入应用凭据：``config init --app-id <id> --app-secret-stdin``。

        用于打包分发：把内置专用应用的 app_id/secret 配到本机 lark-cli（secret 会进系统
        keychain），从而跳过 ``config init --new``（创建新应用）。secret 经 **stdin** 传入，
        不出现在命令行/进程列表里。返回 True 表示配置成功。

        ``--force-init``：lark-cli 的全局配置在 ``~/.lark-cli/config.json``（与该用户账户下
        所有 lark-cli 共享）。当机器处于 "Agent 工作区"（环境里有 OPENCLAW_HOME / HERMES_HOME）
        时，``config init`` 默认会 **拒绝** 并建议改用 ``config bind``。我们就是要把它切到自己的
        专用应用，因此必须带 --force-init，否则切换会静默失败、继续沿用机器上原有的应用
        （在授权页显示成别的 bot）。非 Agent 工作区时该 flag 是无操作。
        """
        cmd = [self.bin, "config", "init", "--app-id", app_id, "--brand", brand,
               "--app-secret-stdin", "--force-init"]
        log.info("lark-cli config init (non-interactive) for app %s", app_id)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_NO_WINDOW_FLAGS,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(input=(secret + "\n").encode("utf-8")), timeout=timeout
            )
        except FileNotFoundError as e:
            raise LarkCLIError(f"lark-cli not found: {self.bin}", cmd=cmd) from e
        except asyncio.TimeoutError as e:
            raise LarkCLIError(f"config init timed out after {timeout}s", cmd=cmd) from e
        if (proc.returncode or 0) != 0:
            log.warning("config init (non-interactive) failed: %s", stderr.decode("utf-8", errors="replace"))
            return False
        return True

    async def ensure_app_configured(self) -> bool:
        """打包内置专用应用时，确保 lark-cli 当前用的就是这个应用。

        - 全新机器（未配置）→ 写入内置应用凭据。
        - 机器上配的是别的应用（旧机器 / 装过早期版本 / lark-cli 自带的 Feishubot）
          → 切换到内置应用。切换后该用户对新应用是"未登录"，前端据此引导其对
          **专用应用**做最小权限授权（否则会一直沿用旧 app，在授权页显示成别的 bot）。
        - 已经是内置应用且 bot 身份就绪 → 不动（避免清掉已有登录）。
        - 已经是内置应用、但 bot 身份未就绪（secret 从系统凭据库丢了：被清理 / 迁移 /
          换机器）→ **照样重注 secret**。否则 auth_status 会报 needs_init，登录流程会把
          用户错误地带到 lark-cli 的"创建新应用"页，用户自己很难恢复。

        无内置凭据（开发态）→ 直接跳过。返回是否执行了配置写入。
        """
        if not (settings.feishu_app_id and settings.feishu_app_secret):
            return False
        try:
            status = await self.auth_status()
        except Exception as e:  # noqa: BLE001
            log.warning("ensure_app_configured: auth_status failed: %s", e)
            status = {}
        current = status.get("app_id")
        bot_ready = bool(status.get("bot_ready"))
        # 已是内置应用且 bot 就绪：真正的 no-op，保留现有登录。
        if current == settings.feishu_app_id and bot_ready:
            return False
        if current == settings.feishu_app_id:
            # app_id 对得上、但 bot 没就绪：secret 丢了，补回去（不换 app，不动用户登录）。
            log.warning("ensure_app_configured: app %s present but bot not ready "
                        "(secret missing from keychain) — re-injecting secret",
                        settings.feishu_app_id)
        else:
            log.info("ensure_app_configured: switching lark-cli app %s -> %s",
                     current, settings.feishu_app_id)
        ok = await self.config_init_with_secret(settings.feishu_app_id, settings.feishu_app_secret)
        # Verify the switch actually took — the global ~/.lark-cli/config.json is shared,
        # so a failed/ignored switch would silently leave the machine on its old app and
        # show the wrong bot on the consent page. Surface that loudly instead.
        try:
            after = (await self.auth_status()).get("app_id")
        except Exception:  # noqa: BLE001
            after = None
        if after != settings.feishu_app_id:
            log.error(
                "ensure_app_configured: switch to %s did NOT take (config init ok=%s, "
                "still on %s). Consent page will show the wrong app. Check that lark-cli "
                "config init succeeded (~/.lark-cli/config.json) and that no stale app is pinned.",
                settings.feishu_app_id, ok, after,
            )
        else:
            log.info("ensure_app_configured: now on %s", after)
        return ok

    async def auth_login(self, recommend: bool = False, scope: str | None = None, force: bool = False) -> dict:
        """触发授权流，根据当前状态自动选择正确命令。

        两步都用同一种模式：spawn 长驻子进程，从 stdout 抓 URL 后立刻返回。
        子进程留在后台继续 poll Feishu 端点，用户完成浏览器授权后会写入凭据并退出。
        前端通过 `/api/auth/status` 轮询感知状态翻转。

        - 先确保用的是内置专用应用（ensure_app_configured：新机器→配置，旧机器/默认 bot→切换）
        - 阶段 1 needs_init → ``config init --new``（仅开发态、无内置凭据时）
        - 阶段 2 needs_login → ``auth login --scope <最小权限集>``  ← 不带 --no-wait，子进程必须存活才能 poll
        - 已授权 → 直接返回当前状态；``force=True`` 时仍重新发起登录，用于**补授新增 scope**
          （如「协作分发」发群消息需要的 im:message）。重新授权会以最新最小权限集覆盖旧授权。
        """
        await self.ensure_app_configured()
        status = await self.auth_status()
        stage = status.get("stage")

        if stage == "authenticated" and not force:
            return {
                "verification_uri": None,
                "user_code": None,
                "stage": "authenticated",
                "user_name": status.get("user_name"),
                "user_id": status.get("user_id"),
                "message": "already authenticated as user",
            }

        if stage == "needs_init":
            # 到这一步仍是 needs_init，说明无内置凭据（开发态）→ 交互式创建/配置应用。
            # 打包态由上面的 ensure_app_configured() 已写好凭据，不会落到这里。
            info = await self._stream_verification_url("config", "init", "--new", deadline=25.0)
            info["stage"] = "needs_init"
            return info

        # 阶段 2: needs_login — 必须保留 lark-cli 子进程在后台 poll，否则用户完成
        # 浏览器授权后凭据不会被写入。先前用 --no-wait + run() 的实现是错的。
        args = ["auth", "login"]
        if recommend:
            args.append("--recommend")
        # 默认请求最小权限集；显式传入 scope 可覆盖（仍受应用 scope 目录封顶）。
        effective_scope = scope or MIN_LOGIN_SCOPES
        if effective_scope:
            args.extend(["--scope", effective_scope])
        info = await self._stream_verification_url(*args, deadline=25.0)
        info["stage"] = "reauth" if stage == "authenticated" else "needs_login"
        return info

    async def auth_list(self) -> list[dict]:
        try:
            out = await self.run("auth", "list")
        except LarkCLIError:
            return []
        if isinstance(out, list):
            return out
        if isinstance(out, dict):
            for k in ("items", "data", "results"):
                v = out.get(k)
                if isinstance(v, list):
                    return v
        return []

    # ── Docs / Wiki / Drive ─────────────────────────────────────────

    async def _drive_list_folder(self, folder_token: str = "", page_limit: int = 3) -> list[dict]:
        """List one drive folder. Empty token = root."""
        try:
            out = await self.run(
                "drive", "files", "list",
                "--params", json.dumps({"folder_token": folder_token}),
                "--page-all", "--page-limit", str(page_limit),
                timeout=60,
            )
            return _extract_items(out)
        except LarkCLIError:
            return []

    async def _drive_walk(self, *, max_depth: int = 4, max_folders: int = 200,
                          concurrency: int = 6) -> list[dict]:
        """Drive 根目录起做**有上限的递归遍历**，按层并发列举。结果按 LarkCLI 实例缓存。

        Drive 根通常只是一堆个人 / 团队文件夹，光列根目录拿不到文档；要往下钻才能拿到
        真正的文件。但全量递归会炸掉调用量，所以封顶：
          - ``max_depth``：最多下钻几层（默认 4，覆盖 …/Contract/项目/验收材料/文件 这种嵌套）。
          - ``max_folders``：最多列多少个文件夹（默认 200），到顶即停，避免跑飞。
          - ``concurrency``：同层文件夹并发列举的上限（默认 6）——串行时 70 个文件夹要 ~100s，
            并发后大幅缩短；限并发是为了别一次性 spawn 太多 lark-cli 子进程 / 触发限流。
        每个文件以其所在文件夹名作兜底 ``space``；用 token 去重，避免快捷方式成环。
        """
        if getattr(self, "_drive_cache", None) is not None:
            return self._drive_cache  # type: ignore[return-value]

        all_items: list[dict] = []
        seen_folders: set[str] = set()
        listed = 0
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _list(token: str, parent_name: str) -> tuple[str, list[dict]]:
            async with sem:
                return parent_name, await self._drive_list_folder(token)

        # 逐层 BFS：同一层的所有文件夹并发列举。current = [(token, parent_name)]，根 token=""。
        current: list[tuple[str, str]] = [("", "")]
        depth = 0
        while current and depth < max_depth and listed < max_folders:
            batch = current[: max_folders - listed]   # 限制总列举数不超过 max_folders
            listed += len(batch)
            results = await asyncio.gather(*[_list(tok, pn) for tok, pn in batch])
            next_level: list[tuple[str, str]] = []
            for parent_name, children in results:
                for c in children:
                    if parent_name:
                        c.setdefault("space", parent_name)
                    all_items.append(c)
                    if c.get("type") == "folder" and (depth + 1) < max_depth:
                        ctoken = c.get("token") or c.get("file_token")
                        if ctoken and ctoken not in seen_folders:
                            seen_folders.add(ctoken)
                            next_level.append((ctoken, c.get("name") or parent_name))
            current = next_level
            depth += 1
        self._drive_cache = all_items
        return all_items

    def reset_drive_cache(self) -> None:
        """清掉"单次刷新内共享"的 drive 遍历缓存。

        ``_drive_walk`` 的结果按实例缓存，供同一次刷新里 docs_list 与
        base_list_apps 复用。但实例是进程级单例（get_lark），缓存会跨刷新长存——
        若某次刷新在"尚未授权 / 接口临时失败"时把空结果缓存下来，后续刷新会一直复用
        这个空缓存，导致云盘文档恒为 0。索引刷新开始时调用本方法清缓存，确保每次重新遍历。
        """
        self._drive_cache = None

    async def docs_list(self, page_all: bool = True, page_limit: int = 5) -> list[dict]:
        """All docs/sheets/slides etc. in drive (excluding bitable apps and folders).

        lark-cli 1.0.43 removed `docs +list`; we use `drive files list` and walk
        the folder tree recursively (bounded depth/breadth) since drive root is
        typically only a list of personal / team folders and real files are nested.
        """
        items = await self._drive_walk()
        return [i for i in items if i.get("type") not in ("bitable", "folder")]

    async def docs_get(self, token: str) -> dict:
        """获取文档元数据（标题等）。

        lark-cli 1.0.43 移除了 ``docs +get``，文档信息合并进 ``docs +fetch``
        （同一命令既返正文也返标题）。这里只取元数据；owner/space/updated/url
        等 +fetch 不返回，仍由本地索引提供。旧版本回退到 ``docs +get``。
        """
        try:
            out = await self.run("docs", "+fetch", "--doc", token, "--format", "json", timeout=60)
        except LarkCLIError as e:
            if "unknown subcommand" in (e.stderr or "") or "+fetch" in (e.stderr or ""):
                return await self.run("docs", "+get", token)
            raise
        if isinstance(out, dict):
            data = out.get("data") or {}
            return {
                "title": data.get("title"),
                "doc_token": data.get("doc_id") or token,
                "length": data.get("length"),
            }
        return {}

    async def docs_export_markdown(self, token: str) -> str:
        """导出文档正文为 Markdown。

        lark-cli 1.0.43 起 ``docs +export-markdown`` 被移除，改用
        ``docs +fetch --doc <token>``：返回 JSON，正文在 ``data.markdown``，
        文档内图片以 ``<image token="..." width=.. height=.. align=../>`` 内联。
        旧版本回退到 ``+export-markdown``。
        """
        try:
            out = await self.run("docs", "+fetch", "--doc", token, "--format", "json", timeout=120)
        except LarkCLIError as e:
            # 旧版 lark-cli：回退到 +export-markdown（纯文本输出）
            if "unknown subcommand" in (e.stderr or "") or "+fetch" in (e.stderr or ""):
                return await self.run("docs", "+export-markdown", token, parse_json=False, timeout=120)
            raise
        if isinstance(out, dict):
            data = out.get("data") or {}
            md = data.get("markdown") or data.get("content") or data.get("text")
            if md:
                return md
            if out.get("_raw"):
                return out["_raw"]
        elif isinstance(out, str):
            return out
        return ""

    async def docs_download_media(self, token: str, dest_dir: Path, filename: str) -> dict:
        """按 file_token 下载文档内嵌图片到 ``dest_dir/filename``。

        lark-cli 沙箱要求 ``--output`` 为相对当前目录的路径，因此把子进程
        ``cwd`` 设为 dest_dir，传相对路径 ``./filename``。
        返回 ``{"path": 绝对路径, "content_type": ..., "size": ...}``。
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        code, stdout, stderr = await self._exec(
            "docs", "+media-download", "--token", token,
            "--output", f"./{filename}", "--overwrite",
            timeout=60, cwd=str(dest_dir),
        )
        if code != 0:
            raise LarkCLIError(
                f"media-download exited {code}", exit_code=code, stderr=stderr,
                cmd=[self.bin, "docs", "+media-download", "--token", token],
            )
        info: dict = {}
        m = re.search(r"\{[\s\S]*\}", stdout or "")
        if m:
            try:
                parsed = json.loads(m.group(0))
                info = parsed.get("data") or {}
            except json.JSONDecodeError:
                pass
        saved = info.get("saved_path")
        path = Path(saved) if saved else (dest_dir / filename)
        return {
            "path": str(path),
            "content_type": info.get("content_type", "image/png"),
            "size": info.get("size_bytes", 0),
        }

    async def drive_download_file(self, file_token: str, dest_dir: Path, filename: str) -> dict:
        """按 file_token 下载云盘文件（如 PDF）到 ``dest_dir/filename``。

        用于「PDF 识别」：飞书云盘里的 PDF 不是文档内嵌 media，需走
        ``drive +download``（API：drive/v1/files/{file_token}/download）。
        与 media-download 一样受沙箱限制——``--output`` 须为相对路径，故把子进程
        ``cwd`` 设为 dest_dir，传相对路径 ``./filename``。
        返回 ``{"path": 绝对路径, "size": ...}``。
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        code, stdout, stderr = await self._exec(
            "drive", "+download", "--file-token", file_token,
            "--output", f"./{filename}", "--overwrite",
            timeout=120, cwd=str(dest_dir),
        )
        if code != 0:
            raise LarkCLIError(
                f"drive download exited {code}", exit_code=code, stderr=stderr,
                cmd=[self.bin, "drive", "+download", "--file-token", file_token],
            )
        info: dict = {}
        m = re.search(r"\{[\s\S]*\}", stdout or "")
        if m:
            try:
                parsed = json.loads(m.group(0))
                info = parsed.get("data") or parsed or {}
            except json.JSONDecodeError:
                pass
        saved = info.get("saved_path")
        path = Path(saved) if saved else (dest_dir / filename)
        if not path.is_file() or path.stat().st_size == 0:
            raise LarkCLIError(
                "drive download produced no file", exit_code=code, stderr=stderr,
                cmd=[self.bin, "drive", "+download", "--file-token", file_token],
            )
        return {"path": str(path), "size": path.stat().st_size}

    async def docs_create_markdown(self, title: str, content: str, folder_token: str | None = None) -> dict:
        args = ["docs", "+create", "--doc-format", "markdown", "--title", title, "--content", content]
        if folder_token:
            args.extend(["--folder-token", folder_token])
        return await self.run(*args, timeout=120)

    async def wiki_spaces_list(self) -> list[dict]:
        out = await self.run("wiki", "spaces", "list", "--page-all", "--page-limit", "3", timeout=60)
        return _extract_items(out)

    async def wiki_nodes_list(self, space_id: str) -> list[dict]:
        out = await self.run(
            "wiki", "nodes", "list",
            "--params", json.dumps({"space_id": space_id}),
            "--page-all", "--page-limit", "3",
            timeout=60,
        )
        return _extract_items(out)

    async def wiki_get_node(self, node_or_url: str, *, obj_type: str | None = None) -> dict:
        """解析知识库节点 → 底层真实对象（obj_token / obj_type）。

        背景：知识库（Wiki）里挂的多维表格 / 电子表格 / 文档，其分享链接与索引里存的
        token 是 **wiki 节点 token**（URL 形如 ``.../wiki/<token>``），而 base / sheets
        等 API 需要的是底层 ``obj_token``。直接拿 wiki 节点 token 调 base API 会被拒：
        ``param baseToken is invalid (800004006)``。这里用 ``wiki +node-get`` 解析。

        入参可以是 Lark URL（最稳，类型从 ``/wiki/`` 路径推断）或裸 token。裸 token 不以
        ``wik`` 开头时 CLI 无法判类型，故本方法会把它包成 ``https://feishu.cn/wiki/<token>``
        再解析（域名不影响 API，调用走当前租户）。

        返回 ``{"obj_token","obj_type","title","space_id"}``；解析不出来返回 ``{}``。
        """
        token = (node_or_url or "").strip()
        if not token:
            return {}
        # 裸 token（非 URL）→ 包成通用 wiki URL，让 CLI 走"节点解析"而非"obj_token 直用"。
        if "://" not in token and "/" not in token:
            token = f"https://feishu.cn/wiki/{token}"
        args = ["wiki", "+node-get", "--node-token", token, "--as", "user", "--format", "json"]
        if obj_type:
            args.extend(["--obj-type", obj_type])
        try:
            raw = await self.run(*args, timeout=40)
        except LarkCLIError as e:
            log.warning("wiki node-get failed for %s: %s", node_or_url, e)
            return {}
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            return {}
        obj = data.get("obj_token")
        if not obj:
            return {}
        return {
            "obj_token": obj,
            "obj_type": (data.get("obj_type") or "").lower(),
            "title": data.get("title") or "",
            "space_id": data.get("space_id") or "",
        }

    # ── Base / Sheets ───────────────────────────────────────────────

    async def base_list_apps(self) -> list[dict]:
        # Share the cached drive walk with docs_list.
        items = await self._drive_walk()
        return [i for i in items if i.get("type") == "bitable"]

    async def docs_search_accessible(self, max_pages: int = 5) -> list[dict]:
        """Drive Search v2 → 用户实际能访问/最近交互的文档（含别人共享给我的）。

        与 drive files list 互补：list 只返用户 drive root 里的文件，
        search 能命中"共享给我"、"群里共享"、"我曾打开过"的所有文档。
        """
        items: list[dict] = []
        token: str | None = None
        for _ in range(max_pages):
            args = ["drive", "+search", "--query", " ", "--page-size", "20", "--as", "user"]
            if token:
                args.extend(["--page-token", token])
            try:
                raw = await self.run(*args, timeout=60)
            except LarkCLIError as e:
                log.warning("docs_search_accessible failed: %s", e)
                break
            if not isinstance(raw, dict):
                break
            data = raw.get("data") or raw
            results = data.get("results") or []
            for r in results:
                norm = _normalize_search_result(r)
                if norm:
                    items.append(norm)
            token = data.get("page_token") or None
            if not data.get("has_more") or not token:
                break
        return items

    async def users_resolve_names(self, open_ids: list[str]) -> dict[str, str]:
        """Batch-resolve open_id → localized_name. Returns {open_id: name}.

        lark-cli contact +search-user --user-ids 虽接受很多 ID，但服务端单次
        **只返回前 ~20 条**（无 has_more 提示，静默截断）。故必须按 15 个/批切，
        否则一半用户解析不到。缺失的 ID（外部/离职）直接不在返回 map 里。
        """
        out: dict[str, str] = {}
        unique = list({i for i in open_ids if i and i.startswith("ou_")})
        if not unique:
            return out
        for i in range(0, len(unique), 15):
            chunk = unique[i : i + 15]
            try:
                raw = await self.run(
                    "contact", "+search-user",
                    "--user-ids", ",".join(chunk),
                    "--as", "user",
                    timeout=30,
                )
            except LarkCLIError as e:
                log.warning("users_resolve_names chunk failed: %s", e)
                continue
            users = []
            if isinstance(raw, dict):
                data = raw.get("data") or {}
                users = data.get("users") or []
                if not users and isinstance(raw.get("users"), list):
                    users = raw["users"]
            for u in users:
                oid = u.get("open_id")
                name = (u.get("localized_name") or u.get("name")
                        or u.get("user_name") or "")
                if oid and name:
                    out[oid] = name
        return out

    async def users_resolve_profiles(self, open_ids: list[str]) -> dict[str, dict]:
        """批量解析 open_id → {open_id, name, email, department}。

        与 users_resolve_names 同走 ``contact +search-user --user-ids``，但保留完整
        记录：姓名 / 邮箱 / 部门路径。部门路径形如 ``集团总部-数码科技-信息安全``，
        供组织架构图谱拆解层级。服务端单次只返回 ~20 条且无 has_more，故按 15 个/批
        切。缺失或外部用户直接不在返回 map 中。
        """
        out: dict[str, dict] = {}
        unique = list({i for i in open_ids if i and i.startswith("ou_")})
        if not unique:
            return out
        for i in range(0, len(unique), 15):
            chunk = unique[i : i + 15]
            try:
                raw = await self.run(
                    "contact", "+search-user",
                    "--user-ids", ",".join(chunk),
                    "--as", "user",
                    timeout=30,
                )
            except LarkCLIError as e:
                log.warning("users_resolve_profiles chunk failed: %s", e)
                continue
            users = []
            if isinstance(raw, dict):
                data = raw.get("data") or {}
                users = data.get("users") or []
                if not users and isinstance(raw.get("users"), list):
                    users = raw["users"]
            for u in users:
                oid = u.get("open_id")
                if not oid:
                    continue
                out[oid] = {
                    "open_id": oid,
                    "name": (u.get("localized_name") or u.get("name")
                             or u.get("user_name") or ""),
                    "email": u.get("email") or u.get("enterprise_email") or "",
                    "department": u.get("department") or "",
                }
        return out

    async def contact_departments(self) -> list[dict]:
        """全量部门树（**应用身份** `--as bot`）。

        通讯录数据范围授予的是应用身份，故必须用 bot 调；用户身份只能拿到没有
        名称/人数的空壳。``fetch_child=true`` + ``--page-all`` 一次取回所有后代部门。
        返回 ``[{id, name, member_count, parent}]``（id/parent 均为 open_department_id，
        顶级 parent 为 "0"）。
        """
        raw = await self.run(
            "api", "GET", "/open-apis/contact/v3/departments",
            "--params", json.dumps({
                "parent_department_id": "0",
                "fetch_child": True,
                "department_id_type": "open_department_id",
            }),
            "--as", "bot", "--page-all", "--page-limit", "0", "--format", "json",
            timeout=120,
        )
        data = raw.get("data") if isinstance(raw, dict) else None
        items = (data or {}).get("items") or []
        out: list[dict] = []
        for d in items:
            did = d.get("open_department_id") or d.get("department_id")
            if not did:
                continue
            out.append({
                "id": did,
                "name": d.get("name") or did,
                "member_count": int(d.get("member_count") or 0),
                "parent": d.get("parent_department_id") or "0",
            })
        return out

    async def contact_department_members(self, dept_id: str) -> list[dict]:
        """某部门的**直属**成员（应用身份）。返回 ``[{open_id, name, email}]``。"""
        raw = await self.run(
            "api", "GET", "/open-apis/contact/v3/users",
            "--params", json.dumps({
                "department_id": dept_id,
                "department_id_type": "open_department_id",
                "user_id_type": "open_id",
            }),
            "--as", "bot", "--page-all", "--page-limit", "0", "--format", "json",
            timeout=60,
        )
        data = raw.get("data") if isinstance(raw, dict) else None
        items = (data or {}).get("items") or []
        out: list[dict] = []
        for u in items:
            oid = u.get("open_id")
            if not oid:
                continue
            out.append({
                "open_id": oid,
                "name": u.get("name") or "",
                "email": u.get("email") or u.get("enterprise_email") or "",
            })
        return out

    async def base_tables_list(self, app_token: str) -> list[dict]:
        out = await self.run(
            "base", "tables", "list",
            "--params", json.dumps({"app_token": app_token}),
            timeout=60,
        )
        return _extract_items(out)

    async def sheets_get(self, spreadsheet_token: str) -> dict:
        return await self.run(
            "sheets", "spreadsheets", "get",
            "--params", json.dumps({"spreadsheet_token": spreadsheet_token}),
            timeout=60,
        )

    # ── Structured table reads (for 多维表格分析 Agent) ───────────────
    # 这些方法把 bitable / sheet 统一抽象成 (headers, rows) 二维结构，供
    # services.table_profile 做确定性列画像。与上面的 *_fetch_summary（输出
    # Markdown 给 HTML 生成）互补：这里要结构化数据而非排版文本。

    async def base_list_tables(self, app_token: str) -> list[dict]:
        """列出多维表格里的数据表 → [{table_id, name}]。用 +table-list 短指令。"""
        raw = await self.run(
            "base", "+table-list", "--base-token", app_token, "--limit", "50", timeout=40,
        )
        out: list[dict] = []
        for t in _extract_items(raw):
            tid = t.get("table_id") or t.get("id")
            if tid:
                out.append({"table_id": tid, "name": t.get("name") or tid})
        return out

    async def base_table_records(self, app_token: str, table_id: str, *, limit: int = 500) -> tuple[list[str], list[list]]:
        """读单张数据表 → (字段名列表, 行列表)。

        ``base +record-list --format json`` 的返回结构（lark-cli 1.0.43）：
          {"data": {"fields": ["序号","当前进展",...],   # 表头，按列序
                    "data":   [[cell, cell, ...], ...],  # 行，与 fields 对齐
                    "has_more": bool, "record_id_list": [...]}}
        富类型单元格（人员/多选/关联/双向链接）以 ``list[str]`` 给出；空单元格为 None。
        """
        raw = await self.run(
            "base", "+record-list", "--base-token", app_token,
            "--table-id", table_id, "--limit", str(limit), "--format", "json", timeout=90,
        )
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            return [], []
        headers = [str(h) for h in (data.get("fields") or [])]
        rows = [r for r in (data.get("data") or []) if isinstance(r, list)]
        return headers, rows

    async def sheet_list_sheets(self, spreadsheet_token: str) -> list[dict]:
        """列出电子表格里的工作表 → [{sheet_id, title, rows, cols}]。"""
        info = await self.run("sheets", "+info", "--spreadsheet-token", spreadsheet_token, timeout=30)
        data = info.get("data") if isinstance(info, dict) else None
        wrap = (data or {}).get("sheets")
        lst = wrap.get("sheets") if isinstance(wrap, dict) else (wrap if isinstance(wrap, list) else [])
        out: list[dict] = []
        for s in lst or []:
            sid = s.get("sheet_id") or s.get("id")
            if not sid:
                continue
            gp = s.get("grid_properties") or {}
            out.append({
                "sheet_id": sid,
                "title": s.get("title") or sid,
                "rows": gp.get("row_count"),
                "cols": gp.get("column_count"),
            })
        return out

    async def sheet_read_grid(self, spreadsheet_token: str, sheet_id: str, *, max_rows: int = 300, max_cols: int = 50) -> list[list]:
        """读工作表前 max_rows 行 × max_cols 列 → 行列表（第 0 行通常为表头）。"""
        end_col = _col_letter(max(1, min(max_cols, 200)))
        vals = await self.run(
            "sheets", "+read",
            "--spreadsheet-token", spreadsheet_token,
            "--sheet-id", sheet_id,
            "--range", f"A1:{end_col}{max_rows}",
            "--value-render-option", "FormattedValue",
            timeout=90,
        )
        return _sheet_values_to_rows(vals)

    # ── Markdown summaries for HTML page generation ─────────────────

    async def bitable_fetch_summary(self, app_token: str, *, max_tables: int = 8,
                                    rows_per_table: int = 300, char_budget: int = 100000) -> str:
        """Pull bitable tables + rows of each, formatted as Markdown.

        读够「整张表」是 HTML 生成忠实还原内容的前提：早期写死「2 表 × 15 行 × 每表 5000 字」，
        几十行的项目组合表会被砍到只剩开头（用户看到的页面只覆盖表头一小段）。现在默认读前
        8 张表、每表至多 300 行，并用 ``char_budget`` 兜住**总量**（防止超大多维表格爆 LLM
        上下文）——逐表累加字符，到预算就停并标注省略；单张巨表也受「剩余预算」约束，
        不会一张表吃满全部额度。
        """
        try:
            tables_raw = await self.run(
                "base", "+table-list", "--base-token", app_token, "--limit", "50",
                timeout=30,
            )
        except LarkCLIError as e:
            raise RuntimeError(f"bitable 表清单失败: {e.stderr or e}") from e
        tables = _extract_items(tables_raw)
        if not tables:
            return f"# 多维表格 {app_token}\n\n（无可访问表格）\n"

        shown = min(max_tables, len(tables))
        parts: list[str] = [
            f"# 多维表格内容预览 ({app_token})", "",
            f"共 {len(tables)} 张表，读取前 {shown} 张，每张最多 {rows_per_table} 行：", "",
        ]
        used = sum(len(p) + 1 for p in parts)
        truncated = False
        for t in tables[:max_tables]:
            tid = t.get("table_id") or t.get("id")
            tname = t.get("name") or tid
            if not tid:
                continue
            if used >= char_budget:
                truncated = True
                break
            header = f"## 表：{tname} (`{tid}`)"
            parts.append(header)
            parts.append("")
            used += len(header) + 2
            try:
                md = await self.run(
                    "base", "+record-list",
                    "--base-token", app_token,
                    "--table-id", tid,
                    "--limit", str(rows_per_table),
                    "--format", "markdown",
                    parse_json=False,
                    timeout=90,
                )
                body = str(md or "（空）")
                # 单表也受剩余预算约束，避免某张巨表吃满全部预算、把后面的表挤掉。
                remaining = max(2000, char_budget - used)
                if len(body) > remaining:
                    body = body[:remaining].rstrip() + "\n\n…（本表较长，已截断）"
                    truncated = True
                parts.append(body)
                used += len(body)
            except LarkCLIError as e:
                parts.append(f"（拉取失败：{str(e)[:120]}）")
            parts.append("")
        if truncated or len(tables) > shown:
            parts.append("")
            parts.append(f"> 注：为控制长度，部分表 / 行未全部展开（多维表格共 {len(tables)} 张表）。")
        return "\n".join(parts)

    async def sheet_fetch_summary(self, spreadsheet_token: str, *, max_rows: int = 300) -> str:
        """Read sheet metadata + first N rows of the first sheet as Markdown.

        早期写死 50 行 × A:Z（26 列），稍大的表就读不全。现在默认读前 300 行，列宽按
        首个工作表的实际列数（封顶 200 列），尽量把整张表喂给 HTML 生成。
        """
        try:
            info = await self.run(
                "sheets", "+info",
                "--spreadsheet-token", spreadsheet_token,
                timeout=30,
            )
        except LarkCLIError as e:
            raise RuntimeError(f"sheets +info 失败: {e.stderr or e}") from e

        # Response shape (lark-cli 1.0.43):
        #   {"data": {"sheets": {"sheets": [...], "title": "..."}}}
        # The outer "sheets" key wraps another object that contains the list.
        data = info.get("data") if isinstance(info, dict) else None
        sheets_wrap = (data or {}).get("sheets")
        if isinstance(sheets_wrap, dict):
            sheets_list = sheets_wrap.get("sheets") or []
            title = sheets_wrap.get("title") or spreadsheet_token
        elif isinstance(sheets_wrap, list):
            sheets_list = sheets_wrap
            title = (data or {}).get("title") or spreadsheet_token
        else:
            sheets_list = []
            title = (info or {}).get("title") or spreadsheet_token

        parts = [f"# 电子表格 · {title}", ""]
        if sheets_list:
            parts.append(f"包含 {len(sheets_list)} 个工作表：" + "、".join(
                s.get("title") or s.get("sheet_id") or "?" for s in sheets_list[:10]
            ))
            parts.append("")
        # Read first sheet
        first = sheets_list[0] if sheets_list else None
        if first:
            sheet_id = first.get("sheet_id") or first.get("id")
            sheet_title = first.get("title") or sheet_id
            # 列宽按表实际列数（grid_properties.column_count），缺省 50，封顶 200。
            col_count = ((first.get("grid_properties") or {}).get("column_count")) or 50
            end_col = _col_letter(max(1, min(int(col_count), 200)))
            parts.append(f"## 工作表：{sheet_title}")
            parts.append("")
            try:
                vals = await self.run(
                    "sheets", "+read",
                    "--spreadsheet-token", spreadsheet_token,
                    "--sheet-id", sheet_id,
                    "--range", f"A1:{end_col}{max_rows}",
                    "--value-render-option", "FormattedValue",
                    timeout=90,
                )
                rows = _sheet_values_to_rows(vals)
                if rows:
                    headers = rows[0]
                    parts.append("| " + " | ".join(str(c) for c in headers) + " |")
                    parts.append("|" + "|".join(["---"] * len(headers)) + "|")
                    for r in rows[1: max_rows]:
                        parts.append("| " + " | ".join(str(c) for c in r) + " |")
                else:
                    parts.append("（无数据）")
            except LarkCLIError as e:
                parts.append(f"（拉取失败：{str(e)[:120]}）")
        return "\n".join(parts)

    async def slides_fetch_text(self, presentation_id: str) -> str:
        """Best-effort slides reader.

        Feishu's ``slides xml_presentations get`` only works on slides created
        with the new XML editor. Imports from PPT/legacy "Lark Slides" return
        ``expired resource deleted by ka``. For those, we fall back to a
        metadata-only stub so the HTML generation still produces a coherent
        cover page (title/owner/space + a note).
        """
        try:
            raw = await self.run(
                "slides", "xml_presentations", "get",
                "--params", json.dumps({"xml_presentation_id": presentation_id}),
                timeout=60,
            )
        except LarkCLIError as e:
            stderr = e.stderr or ""
            if "expired resource" in stderr or "Unknown" in stderr or "permission" in stderr.lower():
                return (
                    f"# 演示文稿 {presentation_id}\n\n"
                    "⚠️ **飞书 slides XML API 无法读取此演示文稿**（通常因为是从 PPT 导入或来自旧版幻灯片产品）。\n"
                    "正文未抽取，仅基于资产元数据生成页面。建议把文档转为飞书新版幻灯片后再试。\n"
                )
            raise RuntimeError(f"slides xml_presentations get 失败: {stderr or e}") from e

        if not raw:
            return f"# 演示文稿 {presentation_id}\n\n（无内容）"
        xml_text = ""
        if isinstance(raw, str):
            xml_text = raw
        elif isinstance(raw, dict):
            data = raw.get("data") or raw
            pres = data.get("xml_presentation") or data
            xml_text = (pres.get("content") if isinstance(pres, dict) else None) \
                or (pres.get("xml") if isinstance(pres, dict) else None) \
                or (pres.get("body") if isinstance(pres, dict) else None) \
                or json.dumps(data, ensure_ascii=False)
        return _slides_xml_to_markdown(str(xml_text), presentation_id)

    # ── Meetings / Minutes / Calendar ───────────────────────────────

    async def calendar_events(self) -> list[dict]:
        out = await self.run("calendar", "+agenda", timeout=30)
        return _extract_items(out)

    async def minutes_list(self) -> list[dict]:
        # lark-cli 1.0.44 的 `minutes minutes` 只有 `get`（按 token 取单条），没有 `list`
        # 子命令；飞书也没有"列出我的全部妙记"的接口。强行调用只会撞 unknown flag、
        # 把 index_service 的 all_ok 置 False（从而跳过「已删除资产」清理）。故直接返回空：
        # 会议纪要仍可经文档/PDF 等其它来源进索引，单条妙记用 minutes_get_content 按 token 取。
        return []

    async def minutes_get_content(self, minute_token: str) -> dict:
        """取妙记信息（标题/时长/链接）+ 转写正文（尽力而为）。

        返回 ``{title, url, duration_ms, transcript}``。

        - 元信息走 ``minutes minutes get``（API: GET /minutes/v1/minutes/{token}）。
        - 转写正文飞书没有专门的 lark-cli 短指令，走原始
          ``api GET /open-apis/minutes/v1/minutes/{token}/transcript``。该接口可能要求
          ``minutes:minutes:readonly`` 等 scope；取不到时 transcript 留空，由上层降级
          （改读会议记录文档）。两步都失败都不抛异常。
        """
        info = {"title": "", "url": "", "duration_ms": "", "transcript": ""}
        try:
            raw = await self.run(
                "minutes", "minutes", "get",
                "--params", json.dumps({"minute_token": minute_token}),
                "--as", "user", "--format", "json", timeout=40,
            )
            data = (raw.get("data") if isinstance(raw, dict) else None) or (raw if isinstance(raw, dict) else {})
            minute = data.get("minute") if isinstance(data, dict) else None
            minute = minute or data or {}
            if isinstance(minute, dict):
                info["title"] = minute.get("title") or ""
                info["url"] = minute.get("url") or ""
                info["duration_ms"] = minute.get("duration") or ""
        except LarkCLIError as e:
            log.warning("minutes get failed: %s", e)

        try:
            raw = await self.run(
                "api", "GET",
                f"/open-apis/minutes/v1/minutes/{minute_token}/transcript",
                "--params", json.dumps({"need_speaker": True, "need_timestamp": False, "file_format": "txt"}),
                "--as", "user", "--format", "json", timeout=90,
            )
            info["transcript"] = _extract_transcript_text(raw)
        except LarkCLIError as e:
            log.warning("minutes transcript failed (scope?): %s", e)

        return info

    # ── IM / Mail / Tasks ───────────────────────────────────────────

    async def im_send(self, chat_id: str, text: str, dry_run: bool = False, markdown: bool = False) -> dict:
        # 关键：经 --content 传 JSON，而不是 --text/--markdown 直传。
        # 直传多行文本时，Windows 上 lark-cli.CMD 经 cmd.exe 解析会在**第一个换行处截断**
        # （只发出首行）；而 JSON 里换行是转义的 \n（argv 中无真实换行），不会被截断。
        # markdown=True → 转飞书富文本 post（保留 **加粗** / 换行 / emoji）。
        if markdown:
            content = json.dumps(_markdown_to_post(text), ensure_ascii=False)
            msg_type = "post"
        else:
            content = json.dumps({"text": text}, ensure_ascii=False)
            msg_type = "text"
        args = ["im", "+messages-send", "--chat-id", chat_id, "--msg-type", msg_type, "--content", content]
        if dry_run:
            args.append("--dry-run")
        # run() 会自动追加 --json；该子命令不识别会报错并自动剥掉重试（不会重复发送）。
        return await self.run(*args, timeout=30)

    async def im_chat_list(self, *, page_size: int = 50) -> list[dict]:
        """列出当前用户所在的群 → [{chat_id, name, members}]，供「协作分发」选目标群。

        用户身份（--as user）+ 按最近活跃排序，把常用群排前面。
        """
        try:
            out = await self.run(
                "im", "+chat-list", "--as", "user",
                "--page-size", str(page_size), "--sort-type", "ByActiveTimeDesc",
                timeout=40,
            )
        except LarkCLIError as e:
            log.warning("im chat-list failed: %s", e)
            return []
        # 响应形如 {"ok":true, "data":{"chats":[...]}}：列表在 data.chats，显式取，
        # 兜底再走通用 _extract_items。
        rows: list = []
        if isinstance(out, dict):
            data = out.get("data") if isinstance(out.get("data"), dict) else out
            rows = data.get("chats") if isinstance(data.get("chats"), list) else _extract_items(out)
        elif isinstance(out, list):
            rows = out
        res: list[dict] = []
        for c in rows:
            if not isinstance(c, dict):
                continue
            cid = c.get("chat_id") or c.get("id")
            if not cid:
                continue
            res.append({
                "chat_id": cid,
                "name": c.get("name") or cid or "(未命名群)",
                "members": c.get("member_count") or c.get("user_count"),
                "external": bool(c.get("external")),
            })
        return res

    async def task_create(self, title: str, due: str | None = None, description: str | None = None, dry_run: bool = False) -> dict:
        # 注意：lark-cli 子命令是 ``task``（单数），任务标题用 ``--summary``。
        args = ["task", "+create", "--summary", title]
        if due:
            args.extend(["--due", due])
        if description:
            args.extend(["--description", description])
        if dry_run:
            args.append("--dry-run")
        return await self.run(*args, timeout=30)

    # ── Raw API fallback ────────────────────────────────────────────

    async def api(self, method: str, path: str, data: dict | None = None, params: dict | None = None) -> dict:
        args = ["api", method.upper(), path]
        if data:
            args.extend(["--data", json.dumps(data)])
        if params:
            args.extend(["--params", json.dumps(params)])
        return await self.run(*args, timeout=60)


def _col_letter(n: int) -> str:
    """1-based 列序号 → A1 列字母（1→A, 26→Z, 27→AA, 50→AX）。"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s or "A"


def _sheet_values_to_rows(vals: Any) -> list[list]:
    """Extract value rows from sheets +read response shape variations."""
    if not isinstance(vals, dict):
        return []
    data = vals.get("data") or vals.get("valueRange") or vals
    if isinstance(data, dict):
        rows = data.get("values") or data.get("valueRange", {}).get("values") if isinstance(data.get("valueRange"), dict) else None
        if isinstance(rows, list):
            return [[c if c is not None else "" for c in r] for r in rows]
    if isinstance(vals.get("values"), list):
        return vals["values"]
    return []


def _slides_xml_to_markdown(xml_text: str, doc_id: str) -> str:
    """Best-effort extract: pull <text>/<run>/title content from slides XML.

    Slides XML schema is intricate; we just grab plain text inside tags so the
    LLM has something to reorganize. Order is preserved.
    """
    import re as _re
    # 段落级文本：捕获所有 <text>...</text> 与 <title>...</title>
    chunks: list[str] = []
    # Slides XML wraps actual text inside <run> or <p> tags inside <text-frame>.
    # We use a permissive extractor: any tag that closes is treated as a text container.
    inner_text = _re.findall(r">([^<>]+?)<", xml_text)
    seen = set()
    for t in inner_text:
        t = t.strip()
        if not t or t in seen:
            continue
        # Skip pure numeric / coordinate noise
        if _re.fullmatch(r"[\d.,\s\-x:]+", t):
            continue
        seen.add(t)
        chunks.append(t)
    if not chunks:
        return f"# 演示文稿 {doc_id}\n\n（解析 XML 后未提取出有效文本，原始长度 {len(xml_text)}）"
    return f"# 演示文稿 {doc_id}\n\n" + "\n".join(f"- {c}" for c in chunks[:300])


def _normalize_search_result(r: dict) -> dict | None:
    """drive +search 返回的结构嵌在 result_meta 里，把它平展到我们 _upsert 期望的形状。"""
    m = r.get("result_meta") or {}
    token = m.get("token") or r.get("token")
    if not token:
        return None
    # doc_types 是 "DOCX"/"SHEET"/"BITABLE"/"DOC"，转成内部小写形式。
    raw_type = (m.get("doc_types") or r.get("entity_type") or "doc").lower()
    type_map = {"docx": "docx", "doc": "doc", "sheet": "sheet", "bitable": "bitable",
                "slides": "slides", "file": "file", "folder": "folder",
                "shortcut": "shortcut", "mindnote": "mindnote", "wiki": "wiki"}
    asset_type = type_map.get(raw_type, raw_type)
    # 标题里可能含 <em>/&amp;/<mark> 等高亮标签，剥掉。
    title = (r.get("title_highlighted") or m.get("title") or "").strip()
    if title:
        import re as _re
        title = _re.sub(r"<[^>]+>", "", title)
        title = title.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return {
        "token": token,
        "type": asset_type,
        "name": title or "(未命名)",
        "url": m.get("url") or "",
        "owner_id": m.get("owner_id") or "",
        "owner_name": m.get("owner_name") or "",
        "modified_time": m.get("update_time") or m.get("edit_time") or "",
        "created_time": m.get("create_time") or "",
    }


_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_MD_BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")


def _md_inline(line: str) -> list[dict]:
    """行内 ``**...**`` → 加粗文本元素；其余为普通文本。"""
    elems: list[dict] = []
    pos = 0
    for m in _MD_BOLD_RE.finditer(line):
        if m.start() > pos:
            elems.append({"tag": "text", "text": line[pos:m.start()]})
        elems.append({"tag": "text", "text": m.group(1), "style": ["bold"]})
        pos = m.end()
    if pos < len(line):
        elems.append({"tag": "text", "text": line[pos:]})
    return elems


def _markdown_to_post(md: str) -> dict:
    """把 Markdown 文本转成飞书富文本 post 结构。

    用于 ``im_send(markdown=True)``：内容经 ``--content`` 以 JSON 传入，换行被转义为
    ``\\n``（argv 里没有真实换行），既避免 Windows 下 cmd.exe 在换行处截断，又保留排版。
    飞书 post 的文本元素只有 bold/italic 等样式、没有标题/列表语义，故：
      · ``#``～``######`` 标题行 → 整行加粗（去掉 ``#`` 标记），不再显示字面 ``##``；
      · ``-`` / ``*`` / ``+`` 无序列表 → 用「• 」前缀，行内继续解析加粗；
      · 其余行 → 行内加粗解析。
    这样群里看到的是干净排版，而不是字面的 ``##`` / ``-`` 等 Markdown 符号。
    """
    content: list[list[dict]] = []
    for raw_line in (md or "").split("\n"):
        line = raw_line.rstrip()
        h = _MD_HEADING_RE.match(line)
        if h:
            elems = [{"tag": "text", "text": h.group(1).strip(), "style": ["bold"]}]
            content.append(elems or [{"tag": "text", "text": ""}])
            continue
        b = _MD_BULLET_RE.match(line)
        if b:
            elems = [{"tag": "text", "text": "• "}] + _md_inline(b.group(1))
            content.append(elems or [{"tag": "text", "text": ""}])
            continue
        elems = _md_inline(line)
        content.append(elems or [{"tag": "text", "text": ""}])
    return {"zh_cn": {"title": "", "content": content}}


def _extract_transcript_text(raw: Any) -> str:
    """从妙记 transcript 接口的返回里提取纯文本。

    该接口以 ``file_format=txt`` 返回时常是纯文本（被 run() 包成 {"_raw": ...}）；
    也可能是 JSON，把文字放在 data.transcript / data.content / sentences[].text 里。
    """
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        if isinstance(raw.get("_raw"), str):
            return raw["_raw"].strip()
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        for key in ("transcript", "content", "text"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        # 句子数组形态：[{text/sentence, speaker_name}]
        sentences = data.get("sentences") or data.get("paragraphs")
        if isinstance(sentences, list):
            lines: list[str] = []
            for s in sentences:
                if not isinstance(s, dict):
                    continue
                spk = (s.get("speaker_name") or s.get("speaker") or "").strip()
                txt = (s.get("text") or s.get("sentence") or s.get("content") or "").strip()
                if txt:
                    lines.append(f"{spk}：{txt}" if spk else txt)
            if lines:
                return "\n".join(lines)
    return ""


def _extract_items(out: Any) -> list[dict]:
    """Unify list-shape extraction across lark-cli command output variations.

    Common shapes:
      [...]                                  raw list
      {"items": [...]}                       wiki spaces/nodes
      {"data": {"items": [...]}}             wiki standard
      {"data": {"files": [...]}}             drive files
      {"data": {"tables": [...]}}            base +table-list
      {"data": {"records": [...]}}           base +record-list (JSON mode)
      {"data": {"spreadsheets": [...]}}      sheets info variants
    """
    LIST_KEYS = ("items", "files", "nodes", "tables", "records", "spreadsheets",
                 "users", "spaces", "results", "list", "rows", "chats")
    if isinstance(out, list):
        return out
    if isinstance(out, dict):
        for key in LIST_KEYS + ("data",):
            v = out.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for k2 in LIST_KEYS:
                    v2 = v.get(k2)
                    if isinstance(v2, list):
                        return v2
        return [out]
    return []


# ── 单例获取（启动时初始化一次） ────────────────────────────────────────

_instance: LarkCLI | None = None


async def get_lark() -> LarkCLI:
    global _instance
    if _instance is None:
        _instance = LarkCLI()
        await _instance.ping()
    return _instance
