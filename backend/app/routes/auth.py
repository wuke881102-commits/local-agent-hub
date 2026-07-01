from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

from ..feishu import LarkCLI, MockLarkCLI, get_lark
from ..config import settings

log = logging.getLogger("auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


async def _adapter():
    lark = await get_lark()
    if isinstance(lark, LarkCLI) and not await lark.ping():
        if settings.enable_mock_fallback:
            return MockLarkCLI(), True
        return lark, False
    return lark, False


@router.get("/status")
async def status() -> dict:
    lark, is_mock = await _adapter()
    info = await lark.auth_status()
    info["mock_mode"] = is_mock or info.get("mock", False)
    return info


@router.post("/login")
async def login(force: bool = False) -> dict:
    """触发飞书授权流。

    - mock 模式：直接返回模拟的 verification_uri / user_code。
    - 真实模式：调 ``lark-cli auth login --scope <最小权限集>``（见 cli.MIN_LOGIN_SCOPES）。
      用户在飞书同意页只会看到最小权限，而非 --recommend 拉来的全量授权。
      若 CLI 报告 "需要先 config init"，自动尝试 ``lark-cli config init --no-wait`` 再降级。
    - ``force=true``：即使已授权也重新发起登录，用于补授新增 scope（如发群消息 im:message）。
    """
    lark, is_mock = await _adapter()

    info = await lark.auth_login(force=force)
    info["mock_mode"] = is_mock or info.get("mock", False)

    # 兜底：若实际 CLI 未配置应用凭据，尝试自动 config init
    if isinstance(lark, LarkCLI) and not info.get("verification_uri") and not info.get("user_code"):
        log.info("auth_login 未返回 URL，尝试 config init 后重试")
        try:
            init_out = await lark.run("config", "init", "--no-wait", timeout=20)
            if isinstance(init_out, dict):
                info.update(init_out)
        except Exception as e:  # noqa: BLE001
            log.warning("config init failed: %s", e)
        # 再次尝试 login
        try:
            second = await lark.auth_login()
            for k, v in (second or {}).items():
                info.setdefault(k, v)
        except Exception as e:  # noqa: BLE001
            log.warning("auth_login retry failed: %s", e)

    # 兼容多种字段名：lark-cli 不同版本可能用 verification_url / verification_uri / url
    for canonical, aliases in (("verification_uri", ("verification_url", "url", "login_url", "auth_url")),
                               ("user_code", ("code", "device_code"))):
        if not info.get(canonical):
            for a in aliases:
                if info.get(a):
                    info[canonical] = info[a]
                    break

    return info


@router.get("/identities")
async def identities() -> dict:
    lark, _ = await _adapter()
    return {"items": await lark.auth_list()}
