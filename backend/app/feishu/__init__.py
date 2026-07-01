"""飞书 CLI 适配层 — 封装 @larksuite/cli 子进程调用。"""

from .cli import LarkCLI, LarkCLIError, get_lark
from .mock import MockLarkCLI

__all__ = ["LarkCLI", "MockLarkCLI", "LarkCLIError", "get_lark"]
