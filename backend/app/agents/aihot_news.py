"""AIHot 新闻简报 Agent —— 把 AIHOT 的公开资讯编成一份内部 AI 简报。

## 为什么这么设计（新闻这块的选型理由）

AIHOT 给了四条入口：Agent Skills / MCP / RSS / REST API v1。这里选 **REST API v1**：
  · RSS 只有标题+摘要，丢掉了 `category` / `score` / `selected` / `reason`（站点自己
    写的「为什么值得看」）这些最有用的字段，也没法按窗口/关键词过滤。
  · MCP 要在本机跑一个 MCP 客户端，本工作台是 FastAPI 后端不是 MCP host，为一个场景
    引入协议栈不划算；而且 MCP 工具返回的就是 v1 的同一批数据。
  · REST v1 有 ETag + s-maxage，能被 services.aihot 的磁盘缓存吃掉，重复运行几乎零成本。

四个端点各司其职，一次运行都拉上：
  · `/items`        —— 窗口内的资讯池（可按 category / q 过滤），简报的正文来源
  · `/hot-topics`   —— 当前热点 Top10（带 rank），决定「今天最该看什么」
  · `/stories/{id}` —— 热点事件的报道时间线 + 随演化更新的综述，回答「这事进展到哪了」
  · `/dailies/latest` —— 站点每天 08:00 发布的精编日报，当作分栏与编辑口径的参照

## 防幻觉的关键设计

条目喂给模型时**编号**（[1]…[n]），并明确禁止模型输出任何 URL —— 它只能填 `refs: [3,7]`。
链接、来源名、时间全部由 Python 按编号回填。这样模型再怎么发挥也不可能编出一条
不存在的新闻或一个错的链接；引用不到编号的内容会被清洗掉。

## 合规

站点响应头 `X-AIHOT-Commercial-Use: written-authorization-required`。产出页脚强制带
AIHOT 署名 + 商用授权提示；只落本地草稿，不自动写回飞书、不自动外发。

输入 inputs：
  - window            24h | 7d（默认 24h）
  - mode              selected（精选，默认）| all（全部公开池）
  - categories        ['ai-models','ai-products','industry','paper','tip'] 子集，空 = 全部
  - q                 盯题关键词（≥2 字，走站点检索）
  - limit             取多少条资讯（默认 40，1–100）
  - include_hot       是否带当前热点榜（默认 True）
  - expand_stories    展开前 N 个热点的事件时间线（默认 3，0–5）
  - use_daily         是否参考站点最新日报（默认 True）
  - instruction       自由要求（如「面向 IT 团队，标出可落地动作」）
  - force_refresh     忽略缓存强制重抓
  - skip_llm          只抓不编（调试）
  - render_html       是否生成 HTML 草稿（默认 True）
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re

from ..config import settings
from ..services import aihot
from .base import AgentContext, AgentResult, register_agent
from ..llm.prompts import build_aihot_news_prompt

_WINDOW_ZH = {"24h": "近 24 小时", "7d": "近 7 天"}
_MODE_ZH = {"selected": "AIHOT 精选（编辑打分筛过）", "all": "全部公开动态（未筛）"}
_MAX_STORIES = 5


# 归一化统一放在 services.aihot（页面接口与本 Agent 共用同一套口径）。
_short_dt = aihot.short_dt
_norm_item = aihot.news_item
_dedupe = aihot.dedupe_items


def _items_block(items: list[dict]) -> str:
    """喂给模型的编号清单。刻意不给 URL —— 模型没有 URL 可抄，也就编不出链接。"""
    lines = []
    for i, it in enumerate(items, start=1):
        head = "[%d] %s" % (i, it["title"])
        bits = [it["category_zh"], it["source_name"] or "来源未标注", it["published_zh"] or "时间未知"]
        if it.get("hot_rank"):
            bits.append("热点榜第 %d" % it["hot_rank"])
        lines.append(head + "\n    · " + " · ".join(b for b in bits if b))
        if it["summary"]:
            lines.append("    摘要：" + it["summary"][:400])
        if it["reason"]:
            lines.append("    站点点评：" + it["reason"][:400])
    return "\n".join(lines)


def _hot_block(hot: list[dict], stories: list[dict]) -> str:
    if not hot:
        return ""
    lines = ["", "# 当前热点榜（AIHOT 口径，rank 越小越热）"]
    for h in hot:
        lines.append("- 第 %s 名：%s（%s 个来源 / %s 条报道）"
                     % (h.get("rank"), (h.get("title") or "").strip(),
                        h.get("sourceCount") or "?", h.get("signalCount") or "?"))
    for s in stories:
        lines.append("")
        lines.append("## 事件时间线：%s" % s["title"])
        if s.get("digest"):
            lines.append("综述：" + s["digest"][:900])
        if s.get("latest"):
            lines.append("最新进展：" + s["latest"][:400])
        for t in (s.get("timeline") or [])[:8]:
            lines.append("- %s %s" % (t.get("at") or "", (t.get("title") or "")[:120]))
    return "\n".join(lines) + "\n"


def _daily_block(rep: dict) -> str:
    """站点日报只作「分栏与口径参照」，明确告诉模型不要照抄。"""
    secs = rep.get("sections") or []
    if not secs:
        return ""
    lines = ["", "# 站点当日日报的分栏参照（%s 期，仅供分栏与口径参考，不要照抄其措辞）"
             % (rep.get("date") or "")]
    for s in secs:
        titles = [((x.get("title") or "").strip())[:60] for x in (s.get("items") or [])[:4]]
        lines.append("- %s：%s" % (s.get("label") or "", "；".join(t for t in titles if t)))
    return "\n".join(lines) + "\n"


class AihotNewsAgent:
    id = "aihot-news"
    name = "AIHot 新闻简报"
    description = "拉取 AIHOT 公开 API 的 AI 资讯 / 热点榜 / 事件时间线 / 当日日报，编成一份带来源链接的内部简报。条目用编号引用回填链接，模型不产 URL。只读，不写回飞书。"
    writeback_allowed = False
    output_types = ["AI 简报", "HTML 页面"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        inputs = ctx.inputs or {}
        window = (inputs.get("window") or "24h").lower()
        if window not in _WINDOW_ZH:
            window = "24h"
        mode = (inputs.get("mode") or "selected").lower()
        if mode not in _MODE_ZH:
            mode = "selected"
        cats = [c for c in (inputs.get("categories") or []) if c in aihot.CATEGORY_CN]
        q = (inputs.get("q") or "").strip()
        limit = max(1, min(int(inputs.get("limit") or 40), 100))
        include_hot = bool(inputs.get("include_hot", True))
        expand = max(0, min(int(inputs.get("expand_stories") or 3), _MAX_STORIES))
        use_daily = bool(inputs.get("use_daily", True))
        force = bool(inputs.get("force_refresh"))

        await ctx.log("info", "抓取 AIHOT 资讯：%s · %s%s%s"
                      % (_WINDOW_ZH[window], _MODE_ZH[mode],
                         " · 分类 " + "/".join(aihot.CATEGORY_CN[c] for c in cats) if cats else "",
                         " · 盯题「%s」" % q if len(q) >= 2 else ""))

        # ── 并发拉取。分类是分开的请求（站点 category 只接受单值），一起并发。
        async def _items_for(cat: str):
            return await aihot.fetch_items(window=window, mode=mode, category=cat,
                                           q=q, limit=limit, force=force)

        jobs = [_items_for(c) for c in (cats or [""])]
        if include_hot:
            jobs.append(aihot.fetch_hot_topics(force=force))
        if use_daily:
            jobs.append(aihot.fetch_daily_latest(force=force))

        try:
            done = await asyncio.gather(*jobs, return_exceptions=True)
        except Exception as e:  # noqa: BLE001
            return AgentResult(task_id=ctx.task_id, status="failed",
                               error="抓取 AIHOT 失败：%s: %s" % (type(e).__name__, str(e)[:200]))

        n_cat = len(cats or [""])
        raw_items: list[dict] = []
        first_err: str = ""
        policy: dict = {}
        for res in done[:n_cat]:
            if isinstance(res, Exception):
                first_err = first_err or str(res)
                continue
            lst, meta = res
            policy = policy or (meta.get("policy") or {})
            raw_items.extend(lst)

        idx = n_cat
        hot: list[dict] = []
        if include_hot:
            res = done[idx]
            idx += 1
            if isinstance(res, Exception):
                await ctx.log("warn", "热点榜拉取失败（跳过）：%s" % str(res)[:120])
            else:
                hot, m = res
                policy = policy or (m.get("policy") or {})
        daily: dict = {}
        if use_daily:
            res = done[idx]
            if isinstance(res, Exception):
                await ctx.log("warn", "当日日报拉取失败（跳过）：%s" % str(res)[:120])
            else:
                daily, m = res
                policy = policy or (m.get("policy") or {})

        items = _dedupe([_norm_item(x) for x in raw_items if (x.get("title") or "").strip()])
        if not items:
            hint = ("；接口报错：" + first_err[:200]) if first_err else ""
            return AgentResult(
                task_id=ctx.task_id, status="failed",
                error="这个窗口没有取到任何资讯。可试：把窗口从 24h 改成 7d、"
                      "内容池从「精选」改成「全部」，或去掉盯题关键词%s" % hint)

        # 热点榜名次回填到对应条目上（同一 id 或同一原文链接），让模型知道哪条最热。
        hot_by_key = {}
        for h in hot:
            links = h.get("links") or {}
            for k in (h.get("id"), links.get("original"), links.get("aihot")):
                if k:
                    hot_by_key[str(k).lower()] = h
        matched: set[int] = set()
        for it in items:
            for k in (it["id"], it["url"], it["aihot_url"]):
                h = hot_by_key.get(str(k).lower()) if k else None
                if h:
                    it["hot_rank"] = h.get("rank")
                    it["story_url"] = it["story_url"] or (h.get("links") or {}).get("story") or ""
                    matched.add(id(h))
                    break

        # 热点榜里没落在 /items 窗口内的事件，补进编号清单。
        # 不补会出事：模型从「当前热点榜」段落读到了当天最重要的事，却没有编号可引用，
        # 清洗阶段就把这条要点整条丢掉——最该出现的新闻反而消失。实测 4 条内容因此被丢。
        added_hot = 0
        for h in hot:
            if id(h) in matched:
                continue
            links = h.get("links") or {}
            src = h.get("source") or {}
            names = h.get("sourceNames") or []
            items.append({
                "id": h.get("id") or "",
                "title": (h.get("title") or "").strip(),
                "original_title": "",
                "summary": "",
                "reason": "",
                "category": "", "category_zh": "热点事件",
                "source_name": (src.get("name") or (names[0] if names else "") or "").strip(),
                "url": links.get("original") or "",
                "aihot_url": links.get("aihot") or "",
                "story_url": links.get("story") or "",
                "published_at": h.get("latestAt") or "",
                "published_zh": _short_dt(h.get("latestAt") or ""),
                "score": None, "selected": False,
                "hot_rank": h.get("rank"),
                "source_count": h.get("sourceCount"),
            })
            added_hot += 1
        if added_hot:
            await ctx.log("info", "热点榜有 %d 个事件不在本窗口的资讯池里，已补进编号清单（否则模型无法引用它们）"
                          % added_hot)

        # 排序：热点榜在前（按 rank），其余按站点分数 → 时间
        items.sort(key=lambda it: (it.get("hot_rank") or 99,
                                   -(it.get("score") or 0),
                                   it.get("published_at") or ""), reverse=False)
        items = items[:limit]
        await ctx.log("info", "去重后 %d 条资讯%s%s"
                      % (len(items),
                         " · 热点 %d 条" % len(hot) if hot else "",
                         " · 已取当日日报（%s）" % daily.get("date") if daily else ""))

        # ── 展开热点事件时间线 ──
        stories: list[dict] = []
        if expand and hot:
            pids = []
            for h in hot[:expand]:
                pid = aihot.story_id_from_url((h.get("links") or {}).get("story") or "")
                if pid:
                    pids.append(pid)
            if pids:
                got = await asyncio.gather(*[aihot.fetch_story(p, force=force) for p in pids],
                                           return_exceptions=True)
                for res in got:
                    if isinstance(res, Exception):
                        continue
                    st, _m = res
                    stories.append(_norm_story(st))
                await ctx.log("info", "展开 %d 个热点事件的时间线" % len(stories))
                # 事件综述回填到对应条目的 summary：既让编号清单有实质内容可读，
                # 也让「引用体检」的内容比对有更多可比的文字（只靠标题容易判错）。
                by_story = {s["url"]: s for s in stories if s.get("url")}
                for it in items:
                    s = by_story.get(it.get("story_url") or "")
                    if s and not it.get("summary"):
                        it["summary"] = (s.get("digest") or s.get("latest") or "")[:600]

        # ── LLM 编简报 ──
        brief: dict = {}
        if inputs.get("skip_llm"):
            brief = {"skipped": True}
        else:
            system, user = build_aihot_news_prompt(
                today=dt.date.today().isoformat(),
                window_zh=_WINDOW_ZH[window], mode_zh=_MODE_ZH[mode],
                focus=("/".join(aihot.CATEGORY_CN[c] for c in cats) if cats else "综合")
                      + (" · 盯题「%s」" % q if len(q) >= 2 else ""),
                items=_items_block(items), n=len(items),
                hot_block=_hot_block(hot, stories), daily_block=_daily_block(daily),
                custom_instruction=inputs.get("instruction") or "",
            )
            await ctx.log("info", "调用 %s 编简报（%d 条素材）…" % (ctx.llm.text_model, len(items)))
            try:
                raw = await ctx.llm.text_complete(
                    user, system=system, json_mode=True, max_tokens=3600, timeout=240, retries=1,
                )
                brief = _clean_brief(_safe_parse_json(raw) or {}, items)
                await ctx.log("info", "简报完成：要点 %d · 分栏 %d · 跟踪 %d · 动作 %d"
                              % (len(brief.get("highlights") or []), len(brief.get("sections") or []),
                                 len(brief.get("watchlist") or []), len(brief.get("actions") or [])))
                if brief.get("refs_fixed") or brief.get("refs_dropped"):
                    await ctx.log("warn", "引用体检：%d 处编号指错已按内容改判，%d 条因完全对不上被丢弃"
                                  % (brief["refs_fixed"], brief["refs_dropped"]))
            except Exception as e:  # noqa: BLE001 —— 编不出也要把原始条目交出去
                await ctx.log("warn", "简报生成失败（不阻塞，原始条目照常输出）：%s: %s"
                              % (type(e).__name__, str(e)[:160]))
                brief = {"error": "%s: %s" % (type(e).__name__, str(e)[:160])}

        payload = {
            "source": "aihot",
            "source_url": (settings.aihot_base_url or "https://aihot.virxact.com").rstrip("/"),
            "window": window, "window_zh": _WINDOW_ZH[window],
            "mode": mode, "mode_zh": _MODE_ZH[mode],
            "categories": cats, "q": q,
            "policy": aihot.merge_policy(policy),
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "items": items,
            "hot_topics": [{"rank": h.get("rank"), "title": (h.get("title") or "").strip(),
                            "source_count": h.get("sourceCount"), "signal_count": h.get("signalCount"),
                            "story_url": (h.get("links") or {}).get("story") or "",
                            "url": (h.get("links") or {}).get("original") or ""} for h in hot],
            "stories": stories,
            "daily": {"date": daily.get("date") or "", "url": ((daily.get("links") or {}).get("aihot") or ""),
                      "sections": [(s.get("label") or "") for s in (daily.get("sections") or [])]},
            "brief": brief,
        }

        if inputs.get("render_html", True):
            try:
                path = _write_html(ctx.task_id, payload)
                payload["html_path"] = str(path)
                await ctx.log("info", "已生成 HTML 草稿：%s" % path.name)
                return AgentResult(task_id=ctx.task_id, status="done",
                                   result_path=str(path), payload=payload)
            except Exception as e:  # noqa: BLE001
                await ctx.log("warn", "HTML 渲染失败（数据仍可在结果页查看）：%s" % type(e).__name__)

        return AgentResult(task_id=ctx.task_id, status="done", payload=payload)


_norm_story = aihot.story_summary


# ── 输出清洗：refs 编号 → 真实条目（模型不碰 URL）─────────────────────

def _norm_txt(s: str) -> str:
    """归一化用于相似度比对：去标点空白、英文小写。"""
    return re.sub(r"[\s\W_]+", "", (s or "").lower())


def _tokens(s: str) -> set[str]:
    """抽「有辨识度的词」：≥4 字母的英文词 + ≥2 字的中文串。用于判断引用是否对得上。"""
    s = s or ""
    out = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9.\-]{3,}", s)}
    out |= {w for w in re.findall(r"[一-鿿]{2,}", s)}
    return out


def _sim(text: str, it: dict) -> float:
    """文本与某条资讯的贴合度 0–1。

    编号引用最容易出的错不是越界，而是**指错条目**（模型把 A 的说法配了 B 的编号）。
    越界能靠范围检查挡住，指错只能靠内容比对。

    两个方向都要看，只看一边会误判：
      fwd —— 这句话里的特征词有多少能在条目里找到（防止硬凑一个条目）
      rev —— 条目标题的特征词有多少出现在这句话里（防止「长句 vs 短标题」被判不相关，
              实测「Stripe 收购 OpenRouter 后续整合」对上标题「OpenRouter 宣布加入
              Stripe」时，只算 fwd 会因为分母太大而漏判）
    取 F1 与整串相似度里的较大值。
    """
    from difflib import SequenceMatcher
    body = (it.get("title") or "") + " " + (it.get("summary") or "")
    ratio = SequenceMatcher(None, _norm_txt(text)[:400], _norm_txt(body)[:400]).ratio()
    tt, bt, ht = _tokens(text), _tokens(body), _tokens(it.get("title") or "")
    fwd = (len(tt & bt) / len(tt)) if tt else 0.0
    rev = (len(tt & ht) / len(ht)) if ht else 0.0
    f1 = (2 * fwd * rev / (fwd + rev)) if (fwd + rev) else 0.0
    return max(ratio, f1)


# 低于这个贴合度就认为「这条引用对不上内容」；另一条要高出 _MARGIN 才敢改判。
_SIM_MIN = 0.34
_SIM_MARGIN = 0.12


def _clean_brief(p: dict, items: list[dict]) -> dict:
    n = len(items)
    repaired = {"fixed": 0, "dropped": 0}

    def _s(v, m=500) -> str:
        return str(v or "").strip()[:m]

    def _refs(v) -> list[int]:
        """把 refs 收敛成合法的 1..n 编号列表（去重、保序）。"""
        out: list[int] = []
        if isinstance(v, (int, float)):
            v = [v]
        for x in (v or [])[:8]:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= i <= n and i not in out:
                out.append(i)
        return out

    def _refs_for(text: str, v) -> list[int]:
        """范围校验 + 内容校验：引用对不上就换成真正匹配的那条。

        规则：算出全库最佳匹配 best；若 best 不在模型给的 refs 里、且明显更贴合，
        就把 best 提到最前面，并剔掉那些贴合度过低的编号。全都对不上就返回空
        （调用方会把这条内容整体丢掉——宁缺毋滥）。
        """
        refs = _refs(v)
        if not text or n == 0:
            return refs
        scores = [_sim(text, it) for it in items]
        best_i = max(range(n), key=lambda i: scores[i])
        best = scores[best_i]
        cur = max((scores[i - 1] for i in refs), default=0.0)
        keep = [i for i in refs if scores[i - 1] >= _SIM_MIN]
        if best >= _SIM_MIN and (best_i + 1) not in keep and best > cur + _SIM_MARGIN:
            keep = [best_i + 1] + keep
            repaired["fixed"] += 1
        if not keep:
            repaired["dropped"] += 1
        return keep[:6]

    highlights = []
    for it in (p.get("highlights") or [])[:8]:
        if isinstance(it, str):
            it = {"text": it, "refs": []}
        if not isinstance(it, dict):
            continue
        txt = _s(it.get("text"), 300)
        refs = _refs_for(txt, it.get("refs"))
        if txt and refs:      # 无编号支撑（或编号全对不上）的要点直接丢掉
            highlights.append({"text": txt, "refs": refs})

    sections = []
    for sec in (p.get("sections") or [])[:8]:
        if not isinstance(sec, dict):
            continue
        rows = []
        for it in (sec.get("items") or [])[:12]:
            if not isinstance(it, dict):
                continue
            title = _s(it.get("title"), 200)
            why = _s(it.get("why"), 400)
            # 标题最能定位条目；标题太泛时再把 why 一起参与比对。
            refs = _refs_for(title if len(title) >= 8 else (title + " " + why), it.get("refs"))
            if not (title and refs):
                continue
            rows.append({"title": title, "why": why, "refs": refs})
        if rows:
            sections.append({"label": _s(sec.get("label"), 40) or "其它", "items": rows})

    watchlist = []
    for it in (p.get("watchlist") or [])[:8]:
        if not isinstance(it, dict):
            continue
        topic = _s(it.get("topic"), 120)
        if topic:
            watchlist.append({"topic": topic, "state": _s(it.get("state"), 300),
                              "refs": _refs_for(topic + " " + _s(it.get("state"), 300),
                                                it.get("refs"))})

    return {
        "headline": _s(p.get("headline"), 160),
        "highlights": highlights,
        "sections": sections,
        "watchlist": watchlist,
        "actions": [_s(x, 260) for x in (p.get("actions") or [])[:8] if _s(x)],
        "caveats": [_s(x, 260) for x in (p.get("caveats") or [])[:8] if _s(x)],
        # 引用体检：fixed = 指错条目被改判的次数，dropped = 完全对不上而被丢弃的条数。
        "refs_fixed": repaired["fixed"],
        "refs_dropped": repaired["dropped"],
    }


def _safe_parse_json(text: str) -> dict | None:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


# ── HTML 草稿 ──────────────────────────────────────────────────────

def _ref_links(refs: list[int], items: list[dict]) -> str:
    """编号 → 「站内 / 原文」两个链接。链接只从 items 里取，模型无从插手。"""
    out = []
    for i in refs:
        if not (1 <= i <= len(items)):
            continue
        it = items[i - 1]
        a = []
        if it["aihot_url"]:
            a.append('<a href="%s" target="_blank" rel="noreferrer">AIHOT</a>' % aihot.esc(it["aihot_url"]))
        if it["url"]:
            a.append('<a href="%s" target="_blank" rel="noreferrer">原文</a>' % aihot.esc(it["url"]))
        label = aihot.esc(it["source_name"] or "来源")
        out.append('<span class="src">[%d] %s%s</span>' % (
            i, label, ("（" + " · ".join(a) + "）") if a else ""))
    return " ".join(out)


def _write_html(task_id: str, p: dict):
    esc = aihot.esc
    items = p["items"]
    b = p.get("brief") or {}
    body = []

    scope = "%s · %s" % (p["window_zh"], p["mode_zh"])
    if p["categories"]:
        scope += " · " + "/".join(aihot.CATEGORY_CN[c] for c in p["categories"])
    if p["q"]:
        scope += " · 盯题「%s」" % p["q"]

    if b.get("headline"):
        body.append('<div class="note"><b>%s</b></div>' % esc(b["headline"]))

    if b.get("highlights"):
        lis = ["<li>%s<br>%s</li>" % (esc(h["text"]), _ref_links(h["refs"], items))
               for h in b["highlights"]]
        body.append('<div class="card"><h2>今日要点<span class="n">%d 条</span></h2><ul>%s</ul></div>'
                    % (len(lis), "".join(lis)))

    for sec in b.get("sections") or []:
        lis = []
        for it in sec["items"]:
            lis.append("<li><b>%s</b><br>%s<br>%s</li>"
                       % (esc(it["title"]), esc(it["why"]), _ref_links(it["refs"], items)))
        body.append('<div class="card"><h2>%s<span class="n">%d 条</span></h2><ul>%s</ul></div>'
                    % (esc(sec["label"]), len(lis), "".join(lis)))

    if b.get("watchlist"):
        lis = ["<li><b>%s</b> — %s<br>%s</li>"
               % (esc(w["topic"]), esc(w["state"]), _ref_links(w["refs"], items))
               for w in b["watchlist"]]
        body.append('<div class="card"><h2>值得持续跟</h2><ul>%s</ul></div>' % "".join(lis))

    if b.get("actions"):
        body.append('<div class="card"><h2>建议动作</h2><ol>%s</ol></div>'
                    % "".join("<li>%s</li>" % esc(x) for x in b["actions"]))

    # 热点事件时间线（确定性渲染，来自 /stories）
    for s in p.get("stories") or []:
        rows = "".join(
            '<tr><td class="num">%s</td><td>%s</td><td>%s</td></tr>' % (
                esc(t["at"]),
                ('<a href="%s" target="_blank" rel="noreferrer">%s</a>' % (esc(t["url"]), esc(t["title"])))
                if t["url"] else esc(t["title"]),
                esc(t["source"]))
            for t in s["timeline"])
        inner = ""
        if s["digest"]:
            inner += "<p>%s</p>" % esc(s["digest"])
        if rows:
            inner += ('<div class="tw"><table><thead><tr><th class="num">时间</th><th>报道</th>'
                      "<th>来源</th></tr></thead><tbody>%s</tbody></table></div>" % rows)
        title = ('<a href="%s" target="_blank" rel="noreferrer">%s</a>' % (esc(s["url"]), esc(s["title"]))) \
            if s["url"] else esc(s["title"])
        body.append('<div class="card"><h2>事件时间线：%s<span class="n">%s 个来源 / %s 条报道</span></h2>%s</div>'
                    % (title, s["source_count"] or "?", s["report_count"] or "?", inner))

    if b.get("caveats"):
        body.append('<div class="card"><h2>不确定性与信息缺口</h2><ul>%s</ul></div>'
                    % "".join("<li>%s</li>" % esc(x) for x in b["caveats"]))
    if b.get("error"):
        body.append('<div class="card"><h2>简报未生成</h2><p class="src">%s</p>'
                    "<p>下面是本次抓到的全部原始条目，可直接阅读。</p></div>" % esc(b["error"]))

    # 全部原始条目（附录）：不管简报编得如何，原始素材都在，便于核对
    trs = []
    for i, it in enumerate(items, start=1):
        links = []
        if it["aihot_url"]:
            links.append('<a href="%s" target="_blank" rel="noreferrer">AIHOT</a>' % esc(it["aihot_url"]))
        if it["url"]:
            links.append('<a href="%s" target="_blank" rel="noreferrer">原文</a>' % esc(it["url"]))
        hot = ('<span class="pill p-md">热 %d</span> ' % it["hot_rank"]) if it.get("hot_rank") else ""
        trs.append('<tr><td class="num">%d</td><td>%s%s<div class="src">%s</div></td>'
                   "<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                   % (i, hot, esc(it["title"]), esc(it["summary"][:160]),
                      esc(it["category_zh"]), esc(it["source_name"] or "—"),
                      esc(it["published_zh"] or "—"), " · ".join(links) or "—"))
    body.append('<div class="card"><h2>本次抓到的原始条目<span class="n">%d 条 · 编号与上文引用一致</span></h2>'
                '<div class="tw"><table><thead><tr><th class="num">#</th><th>标题 / 摘要</th>'
                "<th>分类</th><th>来源</th><th>时间</th><th>链接</th></tr></thead>"
                "<tbody>%s</tbody></table></div></div>" % (len(items), "".join(trs)))

    daily = p.get("daily") or {}
    extra = ("正文每条均以编号回链到 AIHOT 站内页与原文；简报由本机模型基于这些条目编写，"
             "不引入条目之外的信息。")
    if b.get("refs_fixed") or b.get("refs_dropped"):
        extra += ("本次引用体检：<b>%d</b> 处编号指错已按内容自动改判、<b>%d</b> 条因与任何条目都对不上被丢弃。"
                  % (b.get("refs_fixed") or 0, b.get("refs_dropped") or 0))
    if daily.get("date"):
        extra += "站点当日日报（%s）仅用作分栏参照" % esc(daily["date"])
        if daily.get("url"):
            extra += '：<a href="%s" target="_blank" rel="noreferrer">%s</a>' % (
                esc(daily["url"]), esc(daily["url"]))
        extra += "。"
    footer = aihot.attribution_footer(p.get("policy") or {}, source_url=p["source_url"], extra=extra)
    subtitle = "%s · 生成于 %s · 由 %s 编写" % (esc(scope), esc(p["generated_at"]), esc(settings.text_model))
    html = aihot.render_page(title="AI 新闻简报（AIHOT）", subtitle=subtitle,
                             body="\n".join(body), footer=footer)
    path = settings.draft_path / ("%s.html" % task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


register_agent(AihotNewsAgent())
