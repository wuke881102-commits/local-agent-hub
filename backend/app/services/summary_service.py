"""周期总结（周 / 月 / 年）——纯本地索引聚合，零重抓、即时返回。

设计要点：
  · 「本期」收录口径 = 按 updated_time 落在时间窗内的资产（"本周动过的文档"，
    比"本周新建"更贴近实际工作量；改过的老文档也会出现）。
  · 时间轴、数字卡、主题分布都从索引库一次查询里算出，不调 LLM、不抓全文，
    所以整页秒开。LLM 只在 /api/summaries/narrative（叙述回顾）里按需触发。
  · 「陪伴天数」是账号级总时长（从我创建的第一个文档算起），不随周期切换变化，
    随 GET /api/summaries 一并返回。
"""
from __future__ import annotations

import datetime as dt
import urllib.parse
from collections import Counter, defaultdict

from ..db import get_db
from . import local_dir as localdir_svc

# 时间轴/叙述最多纳入的条数（窗口内资产理论上有限，给个上限防极端情况）。
_MAX_ITEMS = 400

_WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _period_range(period: str, offset: int, now: dt.datetime) -> tuple[dt.datetime, dt.datetime, str]:
    """把 (period, offset) 解析成 [start, end) 时间窗 + 中文区间标签。

    offset=0 当前期，-1 上一期，+1 下一期（一般用不到，但允许）。
    """
    today = now.date()
    if period == "month":
        base = today.replace(day=1)
        m_index = base.year * 12 + (base.month - 1) + offset
        y, m0 = divmod(m_index, 12)
        start_d = dt.date(y, m0 + 1, 1)
        nm = m_index + 1
        ny, nm0 = divmod(nm, 12)
        end_d = dt.date(ny, nm0 + 1, 1)
        label = f"{start_d.year} 年 {start_d.month} 月"
    elif period == "year":
        y = today.year + offset
        start_d = dt.date(y, 1, 1)
        end_d = dt.date(y + 1, 1, 1)
        label = f"{y} 年"
    else:  # week（默认）—— ISO 周一为起点
        monday = today - dt.timedelta(days=today.weekday())
        start_d = monday + dt.timedelta(weeks=offset)
        end_d = start_d + dt.timedelta(days=7)
        iso = start_d.isocalendar()
        label = f"{iso[0]} 年第 {iso[1]} 周"
    start = dt.datetime.combine(start_d, dt.time.min)
    end = dt.datetime.combine(end_d, dt.time.min)
    return start, end, label


def _is_meeting(asset_type: str, title: str) -> bool:
    if asset_type == "meeting":
        return True
    if asset_type in ("docx", "doc") and (title.startswith("智能纪要") or title.startswith("文字记录")):
        return True
    return False


def _is_contract(title: str, category: str) -> bool:
    return title.lower().endswith(".pdf") or ("合同" in (category or ""))


async def _fetch_window(start_iso: str, end_iso: str) -> list[dict]:
    """窗口内（按 updated_time）的活跃资产。ISO 串字典序与时间序一致，可直接比较。"""
    sql = (
        "SELECT asset_id, asset_type, title, url, owner, owner_id, "
        "created_time, updated_time, source_space, category, summary "
        "FROM asset "
        "WHERE index_state='active' AND updated_time >= ? AND updated_time < ? "
        "ORDER BY updated_time DESC LIMIT ?"
    )
    out: list[dict] = []
    async with get_db() as db:
        async with db.execute(sql, (start_iso, end_iso, _MAX_ITEMS)) as cur:
            async for r in cur:
                out.append({
                    "asset_id": r[0], "type": r[1], "title": r[2] or "(未命名)",
                    "url": r[3] or "", "owner": r[4] or "", "owner_id": r[5] or "",
                    "created": r[6] or "", "updated": r[7] or "",
                    "space": r[8] or "", "category": r[9] or "", "summary": r[10] or "",
                })
    return out


def _fetch_local(local_dir: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """本地目录里「本期改动」（按文件 mtime 落在窗口内）的文件，映射成与飞书条目同构的字典。

    本地文件没有真正的 asset_id / 创建时间：asset_id 用 ``local:<path>``、url 指向本地文件
    预览接口、created 留空（故不计入「新建」）。失败（目录无效等）静默返回空。
    """
    if not local_dir:
        return []
    try:
        data = localdir_svc.list_files(local_dir)
    except Exception:  # noqa: BLE001 —— 目录失效不应拖垮整页
        return []
    start_ts, end_ts = start.timestamp(), end.timestamp()
    out: list[dict] = []
    for f in data.get("items", []):
        mt = f.get("mtime_ts") or 0
        if not (start_ts <= mt < end_ts):
            continue
        path = f.get("path") or ""
        out.append({
            "asset_id": "local:" + path,
            "type": f.get("kind") or "file",
            "title": f.get("name") or "(未命名)",
            "url": "/api/localdir/file?path=" + urllib.parse.quote(path),
            "owner": "", "owner_id": "", "created": "",
            "updated": f.get("mtime") or dt.datetime.fromtimestamp(mt).isoformat(timespec="seconds"),
            "space": "", "category": "本地文件", "summary": "",
        })
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out[:_MAX_ITEMS]


async def _companion(my_id: str | None) -> dict:
    """账号级「与飞书同行」天数：从我创建的第一个文档算起。

    取 owner_id == 我 的最早 created_time；识别不到我的文档时退化为全库最早，
    文案仍写"与飞书同行"。返回 {days, first_doc_date, first_doc_title}。
    """
    row = None
    async with get_db() as db:
        if my_id:
            async with db.execute(
                "SELECT created_time, title FROM asset "
                "WHERE index_state='active' AND owner_id=? AND created_time IS NOT NULL "
                "AND TRIM(created_time) <> '' ORDER BY created_time ASC LIMIT 1",
                (my_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            async with db.execute(
                "SELECT created_time, title FROM asset "
                "WHERE index_state='active' AND created_time IS NOT NULL "
                "AND TRIM(created_time) <> '' ORDER BY created_time ASC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
    if not row or not row[0]:
        return {"days": 0, "first_doc_date": "", "first_doc_title": ""}
    try:
        first = dt.datetime.fromisoformat(str(row[0])[:19])
    except ValueError:
        return {"days": 0, "first_doc_date": "", "first_doc_title": row[1] or ""}
    days = max(0, (dt.datetime.now() - first).days)
    return {
        "days": days,
        "first_doc_date": first.date().isoformat(),
        "first_doc_title": row[1] or "",
    }


def _build_timeline(items: list[dict]) -> list[dict]:
    """按「更新日期」倒序分桶，每天一组。"""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for a in items:
        day = (a.get("updated") or "")[:10]
        if day:
            by_day[day].append(a)
    days_sorted = sorted(by_day.keys(), reverse=True)
    out: list[dict] = []
    for day in days_sorted:
        group = by_day[day]
        group.sort(key=lambda x: x.get("updated") or "", reverse=True)
        try:
            wd = _WEEKDAY_ZH[dt.date.fromisoformat(day).weekday()]
        except ValueError:
            wd = ""
        out.append({
            "date": day,
            "weekday": wd,
            "items": [{
                "asset_id": a["asset_id"], "title": a["title"], "url": a["url"],
                "type": a["type"], "category": a["category"], "summary": a["summary"],
                "updated": a["updated"], "is_new": (a.get("created") or "")[:10] == day,
            } for a in group],
        })
    return out


async def _data_through() -> str:
    """本地索引最后刷新时刻（= 数据截至时间）。用于提示"数据截至 X"，避免用户把
    "本周为空"误解成自己没干活——其实可能只是索引还没刷到本周。"""
    async with get_db() as db:
        async with db.execute(
            "SELECT MAX(last_processed_at) FROM asset WHERE index_state='active'"
        ) as cur:
            row = await cur.fetchone()
            return (row[0] if row and row[0] else "") or ""


async def build_summary(period: str, offset: int, my_id: str | None, local_dir: str | None = None) -> dict:
    """GET /api/summaries 的载荷：陪伴横幅 + 区间 + 数字卡 + 主题分布 + 时间轴。

    传 local_dir 时，把该本地目录中「本期改动」的文件并入时间轴 / 主题分布 / 回顾。
    """
    now = dt.datetime.now()
    start, end, label = _period_range(period, offset, now)
    start_iso = start.isoformat(timespec="seconds")
    end_iso = end.isoformat(timespec="seconds")
    items = await _fetch_window(start_iso, end_iso)
    local_items = _fetch_local(local_dir or "", start, end)
    all_items = items + local_items

    # 数字卡里「动过文档 / 新建 / 会议 / 合同 / 涉及空间」仍按飞书口径统计；本地文件单列一格。
    created_n = sum(1 for a in items if start_iso <= (a.get("created") or "") < end_iso)
    meeting_n = sum(1 for a in items if _is_meeting(a["type"], a["title"]))
    contract_n = sum(1 for a in items if _is_contract(a["title"], a["category"]))
    spaces = {a["space"] for a in items if a.get("space")}

    cat_counter: Counter[str] = Counter()
    for a in all_items:
        cat_counter[(a.get("category") or "").strip() or "未分类"] += 1
    by_category = [{"name": k, "count": v} for k, v in cat_counter.most_common()]

    return {
        "period": period,
        "offset": offset,
        "range_label": label,
        "range_start": start.date().isoformat(),
        "range_end": (end.date() - dt.timedelta(days=1)).isoformat(),
        "has_prev": True,
        "has_next": offset < 0,
        "data_through": await _data_through(),
        "companion": await _companion(my_id),
        "stats": {
            "updated": len(items),
            "created": created_n,
            "meetings": meeting_n,
            "contracts": contract_n,
            "spaces": len(spaces),
            "local_files": len(local_items),
        },
        "by_category": by_category,
        "timeline": _build_timeline(all_items),
    }


def build_narrative_prompt(label: str, items: list[dict]) -> tuple[str, str]:
    """叙述回顾的 (system, user)。只喂标题+一句话摘要+分类，不抓全文 → 快档够用。"""
    system = (
        "你是用户的工作回顾助手。基于给定时间区间内『动过的飞书文档与本地文件』清单"
        "（标记为「本地文件」的来自用户本地目录），"
        "用中文写一段 80–160 字的自然、口语化的回顾，点出这段时间的工作重心与亮点，"
        "不要逐条罗列、不要编造清单里没有的数字或事实。"
        "然后从清单里挑出最重要的 3–5 个条目作为重点。"
        "严格输出 JSON：{\"narrative\": \"……\", \"highlights\": [{\"index\": 1, \"reason\": \"为何重要(≤20字)\"}]}。"
        "index 用条目前的编号。"
    )
    lines = []
    for i, a in enumerate(items, 1):
        cat = a.get("category") or "未分类"
        summ = (a.get("summary") or "").strip()
        seg = f"[{i}] {a['title']}｜{cat}"
        if summ:
            seg += f"｜{summ}"
        lines.append(seg)
    user = f"时间区间：{label}\n本期动过的文档与本地文件（共 {len(items)} 条）：\n" + "\n".join(lines)
    return system, user


def select_items_for_narrative(timeline: list[dict]) -> list[dict]:
    """把时间轴拍平成顺序条目列表（叙述用）。带 asset_id 以便回填重点。"""
    flat: list[dict] = []
    for day in timeline:
        for it in day.get("items", []):
            flat.append(it)
    return flat
