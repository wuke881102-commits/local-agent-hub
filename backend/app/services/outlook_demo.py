"""本地邮箱 —— 演示数据。**全部是编造的假邮件，不读任何真实邮箱。**

## 为什么需要这个

这个功能的目标环境是用户自己的笔记本（缓存模式、Outlook 正常）。而开发机上的
邮箱是在线模式，任何跟发件人有关的属性读取都会撞上 Outlook 的
Object Model Guard 模态框（"A program is trying to access email address
information"）而无限期挂住 —— 那个框在 Windows Server 上**每次都会弹**，因为
Server SKU 没有 root\\SecurityCenter2 命名空间，Outlook 判定不出「杀毒正常」。

所以在这台机器上没法演示界面。这个模块给一条**根本不碰 COM** 的路径：
形状和真实响应完全一致，前端一行都不用改就能渲染。

## 关键约定：走真逻辑，不写死结果

假数据只提供「原始字段」（主题、正文、to_me、conv_id…），然后交给**真正的**
deadline_hint() 和 triage() 去算分类和理由。这样演示里看到的分类行为就是上线后
的分类行为；如果规则有毛病，演示里也会照样露出来。
反过来说：**绝不能**在这里手写 bucket / reasons —— 那样演示就变成一张好看的
假截图，失去全部意义。

## 数据本身

- 地址一律用 @example.com（RFC 2606 保留域，永远不可能是真实地址）。
- 人名、部门名全是通用占位，不对应任何真实的人、客户或公司。
- 时间以「今天」为锚点动态生成，不写死日期 —— 否则过几个月演示里全是陈年旧邮件，
  而且 deadline_hint 只认今天及以后的日期，写死的截止日会被它正确地拒掉，
  「还剩 N 天」这条分支就永远演示不出来。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .outlook import (BUCKETS, _now, _parse_day, clean_body, deadline_hint,
                      short_dt, triage)
from .outlook_view import build_view, promise_hint

# 覆盖面清单 —— 每条假邮件对应 triage() 的一个分支或前端的一个渲染分支。
# 改数据前先看这里，别把某个分支的演示弄丢了。
#
#   1  need_reply   直接发给我、未回复            + 单附件
#   2  need_reply   同上 + 未来截止日期 + 高重要性 → 「还剩 N 天」
#   3  need_action  会议邀请
#   4  need_action  抄送我 + 我加过标记            → 两条理由叠加
#   5  need_action  有催办词但抽不到日期 + 通讯组投递
#   6  need_reply   MAPI 属性读不到 → 「无法确认我是否在收件人栏」
#   7  fyi          这个会话我已经回过了
#   8  fyi          只是抄送我                    + 多附件（KB / MB 两种量级）
#   9  fyi          收件人栏里没有我（系统告警）
#  10  fyi          发件人只有 X500 地址，解析不出 SMTP
#  11  need_reply   正文带引用历史 → body_truncated
#  12  fyi          超长主题（换行 / 截断的渲染）


def _mk(*, n: int, subject: str, sender: str, addr: str, when: datetime,
        body: str, to_me, cc_me, me_source: str, conv: str,
        meeting: bool = False, flagged: bool = False, high: bool = False,
        unread: bool = False, atts: list[dict] | None = None,
        addr_source: str = "PR_SENDER_SMTP_ADDRESS",
        to: str = "我", cc: str = "") -> dict:
    """拼一条和 norm_message() 输出完全同形的记录。

    正文过**真的** clean_body —— 引用切割和硬截断的行为要和线上一致。
    """
    text, truncated = clean_body(body)
    atts = atts or []
    return {
        "id": "demo-%02d" % n,
        "class": "IPM.Schedule.Meeting.Request" if meeting else "IPM.Note",
        "is_meeting": meeting,
        "subject": subject,
        "sender_name": sender,
        "sender_addr": addr,
        "addr_source": addr_source,
        "received": when.isoformat(),
        "received_ts": when.timestamp(),
        "received_label": short_dt(when),
        "conv_id": conv,
        "conv_topic": subject,
        "to_me": to_me,
        "cc_me": cc_me,
        "me_source": me_source,
        # 收件人/抄送显示名。关系网图谱唯一的依据 —— 谁和谁在同一封邮件里出现过。
        "to": to,
        "cc": cc,
        "unread": unread,
        "flagged": flagged,
        "high_importance": high,
        "attachments": atts,
        "att_count": len(atts),
        "body": text,
        "body_truncated": truncated,
    }


def _messages() -> tuple[list[dict], dict[str, float]]:
    now = _now()

    def at(days_ago: int, hh: int, mm: int) -> datetime:
        d = now - timedelta(days=days_ago)
        return d.replace(hour=hh, minute=mm, second=0, microsecond=0)

    # 未来的截止日 —— deadline_hint 只认今天及以后，所以必须动态算。
    due = (now + timedelta(days=5)).strftime("%Y-%m-%d")

    # 「新写两句 + 一大段引用历史」是真实邮件里最常见的形态，也是 clean_body
    # 最该证明自己的地方：切在引用分隔线上，而不是硬截 4000 字。
    quoted = (
        "见附件，麻烦确认一下第 3 页的口径。\n"
        "发件人: 王芳 <wangfang@example.com>\n"
        "发送时间: 上周五\n"
        "主题: 转发: 月度数据对齐\n\n"
        + "这里是被引用的历史往来内容，真实邮件里这一段通常有几千字。" * 60
    )

    msgs = [
        # 同一会话 3 封 —— 演示「一件事一行」的折叠，以及「等了 4 天还在追问」。
        # 这是 Outlook 给不出来的视图：它只会给你 3 张散装卡片。
        _mk(n=1, subject="合同附件里的金额和系统里对不上，麻烦你核一下",
            sender="张伟", addr="zhangwei@example.com", when=at(4, 9, 12),
            body="核了一下上个月的三笔，系统里合计比合同附件少 12,400。"
                 "是不是有一笔走了单独的结算流程？你那边能查到吗？",
            to_me=True, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-01", unread=True,
            # 收件人栏里除了我还有别人 → 「直发我和其他人」
            to="我；周涛；赵敏", cc="陈静",
            atts=[{"name": "结算明细-本月.xlsx", "ext": "xlsx", "size": 291142}]),

        _mk(n=13, subject="回复: 合同附件里的金额和系统里对不上，麻烦你核一下",
            sender="张伟", addr="zhangwei@example.com", when=at(2, 10, 3),
            body="上面那个问题还没结论，我这边报表卡着出不了。",
            to_me=True, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-01", unread=True, to="我；周涛；赵敏", cc="陈静"),

        _mk(n=14, subject="回复: 合同附件里的金额和系统里对不上，麻烦你核一下",
            sender="张伟", addr="zhangwei@example.com", when=at(1, 9, 30),
            body="今天能给个结论吗？不行的话我先按合同的数出，回头再调。",
            to_me=True, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-01", unread=True, to="我；周涛；赵敏", cc="陈静"),

        _mk(n=2, subject="请于 %s 前确认季度预算口径" % due,
            sender="刘洋", addr="liuyang@example.com", when=at(0, 8, 40),
            body="预算模板已更新。请于 %s 前把你负责的两条线的口径确认回来，"
                 "之后要合并进季度汇总，晚了就赶不上评审。" % due,
            to_me=True, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-02", high=True, unread=True,
            # 收件人栏只有我 → 「只发我」，最强的一档
            to="我"),

        _mk(n=3, subject="架构评审（演示）", sender="陈静",
            addr="chenjing@example.com", when=at(0, 8, 5),
            body="时间：明天上午 10:00-11:00。地点：三号会议室 / 线上同步。"
                 "议题：接口拆分方案的两个备选。",
            to_me=True, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-03", meeting=True, unread=True),

        _mk(n=4, subject="供应商准入材料补充清单",
            sender="赵敏", addr="zhaomin@example.com", when=at(1, 16, 22),
            body="附件是缺件清单，涉及你那边两家。原件扫描件交到合规就行。",
            to_me=False, cc_me=True, me_source="PR_MESSAGE_CC_ME",
            conv="demo-conv-04", flagged=True,
            atts=[{"name": "准入缺件清单.docx", "ext": "docx", "size": 47803}]),

        _mk(n=5, subject="[通知] 测试环境迁移，请尽快确认你的应用清单",
            sender="运维值班", addr="ops-notice@example.com", when=at(1, 11, 48),
            body="测试环境将迁到新机房。请各应用负责人尽快在表格里确认自己的实例清单，"
                 "未确认的按停用处理。",
            to_me=False, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-05", to="全体应用负责人"),

        # 这条会话有 3 封来信，而且**中间我回过一次**（见下面的 sent_map）——
        # 时间轴上要能看到「我」那一步，以及我回完之后又被追了两次。
        _mk(n=15, subject="数据字典对齐的几个字段",
            sender="孙鹏", addr="sunpeng@example.com", when=at(6, 11, 20),
            body="附件是我们这边的字段定义，麻烦你对一下有没有出入。",
            to_me=True, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-06", to="我；周涛", cc="陈静",
            atts=[{"name": "字段定义_我方.xlsx", "ext": "xlsx", "size": 73402}]),

        _mk(n=6, subject="回复: 数据字典对齐的几个字段",
            sender="孙鹏", addr="sunpeng@example.com", when=at(2, 14, 30),
            body="第 2 和第 5 个字段我们这边的定义不一样，你看按哪个口径走？",
            to_me=None, cc_me=None, me_source="unavailable",
            conv="demo-conv-06", to="我", cc="陈静；周涛"),

        _mk(n=16, subject="回复: 数据字典对齐的几个字段",
            sender="孙鹏", addr="sunpeng@example.com", when=at(1, 10, 5),
            body="上面那两个字段还等你定，我们这边建表卡着。",
            to_me=True, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-06", to="我；周涛", cc="陈静；刘洋"),

        _mk(n=7, subject="回复: 月度报表口径调整",
            sender="王芳", addr="wangfang@example.com", when=at(2, 10, 15),
            body="收到，那就按新口径出这个月的。",
            to_me=True, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-replied"),

        _mk(n=8, subject="季度培训材料（含录屏）",
            sender="人力发展", addr="learning@example.com", when=at(3, 15, 2),
            body="材料和录屏都在附件，自行安排时间看完即可，不用回复。",
            to_me=False, cc_me=True, me_source="PR_MESSAGE_CC_ME",
            conv="demo-conv-08",
            atts=[{"name": "培训手册.pdf", "ext": "pdf", "size": 3884120},
                  {"name": "课程录屏.mp4", "ext": "mp4", "size": 128450331},
                  {"name": "签到表.xlsx", "ext": "xlsx", "size": 18402}]),

        _mk(n=9, subject="[监控] 应用节点磁盘使用率 85%",
            sender="监控告警", addr="alert@example.com", when=at(3, 3, 17),
            # 带一个单号：矩阵的「单号 / 编号」列需要有东西才看得出这列在干什么。
            body="工单 INC20260819007：节点 app-07 磁盘使用率 85%，超过警戒线。"
                 "此邮件由系统自动发出，请勿直接回复。",
            to_me=False, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-09", to="运维值班组"),

        _mk(n=10, subject="转发: 外部合作方的接口文档",
            sender="李强", addr="", when=at(4, 17, 40),
            body="对方发来的文档，先转你看看有没有明显问题。",
            to_me=False, cc_me=True, me_source="PR_MESSAGE_CC_ME",
            conv="demo-conv-10", addr_source="x500_only",
            atts=[{"name": "接口说明_v3.pdf", "ext": "pdf", "size": 812004}]),

        _mk(n=11, subject="转发: 月度数据对齐",
            sender="周涛", addr="zhoutao@example.com", when=at(4, 9, 55),
            body=quoted,
            to_me=True, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-11",
            atts=[{"name": "数据对齐_汇总.xlsx", "ext": "xlsx", "size": 654221}]),

        # 真营销邮件：正文里有**退订入口**，这是「广告群发」唯一认的硬信号。
        # （对照组是上面那封监控告警：它写了「请勿直接回复」但没有退订链接，
        #  应该判成系统通知而不是广告 —— 这两者在用户眼里是完全不同的事。）
        _mk(n=17, subject="【限时】年度企业协作套件升级方案，本月下单立减",
            sender="云协作市场部", addr="marketing@example.com", when=at(5, 13, 44),
            body="新版协作套件已上线，支持更大团队规模。本月下单可享折扣。"
                 "如不想再收到此类邮件，请点击此处退订。",
            to_me=False, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-13", to="订阅用户"),

        # 两封**直接发给我**的噪音 —— 用来证明「按类型排除」这条规则真的在工作。
        # 演示里其他的广告/工单恰好也是群组邮件，会先被那条规则吃掉，
        # 于是类型排除永远是 0，看不出它有没有生效。营销邮件把收件人写成本人
        # 是很常见的形态，工单系统的状态通知也一样。
        _mk(n=18, subject="行业周报 第 34 期：仓储自动化的三个新动向",
            sender="产业观察周报", addr="weekly@example.com", when=at(3, 7, 30),
            body="本期看点：三个新动向。往期回顾请见网页版。如需停止接收，请点此退订。",
            to_me=True, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-14", to="我"),

        _mk(n=19, subject="工单 REQ20260821334 已关闭",
            sender="服务台", addr="servicedesk-noreply@example.com", when=at(2, 16, 8),
            body="您提交的工单已关闭。如仍有问题请重新提交。此邮件由系统自动发出。",
            to_me=True, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-15", to="我"),

        _mk(n=12, subject="关于进一步规范内部系统账号申请、变更与回收流程"
                          "并同步更新相关审批表单模板的通知（第二版）",
            sender="信息安全", addr="infosec@example.com", when=at(4, 8, 1),
            body="流程和表单模板都有更新，原版本本月底停用。具体见正文附表。",
            to_me=False, cc_me=False, me_source="PR_MESSAGE_TO_ME",
            conv="demo-conv-12", to="全体员工"),
    ]

    # 「这个会话我回过没有」的假已发送索引：只有 demo-conv-replied 有一条更晚的回信。
    replied = next(m for m in msgs if m["conv_id"] == "demo-conv-replied")
    sent_map = {
        "demo-conv-replied": replied["received_ts"] + 3600,
        # 数据字典那条：我在 4 天前回过一次，之后又被追了两封 —— 演示
        # 「等待时钟从我回话之后那封起算」，以及时间轴上「我」的那一步。
        "demo-conv-06": (now - timedelta(days=4)).timestamp(),
    }
    return msgs, sent_map


def _promises(now: datetime) -> list[dict]:
    """假的「我答应过但还没做」。

    刻意也走**真的** promise_hint()：这里给的是我编的已发送邮件正文，承诺原话由
    抽取器自己找出来。所以演示里出现的每一条，都证明抽取器在这种措辞上确实有效 ——
    而如果它抽不出来，这一栏就会空着，把问题暴露出来，而不是拿写死的文案糊过去。

    第 3 条的会话故意和收件箱里 demo-conv-01 撞上：那件事已经在「有人在等你」里了，
    build_view 必须把它从承诺栏摘掉，不能同一件事数两遍。
    """
    raw = [
        {"conv_id": "demo-sent-a", "to": "王芳",
         "subject": "回复: 月度数据对齐",
         "body": "收到。我明天把两条线的口径整理好发你，你先按旧的出。",
         "days_ago": 6},
        {"conv_id": "demo-sent-b", "to": "孙鹏；陈静",
         "subject": "回复: 接口拆分方案",
         "body": "方案我看过了，有两处要跟安全确认。我确认后回复你们。",
         "days_ago": 3},
        {"conv_id": "demo-conv-01", "to": "张伟",
         "subject": "回复: 合同附件里的金额和系统里对不上",
         "body": "我查一下结算流水再告诉你。",
         "days_ago": 5},
        # 负例：这条不是承诺，抽取器必须抽不出来，因此不该出现在页面上。
        {"conv_id": "demo-sent-d", "to": "赵敏",
         "subject": "回复: 供应商准入",
         "body": "这个我不清楚，你问一下合规那边。",
         "days_ago": 2},
    ]
    out = []
    for r in raw:
        text = promise_hint(r["body"])
        if not text:
            continue
        out.append({
            "conv_id": r["conv_id"], "to": r["to"], "subject": r["subject"],
            "text": text,
            "sent_ts": (now - timedelta(days=r["days_ago"])).timestamp(),
        })
    return out


# 演示模式下「AI 那三列」的内容。键是 conv_id。
# **这是我编的，不是模型输出**。真实路径下这些字段来自 outlook_ai（云端模型）。
_FAKE_AI = {
    "demo-conv-01": {"kind": "review", "project": "月度结算",
                     "matter": "结算金额与合同附件差 12,400，待定原因",
                     "decision": "是否认可单独结算的那一笔"},
    "demo-conv-02": {"kind": "review", "project": "季度预算",
                     "matter": "要在截止日前确认两条线的口径", "decision": "两条线各按哪个口径"},
    "demo-conv-03": {"kind": "meeting", "project": "接口拆分",
                     "matter": "评审会要在两个备选方案里定一个", "decision": "选方案 A 还是 B"},
    "demo-conv-04": {"kind": "info_req", "project": "供应商准入",
                     "matter": "两家供应商的准入材料缺件待补", "decision": ""},
    "demo-conv-05": {"kind": "info_req", "project": "测试环境迁移",
                     "matter": "各应用负责人需确认实例清单", "decision": ""},
    "demo-conv-06": {"kind": "decision", "project": "数据字典",
                     "matter": "两个字段双方定义不一致", "decision": "第 2、5 字段按谁的口径"},
    "demo-conv-replied": {"kind": "review", "project": "月度报表",
                          "matter": "口径调整已确认，按新口径出表", "decision": ""},
    "demo-conv-08": {"kind": "policy", "project": "季度培训",
                     "matter": "培训材料与录屏已发布，无需回复", "decision": ""},
    "demo-conv-09": {"kind": "alert", "project": "节点巡检",
                     "matter": "app-07 磁盘使用率超警戒线", "decision": ""},
    "demo-conv-10": {"kind": "review", "project": "外部接口",
                     "matter": "合作方接口文档待初审", "decision": ""},
    "demo-conv-11": {"kind": "review", "project": "月度数据对齐",
                     "matter": "需确认汇总表第 3 页口径", "decision": ""},
    "demo-conv-12": {"kind": "policy", "project": "账号管理",
                     "matter": "账号申请流程与表单模板更新", "decision": ""},
    "demo-conv-14": {"kind": "newsletter", "project": "行业周报",
                     "matter": "订阅的周报，无需处理", "decision": ""},
    "demo-conv-15": {"kind": "ticket", "project": "服务台工单",
                     "matter": "我提交的工单已关闭", "decision": ""},
    # 这条本地就判出 bulk 了（正文有退订入口），所以 kind 不会被覆盖。
    # 留着 project/matter 是为了让矩阵那两列也有内容。
    "demo-conv-13": {"kind": "bulk", "project": "供应商推广",
                     "matter": "第三方协作套件促销，无需处理", "decision": ""},
}


def _apply_fake_ai(threads: list[dict], rows: list[dict]) -> None:
    """把编好的语义字段写进会话和矩阵行。形状和 outlook_ai.apply_to 一致。"""
    for t in threads:
        got = _FAKE_AI.get(t.get("conv_id") or "")
        if not got:
            continue
        tags = t.setdefault("tags", {})
        if not tags.get("kind"):
            tags["kind"] = got["kind"]
            tags["kind_from"] = "demo"
        t["ai"] = got
    for r in rows:
        got = _FAKE_AI.get(r.get("conv_id") or "")
        if not got:
            continue
        for fid in ("project", "matter", "decision"):
            if got.get(fid):
                r["cells"][fid] = got[fid]


def inbox_demo(*, since_day: str = "", until_day: str = "") -> dict:
    """和 _inbox_sync() 同形的响应，但一次 COM 调用都不发。

    步骤顺序刻意和真实路径一致：先算 deadline，再 triage —— triage 会读 deadline。

    **必须支持 since/until。** 演示模式不过滤日期的话，页面上的「当天 / 近 3 天 /
    近 7 天」三个按钮点下去数据一模一样 —— 用户会以为选择器是坏的，而其实是演示
    路径没实现。假数据也得跟真实路径一样响应参数，否则演示就在说谎。
    """
    t0 = _now()
    msgs, sent_map = _messages()
    msgs.sort(key=lambda m: m["received_ts"], reverse=True)

    since = _parse_day(since_day, end=False)
    until = _parse_day(until_day, end=True)
    if since is not None:
        msgs = [m for m in msgs if m["received_ts"] >= since.timestamp()]
    if until is not None:
        msgs = [m for m in msgs if m["received_ts"] <= until.timestamp()]

    for m in msgs:
        m["deadline"] = deadline_hint(m["subject"], m.get("body") or "")
    counts = triage(msgs, sent_map)
    # 和真实路径同一个调用、同一套规则。演示里看到的分区就是线上的分区。
    view = build_view(msgs, sent_map, promises=_promises(t0), now=t0)

    # 三个视图也走真实路径的同一个装配函数。sent_log 造一条：demo-conv-replied
    # 是唯一「我回过」的会话，时间轴上要能看到我那一步。
    from .outlook import _assemble_views
    sent_log = {c: [ts] for c, ts in sent_map.items()}
    board, matrix, graph = _assemble_views(view, msgs, sent_log)

    # 语义列在演示模式下是**编的，不调模型** —— 演示的全部意义就是不发送任何东西
    # 出去，为了填三列而去调云端 API 就自相矛盾了。但这几列必须有内容，
    # 否则看不出「信息矩阵」长什么样。横幅已经声明整页是假数据。
    _apply_fake_ai(board["threads"], matrix["rows"])
    # 和真实路径同一个理由：kind 刚被填上，计数必须重算、排除必须重跑
    # （被判成「订阅资讯」那类要摘出矩阵）。见 outlook.inbox 里对应的两句。
    from .outlook_tags import apply_exclusions, dimension_counts
    # 先排除、再计数：分面只统计进了矩阵的会话（真实路径同序，见 outlook.py）。
    apply_exclusions(board["threads"], matrix, graph)
    board["counts"] = dimension_counts(board["threads"])

    addr_src: dict[str, int] = {}
    me_src: dict[str, int] = {}
    for m in msgs:
        addr_src[m["addr_source"]] = addr_src.get(m["addr_source"], 0) + 1
        me_src[m["me_source"]] = me_src.get(m["me_source"], 0) + 1

    return {
        "demo": True,
        "folder": "收件箱（演示数据）",
        "window_days": 7,
        "range": {"since": since_day, "until": until_day,
                  "explicit": bool(since_day or until_day)},
        "generated_at": t0.isoformat(),
        "elapsed_ms": int((_now() - t0).total_seconds() * 1000),
        "buckets": BUCKETS,
        "counts": counts,
        "view": view,
        "board": board,
        "matrix": matrix,
        "graph": graph,
        # 演示模式**没有调用任何模型**，这一点要如实报出来 —— 否则页面上的
        # 「本次外发」区块会显示成 0，用户会以为真实模式也不外发。
        "ai": {"demo": True, "sent": 0, "cached": 0, "batches": 0, "failed": 0,
               "mock": False, "model": "", "chars_sent": 0,
               "skipped": "演示模式不调用模型，语义那三列是编的"},
        "total": len(msgs),
        "messages": msgs,
        "partial": False,
        "cached": False,
        "cache_age_s": 0,
        # 演示数据也给个取数时刻，否则页面上那个「上次刷新」在演示模式下是空的。
        "fetched_at": _now().isoformat(timespec="seconds"),
        # 演示数据没有「存储」这个概念。给 None 而不是 True/False：前端对 False 会弹
        # 「在线模式很慢」的警告，对假数据说这句是误导。
        "cached_store": None,
        "sender_skipped": False,
        "diagnostics": {
            "scanned": len(msgs), "above_range": 0, "skipped_non_mail": 0,
            "stopped_by": "demo", "order": "demo",
            "time_budget_s": 0, "cached_store": None,
            "dropped_duplicates": 0,
            "sent_scanned": 1, "sent_stopped_by": "demo",
            "sent_conversations": len(sent_map),
            "sender_addr_source": addr_src,
            "to_me_source": me_src,
            "me_resolved": {"has_name": True, "name_from": "demo", "alias_count": 1},
        },
    }
