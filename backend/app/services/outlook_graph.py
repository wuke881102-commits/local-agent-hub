"""本地邮箱 —— 时间轴 + 关系网。**纯函数，不碰 COM，不调大模型。**

## 一个反直觉的结论：时间轴不需要 AI

「对某个项目的多封往来邮件画时间轴」听起来像个 AI 活儿，其实不是。我手上已经有
每封邮件的发件人、时间、方向（收件箱 = 别人发的；已发送里的同会话记录 = 我发的）、
以及收件人栏的显示名。一条会话的完整时间轴和参与者关系图是可以**算出来**的。

算出来的时间轴永远不会错；AI 画的会偶尔编一个不存在的节点。所以骨架必须是确定性的。
AI 唯一该做的事是给每一步配一句摘要（「张伟提出金额差异」比光看主题有用），
那是 outlook_ai 的活，而且它失败时时间轴照样完整。

## 关系网的依据

只有 To/CC 的**显示名字符串**。不查通讯簿 —— 那会让 Outlook 无限期挂住
（见 outlook.py 里 _me 的警告）。所以关系是「谁和谁在同一封邮件的收件人栏里出现过」，
不是组织架构上的关系。这个口径要在界面上说清楚，别让人以为是汇报线。
"""
from __future__ import annotations

import re
from datetime import datetime

# 收件人栏的分隔符。Outlook 用分号，但转发来的、外部系统发的常混用逗号。
# **只按分号和换行切，不按逗号。** 这里曾经把逗号也当分隔符，是个真 bug：
# Outlook 的 To/CC 用「;」分隔收件人，而显示名本身是「姓, 名」格式
# （"Xu, Sean; Lye, Libby"）。按逗号切的结果是每个人被拆成两个假人 ——
# 关系网里出现一堆 Sean / Lye / Libby 这样的碎片，而且 addressing_of 数收件人
# 个数时把 1 个人数成 2 个，「只发我」这一档几乎永远判不出来。
_SPLIT = re.compile(r"[;；\r\n]+")
# 逗号**在某些形态下**才是分隔符：地址列表（含 @）或纯名字列表（2 个以上逗号）。
# 单个逗号且不含 @ 的，判定为「姓, 名」，保持整体。
_COMMA = re.compile(r"[,，]+")
# 显示名里常见的噪音：尖括号地址、引号、多余空白。
_ADDR_IN_NAME = re.compile(r"<[^>]*>")


def split_names(field: str, *, cap: int = 12) -> list[str]:
    """把 To/CC 字符串切成人名列表。**公开的**：outlook_tags 判「只 to 我 / to 我和
    其他人」时也要数收件人个数，两边必须用同一个切分口径，否则会漂移。

    cap 是必需的：群发邮件的收件人栏能有几百个名字，全画进关系网会得到一张
    看不出任何东西的毛球图 —— 那种图比没有图更糟。超出就截断并如实标注。
    """
    out: list[str] = []
    for raw in _SPLIT.split(field or ""):
        chunk = _ADDR_IN_NAME.sub("", raw).strip().strip("'\"").strip()
        if not chunk:
            continue
        # 这一块里的逗号到底是分隔符还是「姓, 名」的一部分？
        parts = [chunk]
        if "@" in chunk or len(_COMMA.findall(chunk)) >= 2:
            parts = _COMMA.split(chunk)
        for part in parts:
            nm = part.strip().strip("'\"").strip()
            if len(nm) < 2 or len(nm) > 40:
                continue
            if nm not in out:
                out.append(nm)
            if len(out) >= cap:
                return out
    return out


def _short(ts: float) -> str:
    d = datetime.fromtimestamp(ts).astimezone()
    return d.strftime("%m-%d %H:%M")


def build_graph(thread: dict, msgs: list[dict], sent_times: list[float],
                *, now: datetime | None = None) -> dict:
    """一个会话 → 时间轴 + 参与者关系网。

    sent_times 是我在这个会话里发出过的时间戳（来自已发送扫描）。只有时间没有内容 ——
    已发送邮件的正文我们只在承诺扫描里读过一封，不为了画时间轴再去读一遍。
    所以「我」的节点显示为「我回了一次」，不显示内容。这是诚实的残缺，不是 bug。
    """
    ordered = sorted(msgs, key=lambda m: m.get("received_ts") or 0.0)

    steps: list[dict] = []
    for m in ordered:
        ts = float(m.get("received_ts") or 0.0)
        if not ts:
            continue
        steps.append({
            "ts": ts, "at": _short(ts), "dir": "in",
            "actor": (m.get("sender_name") or m.get("sender_addr") or "对方"),
            "subject": m.get("subject") or "",
            # 「他这一步在要什么」。抽不到就空，前端不显示这一行。
            "ask": m.get("ask_hint") or "",
            "att_count": int(m.get("att_count") or 0),
            "msg_id": m.get("id") or "",
            # AI 摘要的落点。outlook_ai 填；填不上就保持空，时间轴照样完整。
            "summary": "",
        })

    lo = ordered[0].get("received_ts") if ordered else None
    for ts in sorted(sent_times or []):
        # 只画落在这个窗口里的：更早的回话不在本次取数范围内，画出来会让人以为
        # 时间轴是完整历史，而它只是最近 N 天。
        if lo and ts < float(lo):
            continue
        steps.append({"ts": float(ts), "at": _short(ts), "dir": "out",
                      "actor": "我", "subject": "", "ask": "",
                      "att_count": 0, "msg_id": "", "summary": ""})
    steps.sort(key=lambda s: s["ts"])

    # 每一步距上一步隔了多久 —— 「谁让这件事停了 4 天」一眼能看出来。
    prev = None
    for s in steps:
        gap = 0.0 if prev is None else max(0.0, s["ts"] - prev)
        s["gap_days"] = int(gap // 86400)
        s["gap_hours"] = int(gap // 3600)
        prev = s["ts"]

    # 参与者。
    #
    # ## 为什么不再画关系网（共现边）
    #
    # 原来的边是「两人在同一封邮件的收件人栁里同时出现过」。而在一个会话里，
    # 大家基本都在每封邮件的收件人栁里 —— 于是每个人跟每个人都有边，画出来
    # 是一张**完全图**。完全图的信息量是零：它不区分任何两个人。这不是布局
    # 或渲染问题，是这个指标本身在这个场景下没有区分度。
    #
    # 换成真能回答问题的：**谁在推这件事、谁一直没说话、最后一句是谁说的**。
    # 三个都能从已有数据里算出来，不需要额外读任何属性。
    people: dict[str, dict] = {}
    truncated = False

    def touch(name: str, key: str, ts: float = 0.0) -> None:
        p = people.setdefault(name, {"name": name, "sent": 0, "in_to": 0, "in_cc": 0,
                                     "last_ts": 0.0, "last_at": ""})
        p[key] = p.get(key, 0) + 1
        # 只有「发了信」才算发言时间；被收件/抄送不算。
        if key == "sent" and ts > p["last_ts"]:
            p["last_ts"], p["last_at"] = ts, _short(ts)

    for m in ordered:
        sender = (m.get("sender_name") or m.get("sender_addr") or "").strip()
        tos, ccs = split_names(m.get("to") or ""), split_names(m.get("cc") or "")
        if len(_SPLIT.split(m.get("to") or "")) > 12:
            truncated = True
        if sender:
            touch(sender, "sent", float(m.get("received_ts") or 0.0))
        for nm in tos:
            touch(nm, "in_to")
        for nm in ccs:
            touch(nm, "in_cc")

    # 发言多的在前，其次是被直接收件的。只被抄送又从不发言的排最后 ——
    # 那些人对「这件事现在卡在谁那儿」没有信息量。
    nodes = sorted(people.values(), key=lambda p: -(p["sent"] * 3 + p["in_to"] * 2 + p["in_cc"]))
    top_sent = max((p["sent"] for p in nodes), default=0)
    for p in nodes:
        if p["sent"] == 0:
            # 没发过信。分两种：被直接收件却没回（可能在等他），和只被抄送（旁观）。
            p["role"] = "只被抄送" if p["in_to"] == 0 else "收件但未发言"
        elif p["sent"] == top_sent and top_sent >= 2:
            p["role"] = "主要推动"
        else:
            p["role"] = "参与讨论"
    span = (steps[-1]["ts"] - steps[0]["ts"]) if len(steps) >= 2 else 0.0

    return {
        "conv_id": thread.get("conv_id") or "",
        "subject": thread.get("subject") or "",
        "steps": steps,
        "step_count": len(steps),
        "span_days": int(span // 86400),
        "nodes": nodes[:16],
        # 球在谁那儿：最后一步是谁走的。这是一个会话最有用的单一事实 ——
        # 最后一句是对方说的，就轮到我；是我说的，就在等对方。
        "last_actor": (steps[-1]["actor"] if steps else ""),
        "last_dir": (steps[-1]["dir"] if steps else ""),
        "recipients_truncated": truncated,
        # 我在这个会话里回过几次。0 = 从头到尾没回过。
        "my_replies": sum(1 for s in steps if s["dir"] == "out"),
    }


def pick_projects(threads: list[dict], msgs_by_conv: dict[str, list[dict]],
                  *, min_msgs: int = 2, cap: int = 12) -> list[dict]:
    """哪些会话值得画时间轴。

    判据是**往来封数**，不是主题看起来像不像项目：一来一回才有时间轴可言，
    单封邮件画出来是一个点。按封数和跨度排，用户自己挑。
    """
    out = []
    for t in threads:
        msgs = msgs_by_conv.get(t.get("conv_id") or "") or []
        n = len(msgs)
        if n < min_msgs:
            continue
        out.append({
            "conv_id": t.get("conv_id") or "",
            "subject": t.get("subject") or "",
            "people": t.get("people") or [],
            "msg_count": n,
            "last_received_label": t.get("last_received_label") or "",
            "waiting_label": t.get("waiting_label") or "",
        })
    out.sort(key=lambda x: -x["msg_count"])
    return out[:cap]
