"""本地邮箱 —— 把「邮件列表」重组成「欠账表」。**纯函数，一次 COM 调用都没有。**

## 为什么要有这一层

第一版这个页面是失败的：它按「什么到了」组织，而这正是 Outlook 已经做的事 ——
按时间排的列表、未读数、Focused Inbox、标记、搜索。照着同一根轴再做一遍，只能得到
一个更慢、字段更少、还不能操作的 Outlook。用户没有任何理由打开它。

用户来这个面板要回答的不是「什么到了」，是**「我现在有什么没交代的」**，
然后就走。所以这一层的输出全部按**责任**组织，而不是按到达时间：

  1. 到期在即 / 已逾期     —— 有硬期限的
  2. 有人在等你回话        —— 会话里最后一封是别人发的，且我一直没回
  3. 你答应过但还没做      —— 从**我自己的已发送邮件**里找出的承诺
  4. 今天可以不看的 N 封   —— 折叠成一行 + 一份为什么

第 2 条里那个「等了几天」是 Outlook 给不出来的数字：它按到达排序、按单封展示，
没有「最后一封入向比我最后一封出向新了 N 天」这个概念。
第 3 条 Outlook **完全没有**对应功能，是这个面板最强的存在理由。

## 三条硬规矩

**一、每个会话只出现在一个区块里。** 同一件事在两处出现，用户就会开始怀疑这些数字，
    整个面板的信任度归零。区块之间是排他的，优先级见 build_view()。
**二、宁可不说，不要猜错。** 抽不到「他到底问了什么」就不显示那一行；抽不到承诺就
    不报承诺。和 deadline_hint() 同一个取舍 —— 一条编出来的「你答应过 X」会让用户
    再也不信这一栏。
**三、第 4 区默认折叠成一行。** 不需要用户动的邮件不该和需要动的抢视觉权重。这一栏
    的价值是「我确认有 47 封、并且告诉你为什么都不用管」，不是「这里有 47 张卡片」。

## 邮件正文是不可信输入

下面所有抽取都是**正则匹配 + 截断**，抽出来的字符串只当数据显示，绝不解释、
不执行、不拼进任何指令。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────────────────────── 「他到底问了什么」

# 句子切分：中英文终止符 + 换行 + 分号。保留终止符，因为「是不是问句」要靠它判断。
_SENTENCE = re.compile(r"[^。！？!?\n；;]+[。！？!?]?")

# 请求类措辞。命中其一 → 这句话大概是在要东西。
# 只收高精度的：像「可以」「需要」这种单独出现太泛，不收。
_ASK_WORDS = (
    "麻烦你", "麻烦帮", "麻烦确认", "能不能", "能否", "是否", "请你", "请确认",
    "请提供", "请回复", "请告知", "确认一下", "确认下", "你看一下", "你看下",
    # 「你那边」曾在这里，实测会把「涉及你那边两家」这种陈述句当成诉求。
    # 弱信号一律不要：抽错一句比不抽更伤 —— 用户会觉得这栏在瞎猜。
    "你看看", "帮我", "帮忙", "需要你", "烦请", "盼复", "等你",
    "求确认", "可以吗", "行吗", "什么时候", "何时", "怎么办", "如何处理",
)

# 截断预算按**显示宽度**算，不按字符数：CJK 记 2，其余记 1。
#
# 原来是「90 个字符」一刀切，跨语言必然出问题：90 个汉字是一大段话，而 90 个
# 拉丁字符只有十几个词 —— 实测一句英文诉求正好被切在 "...on application e…"，
# 读者什么也看不懂，后面还留一大片空白。
# 400 个宽度单位 = 200 汉字 = 约 400 拉丁字符。
# 一开始给的 180（≈90 汉字）实测太窗：真实邮件里一句诉求常带着背景、链接和
# 条件（「请提前查阅 XX Audit Plan <https://...>」），切到 90 字往往刚好把关键那半句
# 截掉。前端那一行可以换行（overflowWrap: anywhere，没有行数限制），放宽不会撑破布局。
_ASK_WIDTH = 400

# 宽字区间：CJK 统一表、CJK 标点、全角形式。用码点而不用字符字面量，
# 避开源文件里出现不可见字符。
_WIDE = ((0x4E00, 0x9FFF), (0x3000, 0x303F), (0xFF00, 0xFFEF), (0x3400, 0x4DBF))


def _dwidth(s: str) -> int:
    """显示宽度：CJK / 全角标点算 2，其余算 1。"""
    w = 0
    for ch in s:
        o = ord(ch)
        w += 2 if any(a <= o <= b for a, b in _WIDE) else 1
    return w


def _clip_w(s: str, budget: int = _ASK_WIDTH) -> str:
    """按显示宽度截断。拉丁文本**不在词中间切** —— 切一半的单词比少一个词难读。"""
    if _dwidth(s) <= budget:
        return s
    w, cut = 0, len(s)
    for i, ch in enumerate(s):
        o = ord(ch)
        w += 2 if any(a <= o <= b for a, b in _WIDE) else 1
        if w > budget:
            cut = i
            break
    head = s[:cut]
    # 切点落在拉丁词中间时回退到上一个空格。回退超过 24 个字符就不退了
    # （那说明这段本来就没空格，多半是 CJK 或长串）。
    sp = head.rfind(" ")
    if sp > 0 and len(head) - sp <= 24 and head[-1].isascii() and not head[-1].isspace():
        head = head[:sp]
    return head.rstrip() + chr(0x2026)


def extract_ask(subject: str, body: str) -> str:
    """从正文里找出「他到底要我做什么」的那一句。抽不到就返回空串。

    单次正向扫描取**第一个**命中：真实邮件里主要诉求几乎总在最前面，后面的问句
    通常是补充。取最后一个或取最长的都试过更差 —— 会拿到「你那边方便吗」这种客套。

    只在正文里找，不碰主题：主题是标题不是诉求，「回复: 数据字典对齐」抽出来没用。
    """
    txt = (body or "").strip()
    if not txt:
        return ""
    for raw in _SENTENCE.findall(txt[:1500]):
        s = raw.strip()
        if len(s) < 6:                      # 「好的。」「收到。」之类，不是诉求
            continue
        if s.rstrip().endswith(("?", "？")) or any(w in s for w in _ASK_WORDS):
            return _clip_w(s)
    return ""


# ─────────────────────────────────────────────────────── 「我答应过什么」

# 从**我自己发出**的邮件里找承诺。精度优先于召回率：一条编出来的
# 「你答应过 X」比漏掉一条严重得多 —— 用户对着一件自己没说过的事，
# 会立刻不再信这一整栏。所以每条模式都要求「时间/动作词 + 明确的交付动词」同时出现。
_PROMISE_PATTERNS = (
    # 我明天发你 / 我下周给你同步 / 我这周整理
    re.compile(r"我[^。！？\n]{0,4}(今天|明天|后天|本周|这周|下周|下个?月|月底|周内)"
               r"[^。！？\n]{0,14}(发|给|回|确认|整理|提供|同步|反馈|安排|出)"),
    # 稍后发你 / 回头给你 / 晚点回复
    re.compile(r"(稍后|回头|晚点|待会|一会儿)[^。！？\n]{0,12}(发|给|回复|回你|同步|反馈)"),
    # 我确认后回复你 / 我去问一下再告诉你 —— 必须带明确的「回给你」动作
    re.compile(r"我[^。！？\n]{0,3}(来|去|先)?(跟进|确认|核对|核一下|问一下|查一下|了解)"
               r"[^。！？\n]{0,14}(回复|回你|告诉你|同步|反馈|给你)"),
    re.compile(r"(i['’]?ll|i will|let me)\s+[a-z ,]{0,24}"
               r"(send|check|get back|follow up|confirm|share|update)", re.I),
)

_PROMISE_MAX = 90


def promise_hint(body: str) -> str:
    """从我自己发出的正文里抽承诺原话。抽不到返回空串。

    返回的是**原话所在的那句**，不是模式本身 —— 用户要看的是自己当时怎么说的，
    「你答应过：我明天把口径发你」比「检测到承诺」有用得多，也更容易自查对错。
    """
    txt = (body or "").strip()
    if not txt:
        return ""
    for raw in _SENTENCE.findall(txt[:1500]):
        s = raw.strip()
        if len(s) < 4:
            continue
        if any(p.search(s) for p in _PROMISE_PATTERNS):
            return s[:_PROMISE_MAX] + ("…" if len(s) > _PROMISE_MAX else "")
    return ""


# ─────────────────────────────────────────────────────── 「今天可以不看」的归因

# 顺序即优先级：从最强的信号往下判。改顺序会改归因结果，别随手调。
_AUTO_ADDR_MARKS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "alert", "notification", "notify", "monitor", "automat", "system",
    "jenkins", "jira", "confluence", "mailer-daemon",
)

QUIET_LABELS = {
    "replied": "我已经回过了",
    "auto": "系统自动发送",
    "cc": "只是抄送我",
    "list": "通讯组或规则投递",
    "read": "看过、无待办",
}


def is_auto_sender(m: dict) -> bool:
    """这封是机器发的吗。

    机器不会等你回话 —— 这个判断决定了它能不能进「有人在等你」那一栏，
    而那一栏被噪音灌满一次，用户就再也不看了。
    """
    addr = (m.get("sender_addr") or "").lower()
    name = m.get("sender_name") or ""
    return (any(k in addr for k in _AUTO_ADDR_MARKS)
            or "告警" in name or "监控" in name or "通知" in name)


def quiet_kind(m: dict, replied: bool) -> str:
    """这封为什么今天可以不看。返回 QUIET_LABELS 的键。"""
    if replied:
        return "replied"
    if is_auto_sender(m):
        return "auto"
    if m.get("cc_me") is True:
        return "cc"
    if m.get("to_me") is False and m.get("cc_me") is False:
        return "list"
    return "read"


# ─────────────────────────────────────────────────────── 组装

# 「到期在即」的门槛。超过一周的期限还谈不上紧急，放它待在别的区块里，
# 卡片上照样有期限徽标，不会丢信息。
_DUE_SOON_DAYS = 7


def _now() -> datetime:
    return datetime.now().astimezone()


def _days_between(newer_ts: float, older_ts: float) -> int:
    return max(0, int((newer_ts - older_ts) // 86400))


def _wait_label(days: int, hours: int) -> str:
    if days >= 1:
        return "%d 天" % days
    if hours >= 1:
        return "%d 小时" % hours
    return "刚刚"


def _thread(msgs: list[dict], *, sent_map: dict[str, float], now_ts: float) -> dict:
    """把一个会话里的多封邮件收成一行。

    为什么按会话而不按单封：Outlook 已经能给你 8 封散装邮件了。用户要知道的是
    「这件事欠着」，一件事一行。同一个会话里 5 封催办展示成 5 张卡片是噪音。
    """
    ordered = sorted(msgs, key=lambda x: x.get("received_ts") or 0.0, reverse=True)
    newest = ordered[0]
    conv = newest.get("conv_id") or ""
    last_out = float(sent_map.get(conv, 0.0) or 0.0)

    # 「我最后一次回话之后，对方又发来的那些邮件」。这一组决定了整栏的可信度。
    unanswered = [m for m in ordered if (m.get("received_ts") or 0.0) > last_out]

    # 「最后一封是别人发的」**远不足以**说明有人在等我 —— 系统告警、通讯组通知、
    # 只抄送我的邮件，永远都不会有人回，于是每一封都满足这个条件，会把最重要的
    # 那一栏整个灌成噪音。（单测里这条一开始就是错的，5 个用例同时挂掉。）
    # 所以还要求两件事：
    #   1. 我确实在收件人栏里 —— to_me 为 False 时明确排除。
    #      **注意是 `is not False` 而不是 `is True`**：实测约 5% 的邮件读不到
    #      PR_MESSAGE_TO_ME（to_me 为 None），把「不知道」当成「不是发给我的」
    #      会静默藏掉真正需要回的邮件。漏报比多报一条糟得多。
    #   2. 发件人是个人，不是机器。
    # 两条都按「这一组里有任意一封符合」来判，不是只看最新那封：追问的最后一封
    # 常常是群发抄送，而最初提问的那封才是直接发给我的。
    addressed = any(m.get("to_me") is not False for m in unanswered)
    human = any(not is_auto_sender(m) for m in unanswered)
    waiting = bool(unanswered) and addressed and human

    # 等待时钟从**最早那封还没被回应的邮件**起算，不是从最新那封。
    #
    # 这是个容易搞错、而且错了就毁掉整个功能的地方：如果按最新一封算，一个人追问
    # 得越勤，显示的「等了几天」反而越小 —— 追了 3 封、压了 4 天的事会显示成
    # 「等了 1 天」，被排到真正不急的事后面。用户要的数字是「这件事搁了多久」。
    clock = min((m.get("received_ts") or 0.0) for m in unanswered) if unanswered else 0.0
    wait_s = max(0.0, now_ts - clock) if waiting else 0.0
    wait_days = int(wait_s // 86400)
    wait_hours = int(wait_s // 3600)

    people, seen = [], set()
    for m in ordered:
        nm = (m.get("sender_name") or "").strip() or (m.get("sender_addr") or "").strip()
        if nm and nm not in seen:
            seen.add(nm)
            people.append(nm)

    # 期限取整个会话里最紧的那一条 —— 期限往往出现在较早的那封里。
    deadline = None
    for m in ordered:
        d = m.get("deadline")
        if not d:
            continue
        if deadline is None:
            deadline = d
        elif d.get("date") and (not deadline.get("date") or d["date"] < deadline["date"]):
            deadline = d

    return {
        "conv_id": conv,
        "open_id": newest.get("id") or "",       # 「在 Outlook 里打开」用最新那封
        "subject": newest.get("subject") or "(无主题)",
        "people": people,
        "msg_count": len(ordered),
        # 「他追了几封」—— 只数我上次回话之后的，这才是催办的强度。
        "chase_count": len(unanswered),
        "ask": extract_ask(newest.get("subject") or "", newest.get("body") or ""),
        "waiting": waiting,
        "waiting_days": wait_days,
        "waiting_label": _wait_label(wait_days, wait_hours) if waiting else "",
        "last_received": newest.get("received") or "",
        "last_received_label": newest.get("received_label") or "",
        # 我上次在这个会话里回话是什么时候。有值 = 这不是新话题，是被追问。
        "replied_before": last_out > 0,
        "replied_days_ago": _days_between(now_ts, last_out) if last_out > 0 else None,
        "att_count": sum(int(m.get("att_count") or 0) for m in ordered),
        "high_importance": any(bool(m.get("high_importance")) for m in ordered),
        "flagged": any(bool(m.get("flagged")) for m in ordered),
        "is_meeting": any(bool(m.get("is_meeting")) for m in ordered),
        # 有催办词但抽不到日期时，卡片上还是要能标一下（它不进「到期在即」，
        # 但用户有权看到「这封在催」）。
        "urgent_word": next((m["deadline"].get("trigger") for m in ordered
                             if (m.get("deadline") or {}).get("trigger")
                             and not (m.get("deadline") or {}).get("date")), ""),
        "to_me": newest.get("to_me"),
        "cc_me": newest.get("cc_me"),
        "deadline": deadline,
        "body_preview": (newest.get("body") or "")[:260],
        "message_ids": [m.get("id") for m in ordered],
        # 归因（「为什么可以不看」）要看真实发件人地址 —— noreply@ / alert@ 这类
        # 只能从地址上认出来。build_view 用完就把这个键删掉，不进响应。
        "_newest": newest,
    }


def _why(t: dict) -> str:
    """这条为什么出现在它所在的那一栏。一句话，给人看的。

    必须有：一个说不出理由的清单，用户只会把它当成又一个黑箱（Focused Inbox 的
    毛病就在这），然后回去用 Outlook。
    另外这也堵住了一个渲染坑：靠「我标过旗」进 waiting 栏的条目 waiting 为假、
    waiting_label 是空串，前端要是无条件拼「等了 X」就会显示成「等了 」。
    """
    bits: list[str] = []
    d = t.get("deadline") or {}
    left = d.get("days_left")
    if isinstance(left, int):
        if left < 0:
            bits.append("已逾期 %d 天（%s）" % (-left, d.get("date") or ""))
        elif left == 0:
            bits.append("今天到期")
        else:
            bits.append("还剩 %d 天（%s）" % (left, d.get("date") or ""))
    if t.get("waiting") and t.get("waiting_label"):
        if t.get("chase_count", 0) > 1:
            bits.append("等了 %s，追了 %d 封" % (t["waiting_label"], t["chase_count"]))
        else:
            bits.append("等了 %s" % t["waiting_label"])
    if t.get("flagged"):
        bits.append("我在 Outlook 里标过旗")
    if t.get("is_meeting"):
        bits.append("会议邀请待回应")
    if t.get("urgent_word"):
        bits.append("正文里在催（「%s」）" % t["urgent_word"])
    if not bits and t.get("section") == "quiet":
        bits.append(QUIET_LABELS.get(t.get("quiet_kind") or "read", "无待办"))
    return " · ".join(bits)


def build_view(messages: list[dict], sent_map: dict[str, float], *,
               promises: list[dict] | None = None,
               now: datetime | None = None) -> dict:
    """邮件列表 → 欠账表。**纯函数**：真实取数和演示数据走的是同一条这里。

    区块之间**排他**，优先级：到期 > 有人在等 > 承诺 > 可以不看。
    排他是为了让每件事只出现一次（见模块头注「三条硬规矩」）。
    """
    now = now or _now()
    now_ts = now.timestamp()

    # 1) 收成会话
    groups: dict[str, list[dict]] = {}
    for m in messages:
        # conv_id 缺失时退回用 id 自成一组，绝不把不同的事并到同一个空键下。
        key = (m.get("conv_id") or "").strip() or ("id:" + str(m.get("id") or id(m)))
        groups.setdefault(key, []).append(m)

    threads = [_thread(v, sent_map=sent_map, now_ts=now_ts) for v in groups.values()]

    # 2) 分区。顺序即优先级，先摘走的不再进后面的区。
    due, waiting, quiet = [], [], []
    for t in threads:
        d = t.get("deadline") or {}
        left = d.get("days_left")
        # 注意判据是**抽到了真实日期**（days_left 是整数），而不是"有催办词"。
        # 只有催办词没有日期（「请尽快确认」）不足以顶到最上面 —— 那种通知太多了，
        # 顶上来会把真正有硬期限的事挤掉。它照常留在下面，卡片上仍有催办徽标。
        overdue = isinstance(left, int) and left < 0
        due_soon = isinstance(left, int) and 0 <= left <= _DUE_SOON_DAYS
        auto = is_auto_sender(t["_newest"])
        if (overdue or due_soon) and not auto:
            # 有硬日期就进这一区，**不要求「在等我」**。通讯组发来的带截止日的通知
            # 同样是你的责任，而它的 to_me 是 False —— 早先那版把 waiting 也当成
            # 必要条件，结果这类邮件会掉进「今天可以不看」，把真期限埋掉。
            # 机器发的除外：监控告警里带个日期不是你的期限。
            t["section"] = "due"
            due.append(t)
        elif t["waiting"] or t["flagged"]:
            # flagged = 用户自己在 Outlook 里标了旗。这是最强的显式信号，
            # 不能被「只是抄送我」之类的推断盖过去 —— 他已经说了这事他要管。
            t["section"] = "waiting"
            waiting.append(t)
        else:
            t["section"] = "quiet"
            t["quiet_kind"] = quiet_kind(t["_newest"], t["replied_before"])
            quiet.append(t)
        t["why"] = _why(t)

    due.sort(key=lambda t: (t["deadline"].get("days_left") if isinstance(
        t["deadline"].get("days_left"), int) else 99, -t["waiting_days"]))
    waiting.sort(key=lambda t: -t["waiting_days"])
    quiet.sort(key=lambda t: t.get("last_received") or "", reverse=True)

    # _newest 只是分区时用来做归因的中间态，别让它进响应体（里面挂着整封原始邮件）。
    for t in threads:
        t.pop("_newest", None)

    # 3) 承诺。已经在 due / waiting 里出现过的会话要摘掉 —— 那件事已经在上面了，
    #    再以「你答应过」的名义出现一次就是重复计数。
    taken = {t["conv_id"] for t in due} | {t["conv_id"] for t in waiting}
    prom = []
    for p in (promises or []):
        if p.get("conv_id") and p["conv_id"] in taken:
            continue
        q = dict(p)
        q["age_days"] = _days_between(now_ts, float(q.get("sent_ts") or now_ts))
        prom.append(q)
    prom.sort(key=lambda p: -p["age_days"])

    # 4) 「可以不看」的归因。给的是**为什么**，不是一堆卡片。
    breakdown: dict[str, int] = {}
    for t in quiet:
        k = t.get("quiet_kind") or "read"
        breakdown[k] = breakdown.get(k, 0) + 1

    people_waiting = {p for t in (due + waiting) for p in t["people"][:1]}
    max_wait = max([t["waiting_days"] for t in (due + waiting)] or [0])

    return {
        "summary": {
            "due": len(due),
            "overdue": sum(1 for t in due
                           if isinstance(t["deadline"].get("days_left"), int)
                           and t["deadline"]["days_left"] < 0),
            "waiting_threads": len(waiting),
            "waiting_people": len(people_waiting),
            "waiting_max_days": max_wait,
            "promises": len(prom),
            "quiet": len(quiet),
            "threads": len(threads),
            # 「一件都不欠」是个值得明确说出来的结论，不是空列表。
            "clear": not (due or waiting or prom),
        },
        "due": due,
        "waiting": waiting,
        "promises": prom,
        "quiet": {
            "count": len(quiet),
            "breakdown": [{"kind": k, "label": QUIET_LABELS.get(k, k), "count": v}
                          for k, v in sorted(breakdown.items(), key=lambda kv: -kv[1])],
            "threads": quiet,
        },
    }
