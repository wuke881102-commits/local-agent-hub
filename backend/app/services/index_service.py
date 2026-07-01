"""本地元数据索引刷新（PRD §10）。"""
from __future__ import annotations

import datetime as dt
import json
from typing import Awaitable, Callable

from ..db import get_db


async def refresh(lark, log: Callable[[str, str], Awaitable[None]] | None = None) -> dict[str, int]:
    """从飞书 CLI 拉取可访问资产，UPSERT 到本地索引。"""
    counts = {"docs": 0, "wiki": 0, "base": 0, "sheet": 0, "meeting": 0, "other": 0, "total": 0, "removed": 0}

    # 本轮在飞书侧仍可见的全部 asset_id；以及"是否所有来源都成功拉取"。
    # 只有全部来源都成功、且确实拉到东西时，才会把"这次没出现的旧资产"标为
    # removed——否则一次临时的接口失败就可能把整类资产误标为已删除。
    seen_ids: set[str] = set()
    all_ok = True

    async def _log(level: str, msg: str) -> None:
        if log:
            await log(level, msg)

    # 每次刷新前清掉 drive 遍历缓存：它按进程级单例缓存，否则一次"无权限/接口失败"的
    # 空结果会被后续所有刷新复用，导致云盘文档恒为 0（即便事后已授权）。
    if hasattr(lark, "reset_drive_cache"):
        lark.reset_drive_cache()

    # docs from drive root walk (我自己的 drive 文件夹下)
    try:
        items = await lark.docs_list()
        await _log("info", f"drive 文件夹拉到 {len(items)} 条")
        counts["docs"] = await _upsert(items, default_type="doc", seen=seen_ids)
    except Exception as e:  # noqa: BLE001
        all_ok = False
        await _log("warn", f"drive 列文件失败：{e}")

    # docs from search (含他人共享给我的、群里共享的、近期交互的)
    if hasattr(lark, "docs_search_accessible"):
        try:
            shared = await lark.docs_search_accessible()
            n_shared = await _upsert(shared, default_type="doc", seen=seen_ids)
            await _log("info", f"drive 搜索拉到 {len(shared)} 条（含共享给我），新增/更新 {n_shared} 行")
            counts["docs"] += n_shared
        except Exception as e:  # noqa: BLE001
            all_ok = False
            await _log("warn", f"drive search 失败：{e}")

    # wiki — 列空间然后每个空间列节点
    try:
        spaces = await lark.wiki_spaces_list()
        await _log("info", f"wiki spaces 返回 {len(spaces)} 个")
        for sp in spaces:
            sid = sp.get("space_id") or sp.get("id")
            if not sid:
                continue
            try:
                nodes = await lark.wiki_nodes_list(sid)
                counts["wiki"] += await _upsert(
                    [{**n, "space": sp.get("name") or sp.get("space_name") or "Wiki"} for n in nodes],
                    default_type="wiki", seen=seen_ids,
                )
            except Exception as ie:  # noqa: BLE001
                all_ok = False  # 单个空间拉取失败 = wiki 数据不完整，本轮不清理
                await _log("warn", f"wiki_nodes_list({sid}) 失败：{ie}")
    except Exception as e:  # noqa: BLE001
        all_ok = False
        await _log("warn", f"wiki_spaces_list 失败：{e}")

    # base
    try:
        bases = await lark.base_list_apps()
        counts["base"] = await _upsert(bases, default_type="base", seen=seen_ids)
    except Exception as e:  # noqa: BLE001
        all_ok = False
        await _log("warn", f"base_list_apps 失败：{e}")

    # minutes / meetings
    try:
        mins = await lark.minutes_list()
        counts["meeting"] = await _upsert(mins, default_type="meeting", seen=seen_ids)
    except Exception as e:  # noqa: BLE001
        all_ok = False
        await _log("warn", f"minutes_list 失败：{e}")

    counts["total"] = counts["docs"] + counts["wiki"] + counts["base"] + counts["sheet"] + counts["meeting"] + counts["other"]
    await _log("info", f"刷新完成，合计 {counts['total']} 条")

    # ── 清理：把飞书侧已删除/已失去访问权限（本轮没再出现）的旧资产标为 removed ──
    # 安全闸：仅在"所有来源都成功"且"确实拉到至少 1 条"时执行，避免误删。
    if all_ok and seen_ids:
        prune_now = dt.datetime.now().isoformat(timespec="seconds")
        async with get_db() as db:
            await db.execute("CREATE TEMP TABLE IF NOT EXISTS _seen (id TEXT PRIMARY KEY)")
            await db.execute("DELETE FROM _seen")
            await db.executemany(
                "INSERT OR IGNORE INTO _seen(id) VALUES (?)", [(i,) for i in seen_ids]
            )
            cur = await db.execute(
                "UPDATE asset SET index_state='removed', last_processed_at=? "
                "WHERE index_state='active' AND asset_id NOT IN (SELECT id FROM _seen)",
                (prune_now,),
            )
            counts["removed"] = cur.rowcount or 0
            await db.commit()
        if counts["removed"]:
            await _log("info", f"标记 {counts['removed']} 条本地资产为「已移除」（飞书侧已删除或已失去访问权限）")
        else:
            await _log("info", "本地索引与飞书一致，无需清理")
    elif not all_ok:
        await _log("warn", "有来源拉取失败，本轮跳过「已删除资产」清理，避免误删")

    # ── owner_id → name 反查（contact +search-user，批量 80 个/次） ──
    if hasattr(lark, "users_resolve_names"):
        try:
            unique_ids: set[str] = set()
            async with get_db() as db:
                async with db.execute(
                    "SELECT DISTINCT owner_id FROM asset WHERE index_state='active' AND owner_id LIKE 'ou_%' AND (owner IS NULL OR owner = '' OR owner LIKE 'ou_%')"
                ) as cur:
                    async for r in cur:
                        if r[0]:
                            unique_ids.add(r[0])
            if unique_ids:
                await _log("info", f"反查 {len(unique_ids)} 个 owner 名字 …")
                names = await lark.users_resolve_names(list(unique_ids))
                if names:
                    async with get_db() as db:
                        for oid, name in names.items():
                            await db.execute(
                                "UPDATE asset SET owner=? WHERE owner_id=? AND (owner IS NULL OR owner = '' OR owner LIKE 'ou_%')",
                                (name, oid),
                            )
                        await db.commit()
                    await _log("info", f"已回填 {len(names)} 个用户名（{len(unique_ids) - len(names)} 个外部/已离职用户跳过）")
        except Exception as e:  # noqa: BLE001
            await _log("warn", f"owner 名字反查失败：{e}（不影响其他索引）")

    return counts


async def _upsert(items: list[dict], default_type: str, seen: set[str] | None = None) -> int:
    now = dt.datetime.now().isoformat(timespec="seconds")
    n = 0
    async with get_db() as db:
        for it in items:
            aid = (it.get("asset_id") or it.get("token") or it.get("id")
                   or it.get("node_token") or it.get("obj_token"))
            if not aid:
                continue
            if seen is not None:
                seen.add(aid)
            # owner_id resolution — different shapes per source:
            #   drive files list: {"owner_id": "ou_..."}
            #   wiki nodes list:  {"owner": "ou_...", "creator": "ou_..."}
            # If `owner` value looks like an open_id (ou_*), use it as id.
            raw_owner = (it.get("owner") or "").strip()
            owner_id = (
                it.get("owner_id")
                or (raw_owner if raw_owner.startswith("ou_") else "")
                or it.get("creator")
                or it.get("owner_open_id")
                or ""
            )
            owner_name = (
                it.get("owner_name")
                or it.get("creator_name")
                or (raw_owner if raw_owner and not raw_owner.startswith("ou_") else "")
            )
            # Time fields vary by source:
            #   drive: modified_time / created_time (epoch seconds, string)
            #   wiki:  obj_edit_time / node_create_time (epoch seconds, int)
            updated_raw = (
                it.get("updated_time") or it.get("modified_time")
                or it.get("obj_edit_time") or it.get("node_edit_time")
                or it.get("updated") or ""
            )
            updated_iso = _to_iso(updated_raw)
            created_raw = (
                it.get("created_time") or it.get("obj_create_time")
                or it.get("node_create_time") or it.get("created") or ""
            )
            created_iso = _to_iso(created_raw)
            row = (
                aid,
                it.get("type") or default_type,
                it.get("title") or it.get("name") or "(未命名)",
                it.get("url") or "",
                owner_name,
                owner_id,
                created_iso,
                updated_iso,
                it.get("space") or it.get("source_space") or "",
                it.get("path") or "",
                json.dumps(it.get("tags") or [], ensure_ascii=False),
                it.get("category") or "",
                it.get("summary") or "",
                now,
                "indexed",
                json.dumps(it, ensure_ascii=False)[:8000],
            )
            await db.execute(
                """INSERT INTO asset (asset_id, asset_type, title, url, owner, owner_id, created_time, updated_time,
                                       source_space, path, tags, category, summary, last_processed_at,
                                       last_task_status, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(asset_id) DO UPDATE SET
                       asset_type=excluded.asset_type,
                       title=excluded.title,
                       url=excluded.url,
                       owner=excluded.owner,
                       owner_id=excluded.owner_id,
                       updated_time=excluded.updated_time,
                       source_space=excluded.source_space,
                       last_processed_at=excluded.last_processed_at,
                       raw_json=excluded.raw_json,
                       index_state='active'
                """,
                row,
            )
            n += 1
        await db.commit()
    return n


def _to_iso(raw: str) -> str:
    """Normalize timestamp formats to ISO date string.

    Drive returns epoch seconds as a string ("1777363683"). Wiki returns ISO
    or epoch — we accept both. Empty stays empty. Unparseable returned as-is.
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if s.isdigit() and len(s) >= 10:
        try:
            sec = int(s[:13]) / 1000 if len(s) == 13 else int(s)
            return dt.datetime.fromtimestamp(sec).isoformat(timespec="seconds")
        except (ValueError, OSError):
            return s
    return s


async def stats() -> dict:
    counts: dict[str, int] = {}
    last: str | None = None
    async with get_db() as db:
        async with db.execute("SELECT asset_type, COUNT(*) FROM asset WHERE index_state='active' GROUP BY asset_type") as cur:
            async for r in cur:
                counts[r[0]] = r[1]
        async with db.execute("SELECT MAX(last_processed_at) FROM asset WHERE index_state='active'") as cur:
            row = await cur.fetchone()
            last = row[0] if row else None
        # PDF：file/shortcut 中 .pdf 子集，单独计数（不计入 total，避免与「文件」重复）
        async with db.execute(
            "SELECT COUNT(*) FROM asset WHERE index_state='active' AND asset_type IN ('file','shortcut') AND lower(title) LIKE '%.pdf'"
        ) as cur:
            row = await cur.fetchone()
            pdf_count = row[0] if row else 0
        # 会议纪要：妙记 + AI 智能纪要/文字记录 docx（不计入 total，与 文档/妙记 重叠）
        async with db.execute(
            f"SELECT COUNT(*) FROM asset WHERE index_state='active' AND {_MEETING_NOTES_SQL}"
        ) as cur:
            row = await cur.fetchone()
            meeting_notes_count = row[0] if row else 0
    total = sum(counts.values())
    counts["pdf"] = pdf_count
    counts["meeting_notes"] = meeting_notes_count
    counts["total"] = total
    counts["last_refreshed"] = last
    return counts


async def get_asset_title(asset_id: str) -> str | None:
    """单个 token → 资产标题（找不到返回 None）。用于把任务「对象」显示成文件名。"""
    if not asset_id:
        return None
    async with get_db() as db:
        async with db.execute("SELECT title FROM asset WHERE asset_id=?", (asset_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else None


async def get_titles_for(asset_ids: list[str]) -> dict[str, str]:
    """批量 token → 标题映射（找不到的 token 不出现在结果里）。"""
    ids = list({a for a in (asset_ids or []) if a})
    if not ids:
        return {}
    out: dict[str, str] = {}
    async with get_db() as db:
        placeholders = ",".join("?" for _ in ids)
        async with db.execute(
            f"SELECT asset_id, title FROM asset WHERE asset_id IN ({placeholders})", ids,
        ) as cur:
            async for r in cur:
                if r[1]:
                    out[r[0]] = r[1]
    return out


TYPE_ALIASES = {
    # Front-end tab id  →  one or more asset_type values stored in the index.
    # Drive uses modern names (docx for the new doc, bitable for the multi-table
    # editor, etc.). Map the human-facing buckets to the union of real types.
    "doc":     ("docx", "doc"),
    "wiki":    ("wiki",),
    "meeting": ("meeting",),
    "base":    ("bitable",),
    "sheet":   ("sheet",),
    "slides":  ("slides",),
    "file":    ("file", "shortcut"),
}

# 「会议纪要」是个跨类型的虚拟分类，不是独立 asset_type：
#   经典妙记（asset_type=meeting） + 飞书 AI 智能纪要 / 文字记录
#   （本质是 docx，标题以「智能纪要」「文字记录」开头）。
# 与 PDF 一样是派生子集，不计入 total（否则与「文档 / 妙记」重复计数）。
# 标题前缀用字面量 LIKE（中文为普通字符，% 为通配符），与前端会议来源下拉口径一致。
_MEETING_NOTES_SQL = (
    "(asset_type = 'meeting' "
    "OR (asset_type IN ('docx','doc') "
    "AND (title LIKE '智能纪要%' OR title LIKE '文字记录%')))"
)


def _build_where(
    asset_type: str | None, q: str | None, *,
    type_exact: str | None, owner_id: str | None, space: str | None,
    created_year: str | None, category: str | None,
) -> tuple[list[str], list]:
    """构造 list_assets / count_assets 共用的 WHERE 子句与参数，避免两处口径漂移。"""
    # 默认只看在飞书侧仍可见的资产；removed（已删/已失权）一律排除，
    # 让本地索引页、文档地图、知识库治理等所有列表口径一致。
    where: list[str] = ["index_state = 'active'"]
    params: list = []
    if type_exact:
        # 精确匹配单一底层类型（如 docx / bitable），用于"按类型"分面下钻。
        where.append("asset_type = ?")
        params.append(type_exact)
    elif asset_type == "pdf":
        # PDF 不是独立的 asset_type：它是 file/shortcut 中标题以 .pdf 结尾的子集。
        where.append("asset_type IN ('file','shortcut') AND lower(title) LIKE '%.pdf'")
    elif asset_type == "meeting_notes":
        # 会议纪要：经典妙记 + AI 智能纪要/文字记录 docx（跨类型虚拟分类）。
        where.append(_MEETING_NOTES_SQL)
    elif asset_type:
        types = TYPE_ALIASES.get(asset_type, (asset_type,))
        placeholders = ",".join("?" for _ in types)
        where.append(f"asset_type IN ({placeholders})")
        params.extend(types)
    if owner_id:
        # facet 把无 owner 的资产归到哨兵 "(未知)"；下钻时还原为空匹配。
        if owner_id == "(未知)":
            where.append("(owner_id IS NULL OR TRIM(owner_id) = '')")
        else:
            where.append("owner_id = ?")
            params.append(owner_id)
    if space:
        # facet 把空字符串归到"未分组"；下钻时还原为 NULL/空匹配。
        if space == "未分组":
            where.append("(source_space IS NULL OR TRIM(source_space) = '')")
        else:
            where.append("source_space = ?")
            params.append(space)
    if created_year:
        # 按创建年份下钻；created_time 形如 "2024-03-15T..."。
        if created_year == "时间未知":
            where.append("(created_time IS NULL OR TRIM(created_time) = '')")
        else:
            where.append("substr(created_time, 1, 4) = ?")
            params.append(created_year)
    if category:
        # 按 AI 回填的分类下钻；facet 把未回填归到「未分类」，下钻还原为空匹配。
        if category == "未分类":
            where.append("(category IS NULL OR TRIM(category) = '')")
        else:
            where.append("category = ?")
            params.append(category)
    if q:
        # 搜索覆盖标题/空间/负责人 + AI 回填的摘要/标签/分类，让回填后的检索真正有用。
        where.append(
            "(title LIKE ? OR source_space LIKE ? OR owner LIKE ? "
            "OR summary LIKE ? OR tags LIKE ? OR category LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like, like, like])
    return where, params


async def list_assets(
    asset_type: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
    *,
    type_exact: str | None = None,
    owner_id: str | None = None,
    space: str | None = None,
    created_year: str | None = None,
    category: str | None = None,
) -> list[dict]:
    sql = (
        "SELECT asset_id, asset_type, title, url, owner, owner_id, updated_time, "
        "source_space, tags, category, summary, created_time FROM asset"
    )
    where, params = _build_where(
        asset_type, q, type_exact=type_exact, owner_id=owner_id,
        space=space, created_year=created_year, category=category,
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_time DESC LIMIT ? OFFSET ?"
    params.extend([limit, max(0, offset)])

    out: list[dict] = []
    async with get_db() as db:
        async with db.execute(sql, params) as cur:
            async for r in cur:
                out.append({
                    "asset_id": r[0], "type": r[1], "title": r[2], "url": r[3],
                    "owner": r[4], "owner_id": r[5],
                    "updated": r[6], "space": r[7],
                    "tags": json.loads(r[8] or "[]"), "category": r[9], "summary": r[10],
                    "created": r[11],
                })
    return out


async def count_assets(
    asset_type: str | None = None,
    q: str | None = None,
    *,
    type_exact: str | None = None,
    owner_id: str | None = None,
    space: str | None = None,
    created_year: str | None = None,
    category: str | None = None,
) -> int:
    """与 list_assets 同条件的匹配总数（用于分页）。"""
    where, params = _build_where(
        asset_type, q, type_exact=type_exact, owner_id=owner_id,
        space=space, created_year=created_year, category=category,
    )
    sql = "SELECT COUNT(*) FROM asset"
    if where:
        sql += " WHERE " + " AND ".join(where)
    async with get_db() as db:
        async with db.execute(sql, params) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def filter_options() -> dict:
    """本地资产页筛选下拉的可选项：AI 分类 / 所属空间 / 负责人（含计数）。

    都按计数倒序，便于把常用项排前面。owners 只取有 open_id 的真实用户
    （owner_id LIKE 'ou_%'），避免把无主 / 外部杂项塞进下拉。
    """
    cats: list[dict] = []
    spaces: list[dict] = []
    owners: list[dict] = []
    async with get_db() as db:
        async with db.execute(
            "SELECT category, COUNT(*) c FROM asset "
            "WHERE index_state='active' AND category IS NOT NULL AND TRIM(category) <> '' "
            "GROUP BY category ORDER BY c DESC"
        ) as cur:
            async for r in cur:
                cats.append({"name": r[0], "count": r[1]})
        async with db.execute(
            "SELECT source_space, COUNT(*) c FROM asset "
            "WHERE index_state='active' AND source_space IS NOT NULL AND TRIM(source_space) <> '' "
            "GROUP BY source_space ORDER BY c DESC"
        ) as cur:
            async for r in cur:
                spaces.append({"name": r[0], "count": r[1]})
        async with db.execute(
            "SELECT owner_id, MAX(owner) nm, COUNT(*) c FROM asset "
            "WHERE index_state='active' AND owner_id LIKE 'ou_%' AND owner IS NOT NULL AND TRIM(owner) <> '' "
            "GROUP BY owner_id ORDER BY c DESC"
        ) as cur:
            async for r in cur:
                owners.append({"owner_id": r[0], "name": r[1], "count": r[2]})
    return {"categories": cats, "spaces": spaces, "owners": owners}


async def save_enrichment(rows: list[dict]) -> int:
    """批量写回 AI 生成的 summary / category / tags（PRD §10 元信息富集）。

    rows: [{asset_id, summary, category, tags}]。只更新这三列，不触碰刷新写入的
    标题/owner/时间等字段，因此可与 refresh 并存、可反复重跑。
    """
    n = 0
    async with get_db() as db:
        for r in rows:
            aid = r.get("asset_id")
            if not aid:
                continue
            await db.execute(
                "UPDATE asset SET summary=?, category=?, tags=? WHERE asset_id=?",
                (
                    (r.get("summary") or "").strip(),
                    (r.get("category") or "").strip(),
                    json.dumps(r.get("tags") or [], ensure_ascii=False),
                    aid,
                ),
            )
            n += 1
        await db.commit()
    return n
