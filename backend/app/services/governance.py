"""陈旧内容三档分流（规则版）。

knowledge-governance Agent 与 /api/assets/governance 端点共用同一套规则，保证
"页面即时预览"与"LLM 复核"口径一致。Agent 在 compute_triage 的规则结果上再叠加
LLM 逐条复核（给置信度/理由、可改判保留），最后都用 to_payload 产出统一的前端结构。
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from typing import Any


# 时效型类别：内容随时间贬值，长期未更新大概率可归档。
TIME_BOUND_CATS = {"会议纪要", "调研分析", "数据报表", "项目管理", "市场材料"}
# 常青型类别：可能长期有效，老了也未必该删，只提示确认。
EVERGREEN_CATS = {"制度规范", "技术文档", "合规安全", "培训材料", "财务税务", "人事行政"}
# 标题里的"死档信号"：副本 / 草稿 / 测试 / 未命名 / 旧版本等。
_DEAD_SIGNAL = re.compile(
    r"(副本|草稿|初稿|废弃|作废|测试|未命名|旧版|备份|test|copy|draft|backup|temp|tmp|\bold\b)",
    re.IGNORECASE,
)
CONF_RANK = {"high": 0, "medium": 1, "low": 2}


def rule_bucket(a: dict) -> tuple[str, str, str]:
    """规则预分档 → (bucket, confidence, reason)。bucket ∈ archive|evergreen|review。"""
    cat = (a.get("category") or "").strip()
    title = a.get("title") or ""
    if _DEAD_SIGNAL.search(title):
        return "archive", "high", "标题含 副本/草稿/测试/废弃/旧版 等信号"
    if cat in TIME_BOUND_CATS:
        return "archive", "medium", f"时效型类别（{cat}）且长期未更新"
    if cat in EVERGREEN_CATS:
        return "evergreen", "low", f"常青型类别（{cat}），建议确认是否仍有效"
    return "review", "low", f"类别（{cat or '未分类'}）不明确，需人工判断"


def normalize_title(title: str) -> str:
    """简易归一化：lowercase、去标点和版本/日期后缀（用于重复检测）。"""
    s = (title or "").lower().strip()
    s = re.sub(r"[v_\-]\d+(\.\d+)*\b", "", s)
    s = re.sub(r"\(?\d{4}[年\-/]\d{1,2}([月\-/]\d{1,2})?日?\)?", "", s)
    s = re.sub(r"[【】\[\]（）()「」『』《》<>·、，。,!?！？：:；;\s]+", "", s)
    s = re.sub(r"(草稿|初稿|定稿|终稿|副本|copy|draft|final)", "", s)
    return s.strip()


def compact(a: dict) -> dict:
    return {
        "asset_id": a.get("asset_id"),
        "title": a.get("title"),
        "owner": a.get("owner") or None,
        "updated": a.get("updated"),
        "space": a.get("space"),
        "type": a.get("type"),
        "category": a.get("category") or None,
        "summary": a.get("summary") or None,
        "url": a.get("url"),
    }


def compute_triage(assets: list[dict], stale_days: int, *, mine_only: bool = False) -> dict:
    """纯规则三档分流。返回包含**完整**列表的中间结构，供 Agent 叠加 LLM 后再 to_payload。"""
    cutoff = (dt.datetime.now() - dt.timedelta(days=stale_days)).strftime("%Y-%m-%d")
    stale = [a for a in assets if (a.get("updated") or "").strip() and (a.get("updated") or "").strip() < cutoff]
    stale.sort(key=lambda x: x.get("updated") or "")

    buckets: dict[str, list[dict]] = {"archive": [], "evergreen": [], "review": []}
    for a in stale:
        bk, conf, reason = rule_bucket(a)
        item = compact(a)
        item["confidence"] = conf
        item["reason"] = reason
        buckets[bk].append(item)
    buckets["archive"].sort(key=lambda x: (CONF_RANK.get(x.get("confidence"), 3), x.get("updated") or ""))
    buckets["evergreen"].sort(key=lambda x: x.get("updated") or "")
    buckets["review"].sort(key=lambda x: x.get("updated") or "")

    no_owner = [a for a in assets if not (a.get("owner") or "").strip()]

    norm_groups: dict[str, list[dict]] = defaultdict(list)
    for a in assets:
        norm = normalize_title(a.get("title") or "")
        if len(norm) < 3:
            continue
        norm_groups[norm].append(a)
    dup_groups = sorted([g for g in norm_groups.values() if len(g) >= 2], key=len, reverse=True)

    return {
        "scanned": len(assets),
        "stale_days": stale_days,
        "mine_only": mine_only,
        "stale_count": len(stale),
        "buckets": buckets,
        "no_owner": no_owner,
        "dup_groups": dup_groups,
    }


def to_payload(triage: dict, recommendations: dict | None = None) -> dict:
    """把（可能已被 LLM 改写过的）triage 中间结构转成前端消费的 JSON 结构。"""
    b = triage["buckets"]
    metrics = {
        "total_assets": triage["scanned"],
        "mine_only": triage["mine_only"],
        "stale_days_threshold": triage["stale_days"],
        "stale_count": triage["stale_count"],
        "archive_count": len(b["archive"]),
        "evergreen_count": len(b["evergreen"]),
        "review_count": len(b["review"]),
        "no_owner_count": len(triage["no_owner"]),
        "dup_groups": len(triage["dup_groups"]),
    }
    overall = (recommendations or {}).get("overall") or _overall(metrics)
    return {
        "overall": overall,
        "metrics": metrics,
        "stale_triage": {
            "archive": b["archive"][:1000],
            "evergreen": b["evergreen"][:1000],
            "review": b["review"][:1000],
        },
        "no_owner": [compact(a) for a in triage["no_owner"][:300]],
        "duplicates": [
            {"normalized_title": normalize_title(g[0].get("title") or ""), "items": [compact(a) for a in g[:20]]}
            for g in triage["dup_groups"][:80]
        ],
        "recommendations": recommendations or {},
    }


def _overall(m: dict) -> str:
    return (
        f"扫描 {m['total_assets']} 篇资产：{m['stale_count']} 篇超过 {m['stale_days_threshold']} 天未更新，"
        f"经分流——建议归档 {m['archive_count']} 篇、长青参考 {m['evergreen_count']} 篇、"
        f"待复核 {m['review_count']} 篇；另有无主 {m['no_owner_count']} 篇、重复嫌疑 {m['dup_groups']} 组。"
    )
