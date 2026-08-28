# -*- coding: utf-8 -*-
"""守一条硬约束：Outlook 数据层严格只读。

背景：这个程序读的是用户真实工作邮箱。一次误发、误删、误标已读都是不可撤销的，
而 COM 里 ``item.Send()`` 和 ``item.Subject`` 长得一样无害。所以不靠人眼 review，
靠这个脚本卡住。

唯一豁免：``outlook_open.py`` 里的 ``MailItem.Display()`` —— 它只是把邮件在
Outlook 界面里打开给用户看，不改任何数据，且只由用户点击触发。

## 为什么用 ast 而不是正则

第一版用正则逐行扫，被 outlook.py 的模块 docstring 误伤了：那里写着
「将来若要加'写草稿箱'，只能是 MailItem.Save()」—— 是段说明，不是代码。
剥掉 ``#`` 注释不够，还得剥字符串、docstring、多行字符串。与其一层层补，
不如直接走语法树：ast 里根本不存在「注释」和「字符串内容」这回事。

这个脚本以前放在临时目录，会话一清就没了（确实丢过一次）。现在收进
``backend/tools/`` 跟着仓库走。

跑法：``python tools/check_readonly.py``
"""
from __future__ import annotations

import ast
import io
import pathlib
import sys

_DEFAULT_SVC = pathlib.Path(__file__).resolve().parent.parent / "app" / "services"
# 可传一个目录覆盖扫描目标 —— 用来自检「这脚本真的会报警，不是只会输出 OK」。
# 一个从不失败的检查和没有检查是一回事。
SVC = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else _DEFAULT_SVC

# COM 上的写操作。逐个都是不可撤销的。
FORBIDDEN_CALLS = {
    "Send", "Reply", "ReplyAll", "Forward", "Delete", "Move", "Copy",
    "Save", "SaveAs", "SaveAsFile", "Post", "Submit", "ClearConversationIndex",
    "MarkAsTask", "ClearTaskFlag",
}
# 可写属性：赋值就是改用户邮箱。UnRead 尤其阴 —— 有些读法会顺手把邮件标成已读。
FORBIDDEN_ASSIGN = {
    "UnRead", "Subject", "Body", "HTMLBody", "To", "CC", "BCC", "Categories",
    "FlagStatus", "FlagRequest", "Importance", "Sensitivity", "IsMarkedAsTask",
    "TaskDueDate", "ReminderSet", "MessageClass",
}
# 豁免：(文件名, 调用名) → 理由。不写理由的豁免会慢慢变成后门。
ALLOWED = {
    ("outlook_open.py", "Display"):
        "只在 Outlook 界面里打开邮件给用户看，不改数据，且仅由用户点击触发",
}


class Scan(ast.NodeVisitor):
    def __init__(self, fname: str) -> None:
        self.fname = fname
        self.hits: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in FORBIDDEN_CALLS:
            if (self.fname, f.attr) not in ALLOWED:
                self.hits.append((node.lineno, "调用了写操作 .%s()" % f.attr))
        self.generic_visit(node)

    def _check_target(self, t: ast.expr, lineno: int, how: str) -> None:
        if isinstance(t, ast.Attribute) and t.attr in FORBIDDEN_ASSIGN:
            self.hits.append((lineno, "%s可写属性 .%s" % (how, t.attr)))

    def visit_Assign(self, node: ast.Assign) -> None:
        for t in node.targets:
            self._check_target(t, node.lineno, "赋值给")
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._check_target(node.target, node.lineno, "增量赋值给")
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._check_target(node.target, node.lineno, "带注解赋值给")
        self.generic_visit(node)


problems: list[str] = []
checked = 0

for path in sorted(SVC.glob("outlook*.py")):
    src = io.open(path, encoding="utf-8").read()
    checked += 1
    tree = ast.parse(src, filename=str(path))
    sc = Scan(path.name)
    sc.visit(tree)
    lines = src.splitlines()
    for lineno, what in sc.hits:
        snippet = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
        problems.append("%s:%d  %s\n    %s" % (path.name, lineno, what, snippet))

print("扫了 %d 个 outlook* 模块（走 ast，注释和字符串不参与判定）" % checked)
for (fn, call), why in ALLOWED.items():
    print("  豁免 %s .%s() —— %s" % (fn, call, why))

if problems:
    print("")
    print("只读保证被破坏，%d 处：" % len(problems))
    for p in problems:
        print("  " + p)
    sys.exit(1)

print("")
print("只读保证成立：没有任何写/发/删/移动/标记调用，也没有对可写属性赋值。")
