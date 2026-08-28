"""在 Outlook 里打开某一封邮件。**这是本功能唯一有副作用的调用。**

## 为什么单独一个模块

services/outlook.py 有一条硬保证：整个模块没有任何写/发/删/移动/标记操作，
并且有 AST 检查在守着它（见 scratchpad/check_readonly.py 的思路）。
下面这个 Display() 不修改邮件内容，但它**打开一个窗口**，而 Outlook 自己会因为
"用户看了这封" 把它标成已读 —— 那是邮箱状态的变化。

把它塞进那个模块会让「只读」这三个字变得需要加脚注，而一条需要加脚注的保证等于
没有保证。所以放在这里：文件树上一眼能看出「这个模块会动东西」。

## 边界

- 只接受一个 EntryID，只调 Display()。不改任何属性，不发送，不删除，不移动。
- **只在用户点击时触发**，页面加载绝不调用。
- 打开哪一封由前端传 ID 决定，而那个 ID 来自我们自己刚读出来的列表 ——
  不接受来自邮件正文的任何内容作为参数。
- 在被 Object Model Guard 卡住的邮箱上，这个调用同样可能挂住（GetItemFromID 会
  触发地址信息访问）。所以它走和取数一样的 run_com 硬超时，失败就给人话错误，
  不留一个转不完的圈。
"""
from __future__ import annotations

from typing import Any

from .outlook import OutlookError, _s, _session, run_com


def _open_sync(entry_id: str) -> dict:
    ns = _session.ns_retry()
    try:
        item = ns.GetItemFromID(entry_id)
    except Exception as e:  # noqa: BLE001
        raise OutlookError(
            "在 Outlook 里没找到这封邮件。它可能已经被移动或删除了 —— "
            "刷新一下列表再试。") from e
    subject = ""
    try:
        subject = _s(getattr(item, "Subject", ""))
    except Exception:  # noqa: BLE001
        pass
    # 唯一的副作用：把邮件窗口调出来。之后回复与否是用户在 Outlook 里的事，
    # 我们不代劳、也不预填 —— 发信这件事不该由这个程序碰。
    item.Display()
    return {"ok": True, "subject": subject}


async def open_item(entry_id: str) -> dict:
    eid = (entry_id or "").strip()
    if not eid:
        raise OutlookError("没有指定要打开哪一封邮件。")
    # 演示数据的 id 是 demo-01 这种，不是真的 EntryID。真实 EntryID 是长十六进制串，
    # 这里挡一下，免得对着假数据去问 Outlook，白等一次超时。
    if eid.startswith("demo-"):
        raise OutlookError("这是演示数据里的假邮件，Outlook 里并不存在这一封。")
    return await run_com(_open_sync, eid)
