"""本地邮箱 —— 类型分类 + 信息矩阵的字段定义。**纯函数，不碰 COM，不调大模型。**

## 两件事，一个模块

「智能看板」要的是**类型标签**，「信息矩阵」要的是**表格字段**。两者其实是同一件事：
从一封邮件里派生出结构化属性。放一起，共用抽取器。

## 本地 / AI 的分界

类型这一维是**混合**的。有硬信号的四类本地判：

  会议安排  MessageClass 就是会议邀请 —— 最硬，连正文都不用读
  广告推广  正文里有退订入口 —— 真人写的邮件不会带
  系统告警  机器发件人 + 主题里有告警类词
  工单状态  机器发件人 + 主题里有工单类词

其余（决策请求 / 审阅确认 / 审批流转 / 索取信息 / 故障事故 / 商务合同 / 交付进度 /
人事财务 / 制度培训 / 订阅资讯）是语义判断，留空等 outlook_ai 填。
**留空而不是猜** —— 一个猜错的「决策请求」会让整块看板失去可信度，空标签只是少一条信息。

**AI 不可用时（没配 key / 调用失败 / mock 模式）本模块的输出照常成立**，
看板会多几个「未分类」但不会空白。这条不能破：一个必须联网才能开的本地邮箱页面
是失败的设计。

## 邮件正文是不可信输入

下面全是正则匹配 + 截断。抽出来的字符串只当数据显示，不解释、不执行、不拼进指令。
"""
from __future__ import annotations

import re

# is_auto_sender 在 outlook_view 里（那边判「有人在等我」时也要用它）。
# split_names 在 outlook_graph 里（关系网靠它切收件人栏；这里数收件人个数要用同一个
# 口径，各写一份迟早漂移）。两个模块都不 import 本模块，所以没有循环依赖。
from ..config import settings
from .outlook_graph import split_names
from .outlook_view import is_auto_sender

# ─────────────────────────────────────────────────────── 维度定义

# ## 为什么只剩「类型」一个维度
#
# 早先还有责任 / 紧急程度 / 重要性三个维度，都删了：
#
#   责任      —— 顶部那句结论已经说了「N 个人在等你回话 · M 件你答应过还没做」，
#                矩阵的「状态」列每行又写了一遍「等了 4 天，追了 3 封」。第三份拷贝。
#   紧急程度  —— 实测 15 个会话里 13 个落在「无期限」。87% 集中在一个桶里的分面
#                没有区分能力；真有期限的，矩阵「状态」列里写着「还剩 N 天（日期）」。
#   重要性    —— 拿几个弱信号凑出来的加权分（连我自己都在注释里标了"半可靠"）。
#                「中 9」这个格子做不了任何决定，是把噪音包装成指标。
#
# 剩下的这一个是 Outlook 真给不出来的东西：**这封邮件是件什么事**。
#
# ## 粒度：14 类 + 未分类
#
# 原来只有 5 类（决策/业务/行政/通知/广告），在真实收件箱上太粗 —— 7 天几百个
# 会话分成 5 堆，每堆几十个，跟没分一样。所以按「这封邮件要你做什么动作」切细。
#
# 顺序即优先级，前端按这个顺序排格子。前四类是"要你动手"，往后逐渐变成"知道就行"。
# 演示数据只有 15 个会话，14 个格子会显得稀 —— 那是样本量的问题，不是粒度的问题。
DIMENSIONS = [
    {"id": "kind", "label": "类型",
     "hint": "会议 / 告警 / 工单 / 广告由本机硬信号判定，其余由 AI 判定",
     "values": [
         # ── 要你动手 ──
         {"id": "decision", "label": "决策请求", "tone": "hot", "ai": True},
         {"id": "review", "label": "审阅确认", "tone": "hot", "ai": True},
         {"id": "approval", "label": "审批流转", "tone": "hot", "ai": True},
         {"id": "info_req", "label": "索取信息", "tone": "hot", "ai": True},
         # ── 业务往来 ──
         {"id": "incident", "label": "故障事故", "tone": "hot", "ai": True},
         {"id": "commercial", "label": "商务合同", "tone": "warm", "ai": True},
         {"id": "delivery", "label": "交付进度", "tone": "warm", "ai": True},
         {"id": "meeting", "label": "会议安排", "tone": "warm"},
         # ── 流程行政 ──
         {"id": "hr_fin", "label": "人事财务", "tone": "cool", "ai": True},
         {"id": "policy", "label": "制度培训", "tone": "cool", "ai": True},
         # ── 机器发的 ──
         {"id": "alert", "label": "系统告警", "tone": "warm"},
         {"id": "ticket", "label": "工单状态", "tone": "cool"},
         {"id": "newsletter", "label": "订阅资讯", "tone": "cool", "ai": True},
         {"id": "bulk", "label": "广告推广", "tone": "cool"},
         {"id": "", "label": "未分类", "tone": "cool"},
     ]},
]

# ─────────────────────────────────────────────────────── 广告 / 群发

# 只留**退订入口**这一类硬信号。真人写的邮件不会带退订链接。
#
# 「此邮件由系统自动发送」「请勿直接回复」曾经在这个表里，是错的：这两句在**系统
# 通知**里远比在营销邮件里常见。加进去之后，一封监控告警会被判成「广告群发」——
# 而告警和广告在用户眼里是两件完全不同的事（一个要看一眼，一个永远不看）。
# 那两句现在归 is_auto_sender 那条线管。
_BULK_MARKS = (
    "退订", "取消订阅", "unsubscribe", "opt out", "opt-out",
    "view in browser", "在浏览器中查看", "查看网页版",
)


def is_bulk(m: dict) -> bool:
    """群发/广告。宁可漏判，不可误判 —— 把一封真人邮件标成广告，用户会直接不信这栏。

    只看正文里的退订类标记，不看主题关键词（「优惠」「福利」这类词在正常业务
    邮件里太常见了，按主题判会把一堆真业务邮件标成广告）。
    """
    body = (m.get("body") or "").lower()
    if any(k in body for k in _BULK_MARKS):
        return True
    return False


# ─────────────────────────────────────────────────────── 单条维度

# 机器发的邮件里，告警和工单要分开：告警要瞟一眼，工单状态变更基本不用管。
# 用主题里的词区分 —— 这两类系统的主题格式都很固定。
_ALERT_WORDS = ("告警", "报警", "预警", "alert", "alarm", "critical", "warning",
                "超过", "阈值", "异常", "失败", "down", "unreachable", "使用率")
_TICKET_WORDS = ("工单", "ticket", "已受理", "已关闭", "已完成", "已提交", "待处理",
                 "状态变更", "流程通知", "incident", "request #", "req")


def local_kind(m: dict, *, is_auto: bool, is_meeting: bool = False) -> str:
    """类型维度的**本地那一半**。判不出就返回空串，留给 AI。

    返回空串是刻意的：猜一个「决策请求」比留空糟得多。

    只判有**硬信号**的四类：
      meeting  MessageClass 就是会议邀请 —— 最硬的信号，不需要读正文
      bulk     正文里有退订入口 —— 真人写的邮件不会带
      alert    机器发件人 + 主题里有告警类词
      ticket   机器发件人 + 主题里有工单类词
    机器发的但两类词都不命中 → 留空交给 AI（可能是订阅资讯、系统里发的制度通知…）。

    顺序重要：**先判机器发件人，再判退订标记**。很多告警平台和工单系统底部也带
    订阅设置链接，先判 bulk 会把告警全归成广告。发件人是机器这个信号更强也更稳。
    """
    if is_meeting:
        return "meeting"
    hay = ((m.get("subject") or "") + " " + (m.get("sender_name") or "")).lower()
    if is_auto:
        if any(w in hay for w in _ALERT_WORDS):
            return "alert"
        if any(w in hay for w in _TICKET_WORDS):
            return "ticket"
        return ""            # 机器发的但看不出是哪种 —— 交给 AI，别硬塞
    if is_bulk(m):
        return "bulk"
    return ""


# ─────────────────────────────────────────────────────── 信息矩阵：字段

# 「可扩展」的落点：字段就是**数据** —— 加一列 = 往 MATRIX_FIELDS 加一个 dict，
# 加一个徽标 = 往 BADGE_FIELDS 加一个 dict，都不用改代码。
#   kind="regex"    —— pattern 命中即取值
#   kind="derived"  —— 从已归一化的字段直接算（在 matrix_row 里赋值）
#   kind="ai"       —— 语义字段，由 outlook_ai 填；AI 不可用时保持空
#
# ## 列数的取舍：**每一列都要在多数行里有内容**
#
# 早先有 12 列，实测下来 对方机构 / 金额 / 单号 / 附件 整列都是「—」，而
# 截止日期 和「状态」列纯重复（状态里已经写着「还剩 5 天（2026-08-28）」）。
# 一列在多数行里是空的，就是在拿横向滚动换白纸 —— 表格的价值是能一眼扫完，
# 12 列扫不完。
#
# 所以稀疏的那几个不作为列，改成挂在主题格里的**行内徽标**（见 BADGE_FIELDS）：
# 它们的值都很短（12,400 / INC20260819007 / 2 个附件），有就显示，没有就不占位。
MATRIX_FIELDS = [
    {"id": "received", "label": "时间", "kind": "derived", "width": 92},
    # 「我跟这封是什么关系」。五档，见 addressing_of。它决定了这封该不该你管，
    # 而「群组邮件」那一档还决定了这一行**要不要出现在矩阵里**（见 in_working_views）。
    {"id": "addressing", "label": "收件方式", "kind": "derived", "width": 104},
    {"id": "people", "label": "对方", "kind": "derived", "width": 104},
    # 「状态」= 为什么这一行值得看（等了几天、追了几封、还剩几天到期、我标过旗）。
    # 这一列是矩阵能取代原先那排卡片的关键：卡片上最有价值的就是这句话。
    {"id": "why", "label": "状态", "kind": "derived", "width": 168},
    {"id": "subject", "label": "主题", "kind": "derived", "width": 280},
    {"id": "project", "label": "项目", "kind": "ai", "width": 118},
    # 待定结论合并进这一格（第二行、红色），不单独占一列 —— 它和「事项」讲的是
    # 同一封邮件的同一件事，分两列会让人来回对照。
    {"id": "matter", "label": "事项", "kind": "ai", "width": 300},
]

# 挂在主题格里的行内徽标。抽取方式和列一样（regex / derived），只是不占列。
# 「可扩展」的落点还在这里：加一个 dict 就多一个徽标。
BADGE_FIELDS = [
    {"id": "amount", "label": "金额", "kind": "regex",
     # 三种形态：带货币符号 / 带千分位 / 带中文单位。必须有其中之一 ——
     # 纯数字匹配会把编号、页码、楼层全抽成金额。
     "pattern": r"(?:(?:[¥$€£]|USD|RMB|CNY|人民币)\s?\d[\d,\.]*\s?(?:万|亿|k|K|M)?)"
                r"|(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?\s?(?:元|万元|美元|万)?)"
                r"|(?:\d[\d\.]*\s?(?:万元|亿元|万美元|元整))"},
    {"id": "ticket", "label": "单号", "kind": "regex",
     # [REQ202606110054] / MSS-353372 / CHG0012345 这类。要求字母+数字且数字够长。
     "pattern": r"\[?[A-Z]{2,6}[-_]?\d{4,}\]?"},
    {"id": "atts", "label": "附件", "kind": "derived"},
    {"id": "org", "label": "外部", "kind": "derived"},
]

_ALL_FIELDS = MATRIX_FIELDS + BADGE_FIELDS
_COMPILED = {f["id"]: re.compile(f["pattern"]) for f in _ALL_FIELDS if f.get("pattern")}

# 常见邮箱服务商的域名不算「对方机构」——那只说明这人用什么邮箱，不是他属于哪。
_PUBLIC_MAIL = {
    "gmail.com", "outlook.com", "hotmail.com", "live.com", "qq.com", "163.com",
    "126.com", "sina.com", "foxmail.com", "yahoo.com", "icloud.com", "aliyun.com",
    "example.com",
}


def _org_of(addr: str) -> str:
    if "@" not in addr:
        return ""
    dom = addr.rsplit("@", 1)[-1].strip().lower()
    if not dom or dom in _PUBLIC_MAIL:
        return ""
    return dom


def regex_cell(field_id: str, m: dict) -> str:
    """正则列取值。取**第一个**命中，多个命中时在后面标 +N。

    只在主题 + 正文前 1200 字里找：更靠后的内容多半是引用历史，抽出来的金额
    是上一轮讨论里的，放进表格会误导。
    """
    pat = _COMPILED.get(field_id)
    if pat is None:
        return ""
    hay = (m.get("subject") or "") + "\n" + (m.get("body") or "")[:1200]
    hits = []
    for mo in pat.finditer(hay):
        s = mo.group(0).strip().strip("[]")
        if s and s not in hits:
            hits.append(s)
        if len(hits) >= 4:
            break
    if not hits:
        return ""
    return hits[0] + (" +%d" % (len(hits) - 1) if len(hits) > 1 else "")


# 收件方式的取值。**顺序即「跟我的关系有多直接」**，前端按这个排色。
ADDR_ONLY_ME = "只发我"
ADDR_TO_ME = "直发我和其他人"
ADDR_CC_ME = "抄送我"
ADDR_GROUP = "群组邮件"
ADDR_UNKNOWN = "不确定"
ADDR_SENT = "我发出"


def addressing_of(t: dict, msgs: list[dict]) -> str:
    """我跟这封邮件是什么关系。

    五档，从最直接到最不直接：

      只发我          收件人栏里只有我一个 —— 这封百分百是要我处理的
      直发我和其他人   我在收件人栏，但不止我 —— 可能是我，也可能是别人先动手
      抄送我          我只在抄送里 —— 知会性质
      群组邮件        我的地址**既不在收件人也不在抄送**里，那就是通过通讯组/规则
                     投递进来的。这一档会被**排除出信息矩阵和时间图谱**（见下面）。
      不确定          读不到判定所需的属性

    「只发我 / 直发我和其他人」靠数收件人栏里有几个名字来分。用的是 outlook_graph
    的 split_names，和关系网同一个切分口径 —— 两处各写一份迟早会漂移。

    注意 to 字段在归一化时截断到 300 字符：这对计数无害，因为收件人多到会被截断时，
    个数一定已经大于 1，结论不变。

    ## 「不确定」为什么不能合并到「群组邮件」里

    实测约 1/3 的邮件读不到 PR_MESSAGE_TO_ME。而「群组邮件」这一档会被排除出矩阵和
    图谱 —— 把「读不到」当成「群组邮件」，就等于**静默删掉**那三分之一里真正需要我回
    的邮件，而页面上完全看不出发生了什么。所以不确定必须是独立的一档，并且照常进矩阵。
    只有**确定**我不在收件人栏时才排除。
    """
    if t.get("promise"):
        return ADDR_SENT
    if not msgs:
        return ""
    # 「我在收件人栏 / 只在抄送 / 都不在」这一层取**整个会话最直接的一档**：
    # 常见形态是「最初直接问我，后面的追问群发抄送一圈」，只看最新那封会判成抄送，
    # 而它其实是要我回的。
    #
    # 但「只发我 vs 直发我和其他人」这一层要看**最新那封我在收件人栏里的邮件** ——
    # 它反映这件事现在的形态。msgs 是按时间倒序的，所以第一个命中的就是最新那封。
    #
    # 早先这里写了「遇到只发我就立即返回」的早退，是错的：它会跳过最新那封去看更早
    # 的，于是一个会话只要历史上有过一封单发，就永远显示「只发我」——「直发我和其他
    # 人」这一档在真实数据里几乎永远不会出现（演示数据里就是 0）。
    for m in msgs:
        if m.get("to_me") is True:
            n = len(split_names(m.get("to") or "", cap=4))
            return ADDR_ONLY_ME if n <= 1 else ADDR_TO_ME
    if any(m.get("cc_me") is True for m in msgs):
        return ADDR_CC_ME
    # 全部邮件都**确定**两栏都没有我 → 通讯组/规则投递
    if all(m.get("to_me") is False and m.get("cc_me") is False for m in msgs):
        return ADDR_GROUP
    return ADDR_UNKNOWN


# 不进「信息矩阵 / 时间图谱」的类型。这两个视图是**工作台**：给「跟我有关、我可能
# 要动手」的事用。下面这三类不管怎么排都不需要我动手，混进去只会稀释信号：
#
#   bulk        广告推广 —— 永远不用管
#   newsletter  订阅资讯 —— 定期送达，看不看随意，没有动作
#   ticket      工单状态 —— 「已受理」「已关闭」这种状态流水，同一件事反复通知，
#               是典型的「重复的无价值邮件」
#
# **系统告警（alert）刻意不在这个名单里**：磁盘满了、证书要过期是真需要处置的，
# 它只是发件人是机器而不是人。真要屏蔽，通常它已经因为「群组邮件」被排掉了。
#
# 这个名单从配置读（OUTLOOK_SKIP_KINDS，逗号分隔），不写死 —— 哪些类别算噪音
# 因人而异，改一个环境变量就能调，不用改代码。
_DEFAULT_SKIP_KINDS = ("bulk", "newsletter", "ticket")


def skip_kinds() -> set[str]:
    raw = getattr(settings, "outlook_skip_kinds", None)
    if raw is None:
        return set(_DEFAULT_SKIP_KINDS)
    return {k.strip() for k in str(raw).split(",") if k.strip()}


def in_working_views(addressing: str, kind: str = "") -> bool:
    """这个会话该不该进「信息矩阵」和「时间图谱」。

    两条排除规则：

    1. **群组邮件** —— 我的地址既不在收件人也不在抄送里，是通讯组/规则投递进来的。
       判据必须是「**确定**不在」，不是「读不到」：实测约 1/3 的邮件读不到
       PR_MESSAGE_TO_ME，把读不到也排掉就是静默删除可能要回的邮件（见 addressing_of）。
    2. **噪音类型** —— 广告 / 订阅资讯 / 工单状态流水（见 _DEFAULT_SKIP_KINDS）。

    两类都**照常留在智能看板里**。看板要给全貌 ——「这周有 20 封广告」本身是有用的
    信息（它告诉你噪音有多少），只是不该占据工作视图。
    """
    if addressing == ADDR_GROUP:
        return False
    if kind and kind in skip_kinds():
        return False
    return True


def _stamp(iso: str) -> str:
    """列表用的时间戳，**始终带日期**：``2026-08-24T23:53:00`` → ``08-24 23:53``。

    直接切 ISO 串而不解析 datetime：省一次解析，也不用在这个模块引入时区概念
    （上游给的已经是本地时间）。
    """
    if not iso or len(iso) < 16:
        return ""
    return iso[5:16].replace("T", " ")


def matrix_row(t: dict, msgs: list[dict]) -> dict:
    """一个会话 → 矩阵的一行。

    为什么按会话而不按单封：矩阵是给人扫的，同一件事 5 行只会让人数不清有几件事。
    正则抽取在会话内**所有**邮件上跑，取第一个非空 —— 金额往往只在最初那封里出现。

    返回的 badges 是挂在主题格里的行内标记（金额 / 单号 / 附件 / 外部），不占列。
    见 MATRIX_FIELDS 上面关于列数取舍的注释。
    """
    newest = msgs[0] if msgs else {}
    cells: dict[str, str] = {}
    for f in _ALL_FIELDS:
        fid = f["id"]
        if f["kind"] == "regex":
            val = ""
            for m in msgs:
                val = regex_cell(fid, m)
                if val:
                    break
            cells[fid] = val
        elif f["kind"] == "ai":
            cells[fid] = ""          # 由 outlook_ai 覆盖；不可用则保持空

    # 时间**始终带日期**。原先用 last_received_label（short_dt：今天只给时分），
    # 于是表格里「08-25 00:30」下面紧跟一个「23:53」，看起来像时间倒回去了 ——
    # 实际顺序是对的，只是日期被藏了。表格是拿来扫的，不能让人猜是哪天。
    cells["received"] = _stamp(t.get("last_received") or "")
    # tag_threads 已经算好挂在会话上了，这里不重算 —— 两处各算一次早晚会不一致。
    cells["addressing"] = t.get("addressing") or addressing_of(t, msgs)
    cells["why"] = t.get("why") or ""
    cells["people"] = "、".join(t.get("people") or [])
    cells["subject"] = t.get("subject") or ""
    # 外部机构只在真是外部域名时才有值，所以做成徽标而不是一列 —— 内部邮件占
    # 大多数，做成列的话整列都是同一个公司域名，等于白占一列宽度。
    cells["org"] = _org_of(newest.get("sender_addr") or "")

    atts: list[str] = []
    for m in msgs:
        for a in (m.get("attachments") or []):
            if a.get("name") and a["name"] not in atts:
                atts.append(a["name"])
    # 徽标里只放数量，文件名放 tooltip —— 文件名很长，塞进表格会把行撑开。
    cells["atts"] = ("%d 个" % len(atts)) if atts else ""

    # 「我答应过」那类是伪会话（来源是我自己的已发送邮件，不是收件箱），没有收件
    # 时间和附件。它必须出现在矩阵里 —— 矩阵是唯一的列表，按「我答应过」筛出一张
    # 空表等于这个功能又消失了。填它有的：我发出的时间和原话。
    prom = t.get("promise")
    if prom:
        cells["received"] = "%s · 我发出" % (_stamp(t.get("last_received") or "")
                                            or "%s 天前" % prom.get("age_days"))
        cells["matter"] = prom.get("text") or ""

    badges = [{"id": f["id"], "label": f["label"], "value": cells.get(f["id"]) or ""}
              for f in BADGE_FIELDS if cells.get(f["id"])]
    return {"conv_id": t.get("conv_id") or "",
            "open_id": "" if prom else (t.get("open_id") or ""),
            # 排序键：ISO 串直接字典序比较即可（同一时区、同一格式）。
            # 矩阵是表格，用户期望「最近的在最上面」；看板那三段各有自己的
            # 排序口径（到期近的 / 等得久的），两者刻意不同。
            "sort_key": t.get("last_received") or "",
            "cells": cells, "badges": badges,
            "att_names": "、".join(atts[:6])}


# ─────────────────────────────────────────────────────── 组装

def tag_threads(threads: list[dict], msgs_by_conv: dict[str, list[dict]],
                *, promised_convs: set[str]) -> None:
    """给每个会话打类型标签。**原地修改**，写入 t["tags"]。

    promised_convs 现在不参与打标（「我答应过」不再是一个维度），但保留在签名里：
    调用方本来就有这个集合，而将来若要给承诺行加个专属标记，这里是唯一的落点。
    """
    for t in threads:
        conv = t.get("conv_id") or ""
        msgs = msgs_by_conv.get(conv) or []
        newest = msgs[0] if msgs else {}

        # **不要**用 t["quiet_kind"] == "auto" 来判：quiet_kind 只在会话落进
        # 「今天可以不看」时才有值。一封带硬日期的告警会进「到期在即」，那时
        # quiet_kind 是 None，机器发件人这个事实就丢了，类型会显示成未分类
        # 并且白白发去问模型。直接问发件人本身，跟分区无关。
        kind = local_kind(newest, is_auto=is_auto_sender(newest),
                          is_meeting=bool(t.get("is_meeting")))
        # 收件方式挂在会话上，不只在矩阵行里算：矩阵和时间图谱都要在**建行/挑候选
        # 之前**就知道该不该排除这个会话（群组邮件不进那两个视图）。
        t["addressing"] = addressing_of(t, msgs)
        # 这里只能用**本地**判出的 kind（AI 还没跑）。AI 写回之后
        # apply_exclusions() 会重跑一遍，把被判成「订阅资讯」那类也摘掉。
        t["in_working_views"] = in_working_views(t["addressing"], kind)
        t["tags"] = {
            # 本地判不出就留空，等 outlook_ai 填。看板上显示「未分类」。
            "kind": kind,
            # 标一下哪些是 AI 填的，前端要能让用户知道哪些结论来自模型。
            "kind_from": "local" if kind else "",
        }


def apply_exclusions(threads: list[dict], matrix: dict, graph: dict) -> dict:
    """按 in_working_views 把矩阵行和图谱候选筛一遍。**原地修改**，返回排除计数。

    ## 为什么必须在 AI 之后再跑一次

    排除规则要看 kind，而 kind 有两个来源、两个时刻：
      · 本地硬信号（广告 / 工单 / 会议 / 告警）—— 打标时就有
      · AI 语义（订阅资讯 等）—— 要等模型返回才有
    _assemble_views 里只能用本地那一半，所以一封被 AI 判成「订阅资讯」的邮件那时
    还留在矩阵里。这个函数在 AI 写回之后重跑一遍，把它摘掉。

    分开报 group / kind 两个原因的计数：界面上要说清「少的那些行是因为不是发给我的」
    还是「因为是噪音类别」，混成一个数字用户没法判断规则合不合他的意。
    """
    keep: set[str] = set()
    n_group = n_kind = 0
    # 再按 kind 分别记一份。用途：在智能看板里点某个类型、矩阵却是空的时候，
    # 页面要能说出「这一类的 13 封里 9 封是群组邮件、4 封属于噪音类型」——
    # 只给总数说不清是哪条规则在起作用，用户就没法判断规则合不合他的意。
    # 空表配一句解释和空表配一片空白，是两个完全不同的产品。
    by_kind: dict[str, dict[str, int]] = {}
    for t in threads:
        addressing = t.get("addressing") or ""
        kind = (t.get("tags") or {}).get("kind") or ""
        ok = in_working_views(addressing, kind)
        t["in_working_views"] = ok
        slot = by_kind.setdefault(kind, {"kept": 0, "group": 0, "kind": 0})
        if ok:
            keep.add(t.get("conv_id") or "")
            slot["kept"] += 1
        elif addressing == ADDR_GROUP:
            # 群组邮件优先计入 group：这条规则在 in_working_views 里先判，
            # 一封既是群组邮件又是广告时，真正起作用的是 group。
            n_group += 1
            slot["group"] += 1
        else:
            n_kind += 1
            slot["kind"] += 1

    matrix["rows"] = [r for r in matrix.get("rows") or []
                      if (r.get("conv_id") or "") in keep]
    graph["projects"] = [p for p in graph.get("projects") or []
                         if (p.get("conv_id") or "") in keep]
    graph["graphs"] = {k: v for k, v in (graph.get("graphs") or {}).items() if k in keep}

    counts = {"group": n_group, "kind": n_kind, "total": n_group + n_kind,
              "skip_kinds": sorted(skip_kinds()), "by_kind": by_kind}
    matrix["excluded"] = counts
    graph["excluded"] = counts
    return counts


def dimension_counts(threads: list[dict], *, samples: int = 3) -> dict:
    """分面计数。**只统计进了信息矩阵的会话**（in_working_views）。

    为什么不统计全量：看板的格子是**筛选器**，点一下就去筛矩阵。要是看板按全量
    计数、矩阵按工作视图筛，就会出现「广告推广 12」点下去矩阵空空的情况 ——
    用户只会觉得筛选坏了。两边口径必须一致。

    代价是群组邮件、广告、订阅资讯、工单流水不再出现在看板里。它们的数量改由
    「已挡掉 N 个」那一行报出来（apply_exclusions 的 by_kind），信息没丢，
    只是不再假装它们是可点的筛选项。

    **必须在 apply_exclusions 之后调用** —— in_working_views 是它写上去的。
    """
    """每个维度每个取值：会话数 + 几条样本主题。

    为什么要带样本：看板照的是「文档地图」那套分面格子的格式 —— 每格除了数字还列
    2~3 条里面的东西，用户**不用点进去**就知道这个桶里装的是什么。
    只给计数的话，「业务类 4」这种标签除了数字什么信息都没有，得靠猜和试。

    样本取每个桶里排在最前的几条（threads 进来时已经按分区优先级排好），
    所以看到的是这个桶里最该看的那几条，不是随机的。
    """
    # 只留进了工作视图的（群组邮件、广告、订阅资讯、工单流水都被 apply_exclusions
    # 标成 False）。in_working_views 还没被写上时（None）保守当作要保留。
    threads = [t for t in threads if t.get("in_working_views") is not False]
    out: dict[str, dict[str, dict]] = {}
    for dim in DIMENSIONS:
        did = dim["id"]
        buckets: dict[str, dict] = {}
        for t in threads:
            v = (t.get("tags") or {}).get(did) or ""
            b = buckets.setdefault(v, {"count": 0, "samples": []})
            b["count"] += 1
            if len(b["samples"]) < samples:
                b["samples"].append(t.get("subject") or "(无主题)")
        out[did] = buckets
    return out
