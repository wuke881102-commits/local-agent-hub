"""规则维度计算（来源 / 类型 / 所有者 / 活跃度 / 空间）。

document_map Agent 与 /api/assets/map 端点共用同一套实现，避免漂移。
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from typing import Any


TYPE_ZH = {
    "doc": "云文档 (旧)", "docx": "云文档", "wiki": "知识库节点",
    "sheet": "电子表格", "bitable": "多维表格", "slides": "幻灯片",
    "file": "文件附件", "folder": "文件夹", "shortcut": "快捷方式",
    "meeting": "会议纪要", "mindnote": "思维导图", "other": "其他",
}


def compute_all(assets: list[dict], my_open_id: str | None) -> dict[str, Any]:
    """一次性算齐所有 facet，并附带 summary。供 Agent / 端点共用。"""
    by_origin = facet_origin(assets, my_open_id)
    by_type = facet_type(assets)
    by_owner = facet_owner(assets, my_open_id)
    by_recency = facet_recency(assets)
    by_created = facet_created(assets)
    by_space = facet_space(assets)
    by_category = facet_category(assets)
    summary = _summary(assets, by_type, by_origin)
    return {
        "total": len(assets),
        "summary": summary,
        "by_origin": by_origin,
        "by_type": by_type,
        "by_owner": by_owner,
        "by_recency": by_recency,
        "by_created": by_created,
        "by_space": by_space,
        "by_category": by_category,
        "my_open_id": my_open_id,
    }


def origin_of(asset: dict, my_id: str | None) -> str:
    """单条资产的来源归类。facet 与 /api/assets 过滤共用，避免漂移。"""
    t = asset.get("type") or ""
    oid = asset.get("owner_id") or ""
    if t == "wiki":
        return "知识库"
    if my_id and oid == my_id:
        return "我创建的"
    if oid:
        return "他人共享给我"
    return "未知归属"


def facet_origin(assets: list[dict], my_id: str | None) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for a in assets:
        groups[origin_of(a, my_id)].append(a)
    return _to_buckets(groups, with_samples=True)


def facet_type(assets: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for a in assets:
        t = a.get("type") or "other"
        groups[TYPE_ZH.get(t, t)].append(a)
    return _to_buckets(groups, with_samples=True, with_type=True)


def facet_owner(assets: list[dict], my_id: str | None) -> list[dict]:
    counter: Counter[str] = Counter()
    name_map: dict[str, str] = {}
    sample_map: dict[str, list[dict]] = defaultdict(list)
    for a in assets:
        oid = a.get("owner_id") or "(未知)"
        counter[oid] += 1
        name = a.get("owner") or ("我" if my_id and oid == my_id else (oid[:12] + "…" if oid != "(未知)" else "(未知)"))
        if oid not in name_map:
            name_map[oid] = name
        if len(sample_map[oid]) < 5:
            sample_map[oid].append({
                "title": a.get("title"), "type": a.get("type"),
                "updated": a.get("updated"), "url": a.get("url"),
            })
    out = []
    for oid, n in counter.most_common(10):
        out.append({
            "owner_id": oid,
            "name": name_map[oid] + (" · 你自己" if my_id and oid == my_id else ""),
            "count": n,
            "is_me": bool(my_id and oid == my_id),
            "samples": sample_map[oid],
        })
    return out


RECENCY_BUCKETS = ["最近 7 天", "最近 30 天", "最近 90 天", "最近半年", "半年以上未动", "时间未知"]


def recency_of(asset: dict, now: dt.datetime | None = None) -> str:
    """单条资产的活跃度归类。facet 与 /api/assets 过滤共用，避免漂移。"""
    now = now or dt.datetime.now()
    updated = (asset.get("updated") or "").strip()
    if not updated:
        return "时间未知"
    try:
        ts = dt.datetime.fromisoformat(updated[:19])
    except ValueError:
        return "时间未知"
    days = (now - ts).days
    if days <= 7:
        return "最近 7 天"
    if days <= 30:
        return "最近 30 天"
    if days <= 90:
        return "最近 90 天"
    if days <= 180:
        return "最近半年"
    return "半年以上未动"


def facet_recency(assets: list[dict]) -> list[dict]:
    now = dt.datetime.now()
    bucket_assets: dict[str, list[dict]] = defaultdict(list)
    for a in assets:
        bucket_assets[recency_of(a, now)].append(a)
    out = []
    for label in RECENCY_BUCKETS:
        items = bucket_assets.get(label, [])
        if not items:
            continue
        items.sort(key=lambda x: x.get("updated") or "", reverse=True)
        out.append({
            "name": label,
            "count": len(items),
            "samples": [{
                "title": a.get("title"), "type": a.get("type"),
                "updated": a.get("updated"), "owner": a.get("owner"), "url": a.get("url"),
            } for a in items[:6]],
        })
    return out


def created_year_of(asset: dict) -> str:
    """单条资产的创建年份（"2024" / "时间未知"）。facet 与下钻过滤共用。"""
    c = (asset.get("created") or "").strip()
    if len(c) >= 4 and c[:4].isdigit():
        return c[:4]
    return "时间未知"


def facet_created(assets: list[dict]) -> list[dict]:
    """按创建年份分组——回答"知识是哪几年攒下来的"，与"按活跃度（最后编辑）"互补。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for a in assets:
        groups[created_year_of(a)].append(a)
    known = sorted((y for y in groups if y != "时间未知"), reverse=True)
    order = known + (["时间未知"] if "时间未知" in groups else [])
    out = []
    for y in order:
        items = groups[y]
        items.sort(key=lambda x: x.get("created") or "", reverse=True)
        out.append({
            "name": f"{y} 年" if y != "时间未知" else "时间未知",
            "year": y,
            "count": len(items),
            "samples": [{
                "title": a.get("title"), "type": a.get("type"),
                "updated": a.get("updated"), "owner": a.get("owner"), "url": a.get("url"),
            } for a in items[:6]],
        })
    return out


def facet_space(assets: list[dict]) -> list[dict]:
    counter: Counter[str] = Counter()
    for a in assets:
        sp = (a.get("space") or "").strip() or "未分组"
        counter[sp] += 1
    return [{"name": k, "count": v} for k, v in counter.most_common(20)]


def category_of(asset: dict) -> str:
    """单条资产的 AI 分类（「摘要 / 标签回填」生成）。未回填归到「未分类」。

    facet 与 /api/assets 下钻过滤共用，避免漂移。
    """
    return (asset.get("category") or "").strip() or "未分类"


def facet_category(assets: list[dict]) -> list[dict]:
    """按 AI 回填的业务分类分组——稳定可分组（模型从固定候选里选），可点击下钻。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for a in assets:
        groups[category_of(a)].append(a)
    return _to_buckets(groups, with_samples=True)


def _to_buckets(groups: dict[str, list[dict]], *, with_samples: bool = False, with_type: bool = False) -> list[dict]:
    out = []
    for name, items in sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True):
        b: dict = {"name": name, "count": len(items)}
        if with_type and items:
            b["type"] = items[0].get("type")
        if with_samples:
            b["samples"] = [{
                "title": a.get("title"), "type": a.get("type"),
                "updated": a.get("updated"), "owner": a.get("owner"), "url": a.get("url"),
            } for a in items[:5]]
        out.append(b)
    return out


def _summary(assets: list[dict], by_type: list[dict], by_origin: list[dict]) -> str:
    type_str = "、".join(f"{t['name']} {t['count']}" for t in by_type[:4])
    origin_str = "、".join(f"{o['name']} {o['count']}" for o in by_origin)
    return f"本地索引共 {len(assets)} 条资产。类型分布：{type_str}。来源分布：{origin_str}。"
