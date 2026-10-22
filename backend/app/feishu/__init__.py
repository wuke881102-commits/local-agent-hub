"""飞书 CLI 适配层 — 封装 @larksuite/cli 子进程调用。"""

from .cli import LarkCLI, LarkCLIError, explain_error, get_lark, needs_relogin
from .mock import MockLarkCLI

__all__ = ["LarkCLI", "MockLarkCLI", "LarkCLIError", "get_lark",
           "explain_error", "needs_relogin"]
