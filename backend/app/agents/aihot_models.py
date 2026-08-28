"""AIHot 模型榜 Agent —— 把 AIHOT 大模型共识榜拉进本地，并给本机的选型建议。

流程（数字全部确定性计算，模型只做取舍判断）：
  1) services.aihot.fetch_leaderboard() 解析榜单页 SSR 数据 → 30 条真实条目
  2) 归一化成表格行：完全对齐榜单页的列（排名/模型/上线/完整度/输入价/输出价/共识分/可信度），
     再补两列本地口径：**性价比**（共识分 ÷ 混合价）与**名次区间**
  3) 把本机配置的三档模型（text_model / _fast / _best）在榜里做匹配 → 「你在用」标记
  4) LLM 只读这张表 + 当前配置，输出三档选型建议 / 换不换 / 值得盯的信号
  5) 确定性渲染 HTML 草稿（不让模型生成 HTML，杜绝写错数字和链接）

输入 inputs：
  - limit            取前 N 名（默认 30，1–100）
  - providers        只看这些厂商（列表，可空 = 全部）
  - focus            balanced | cost | quality | custom（选型侧重，默认 balanced）
  - instruction      focus=custom 时的自由要求（也可与其它 focus 叠加）
  - compare_local    是否对比本机配置的模型档位（默认 True）
  - in_out_ratio     性价比的输出:输入 token 假设（默认 3，即 1:3）
  - force_refresh    True 则忽略本地缓存强制重抓（默认 False）
  - skip_llm         True 则只出榜单不出建议（省 token / 调试）
  - render_html      是否生成 HTML 草稿（默认 True）

只读：不写回飞书。产出落 data/drafts/{task_id}.html。
"""
from __future__ import annotations

import datetime as dt
import json
import re

from ..config import settings
from ..services import aihot
from .base import AgentContext, AgentResult, register_agent
from ..llm.prompts import build_aihot_models_prompt

# 归一化 / 性价比 / 本机模型匹配统一放在 services.aihot，页面接口与本 Agent 共用。
_CONF_CN = aihot.CONF_CN
_CONF_CLS = {"HIGH": "p-hi", "MEDIUM": "p-md", "LOW": "p-lo"}
_FOCUS_ZH = {
    "balanced": "均衡（能力与成本兼顾）",
    "cost": "省钱优先（在够用的前提下压成本）",
    "quality": "能力优先（先要效果，成本次之）",
    "custom": "按用户自定义要求",
}
_TIER_ZH = {"balanced": "均衡档", "fast": "快省档", "best": "最强档"}
_norm_name = aihot.norm_model_name
_fmt_price = aihot.fmt_price


def _compact_table(rows: list[dict]) -> str:
    """喂给 LLM 的紧凑表（Markdown）。只给判断需要的列，省 token。"""
    head = ("| 名次 | 模型 | 厂商 | 共识分 | 名次区间 | 完整度 | 可信度 | 输入价 | 输出价 | 性价比 | 上线 |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|")
    lines = [head]
    for r in rows:
        span = ("%s–%s" % (r["rank_from"], r["rank_to"])) if r["rank_from"] else "—"
        comp = ("%.1f%%" % r["completeness"]) if r["completeness"] is not None else "—"
        val = ("%.2f" % r["value"]) if r["value"] else "—"
        lines.append("| %s | %s | %s | %.1f | %s | %s | %s | %s | %s | %s | %s |" % (
            r["rank"], r["name"], r["provider"], r["score"], span, comp,
            _CONF_CN.get(r["confidence"], r["confidence"] or "—"),
            _fmt_price(r, "price_in"), _fmt_price(r, "price_out"), val, r["released"] or "—",
        ))
    return "\n".join(lines)


def _current_block(local: list[dict]) -> str:
    lines = []
    for m in local:
        if m["on_board"]:
            lines.append("- %s档（%s）= `%s` → 榜内第 %s 名，共识分 %.1f，可信度 %s。用途：%s"
                         % (m["tier"], m["setting"], m["model"], m["rank"], m["score"],
                            _CONF_CN.get(m["confidence"], "—"), m["usage"]))
        else:
            lines.append("- %s档（%s）= `%s` → **未上榜**（榜单里没有这个型号）。用途：%s"
                         % (m["tier"], m["setting"], m["model"], m["usage"]))
    return "\n".join(lines) or "（未配置）"


class AihotModelsAgent:
    id = "aihot-models"
    name = "AIHot 模型榜"
    description = "拉取 AIHOT 大模型共识榜（30 名，含共识分/证据可信度/官方价），本地算性价比，并对照本机三档模型给选型建议。只读，不写回飞书。"
    writeback_allowed = False
    output_types = ["模型榜单", "选型建议", "HTML 页面"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        inputs = ctx.inputs or {}
        limit = max(1, min(int(inputs.get("limit") or 30), 100))
        providers = [str(p).strip().lower() for p in (inputs.get("providers") or []) if str(p).strip()]
        focus = (inputs.get("focus") or "balanced").lower()
        if focus not in _FOCUS_ZH:
            focus = "balanced"
        ratio = float(inputs.get("in_out_ratio") or 3)
        compare_local = bool(inputs.get("compare_local", True))

        await ctx.log("info", "开始抓取 AIHOT 模型榜 …")
        try:
            lb = await aihot.fetch_leaderboard(force=bool(inputs.get("force_refresh")))
        except aihot.AihotError as e:
            return AgentResult(task_id=ctx.task_id, status="failed", error=str(e))

        entries = lb["entries"]
        meta = lb.get("meta") or {}
        cache_note = "命中本地缓存（%ss 前抓的）" % meta.get("age_s") if meta.get("cached") else "刚从站点抓取"
        await ctx.log("info", "解析到 %d 个模型；榜单更新于 %s，综合 %s 家公开榜单（%s）"
                      % (len(entries), lb.get("updated_label") or "未知",
                         lb.get("board_count") or "?", cache_note))

        rows_all = aihot.leaderboard_rows(entries, ratio)
        rows = rows_all
        if providers:
            rows = [r for r in rows if r["provider"].lower() in providers]
            await ctx.log("info", "按厂商筛选（%s）后剩 %d 条" % ("/".join(providers), len(rows)))
        rows = rows[:limit]
        if not rows:
            return AgentResult(task_id=ctx.task_id, status="failed",
                               error="按当前筛选条件没有任何模型（厂商名要与榜单一致，如 Anthropic / OpenAI / Alibaba）。")

        local = aihot.match_local_models(rows_all) if compare_local else []
        if local:
            on = [m for m in local if m["on_board"]]
            await ctx.log("info", "本机三档模型对照：%d/%d 在榜（%s）" % (
                len(on), len(local),
                "，".join("%s=%s 第%s名" % (m["tier"], m["model"], m["rank"]) for m in on) or "无"))

        # ── LLM 选型建议 ──
        advice: dict = {}
        if inputs.get("skip_llm"):
            advice = {"skipped": True}
        else:
            system, user = build_aihot_models_prompt(
                today=dt.date.today().isoformat(),
                updated=lb.get("updated_label") or "",
                boards=lb.get("board_count") or 0,
                focus_zh=_FOCUS_ZH[focus],
                table=_compact_table(rows),
                current=_current_block(local) if local else "（未开启对照）",
                n=len(rows),
                custom_instruction=inputs.get("instruction") or "",
            )
            await ctx.log("info", "调用 %s 生成选型建议 …" % ctx.llm.text_model)
            try:
                raw = await ctx.llm.text_complete(
                    user, system=system, json_mode=True, max_tokens=2400, timeout=180, retries=1,
                )
                parsed = _safe_parse_json(raw) or {}
                advice = _clean_advice(parsed, rows)
                await ctx.log("info", "建议生成完成：%d 档推荐 · %d 条换/留判断"
                              % (len(advice.get("picks") or []), len(advice.get("keep_or_switch") or [])))
            except Exception as e:  # noqa: BLE001 —— 建议失败不该让榜单也拿不到
                await ctx.log("warn", "选型建议生成失败（不阻塞，榜单照常输出）：%s: %s"
                              % (type(e).__name__, str(e)[:160]))
                advice = {"error": "%s: %s" % (type(e).__name__, str(e)[:160])}

        payload = {
            "source": "aihot",
            "source_url": lb.get("source_url") or "",
            "updated_label": lb.get("updated_label") or "",
            "board_count": lb.get("board_count") or 0,
            "fetched_at": meta.get("fetched_at_iso") or "",
            "cached": bool(meta.get("cached")),
            "cache_age_s": meta.get("age_s") or 0,
            "policy": aihot.merge_policy(meta.get("policy")),
            "focus": focus,
            "in_out_ratio": ratio,
            "total_on_board": len(entries),
            "rows": rows,
            "local_models": local,
            "advice": advice,
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


# ── 输出清洗 ───────────────────────────────────────────────────────

def _clean_advice(p: dict, rows: list[dict]) -> dict:
    """把模型输出收敛回可信范围：推荐的模型名必须在榜里，否则丢掉这条。"""
    known = {_norm_name(r["name"]): r["name"] for r in rows}

    def _s(v, n=400) -> str:
        return str(v or "").strip()[:n]

    picks = []
    for it in (p.get("picks") or [])[:6]:
        if not isinstance(it, dict):
            continue
        name = _s(it.get("model"), 80)
        real = known.get(_norm_name(name))
        if not real:      # 模型编了个表外型号 → 丢弃，宁缺毋滥
            continue
        tier = (it.get("tier") or "").lower()
        picks.append({
            "tier": tier if tier in _TIER_ZH else "balanced",
            "tier_zh": _TIER_ZH.get(tier, "推荐"),
            "model": real,
            "provider": _s(it.get("provider"), 60),
            "why": _s(it.get("why")),
            "tradeoff": _s(it.get("tradeoff")),
        })

    ks = []
    for it in (p.get("keep_or_switch") or [])[:6]:
        if not isinstance(it, dict):
            continue
        cur = _s(it.get("current"), 80)
        if not cur:
            continue
        verdict = (it.get("verdict") or "").lower()
        target = _s(it.get("target"), 80)
        if verdict == "switch" and not known.get(_norm_name(target)):
            verdict, target = "unknown", ""   # 目标不在榜上，判断不可信
        ks.append({
            "current": cur, "tier": _s(it.get("tier"), 20),
            "verdict": verdict if verdict in ("keep", "switch", "unknown") else "unknown",
            "target": known.get(_norm_name(target), "") if verdict == "switch" else "",
            "reason": _s(it.get("reason")),
        })

    return {
        "headline": _s(p.get("headline"), 160),
        "picks": picks,
        "keep_or_switch": ks,
        "watch": [_s(x, 220) for x in (p.get("watch") or [])[:8] if _s(x)],
        "caveats": [_s(x, 220) for x in (p.get("caveats") or [])[:8] if _s(x)],
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

def _ctx_label(tokens) -> str:
    """上下文窗口短标签。

    站点给的是真实 token 数，同一句「1M 上下文」有 1000000 / 1048576(2^20) /
    1050000 / 1310720 好几种写法。四舍五入到 2 位再去掉多余的 0，否则会印出
    `1.04858M` 这种没人想看的数（%g 按有效位数截断，不是按小数位）。
    """
    if not isinstance(tokens, (int, float)) or tokens <= 0:
        return "—"
    if tokens >= 1_000_000:
        return ("%.2f" % (tokens / 1_000_000)).rstrip("0").rstrip(".") + "M"
    if tokens >= 1000:
        return "%dK" % round(tokens / 1000)
    return str(int(tokens))


def _delta(r: dict) -> str:
    """名次变化。用 previous_rank 自己算，不依赖站点 rankChange 的正负约定。

    站点同时给了 previousRank 和 rankChange，但 2026-08 实测全榜 rankChange 都是 0
    （previousRank == rank），无法验证它「正数=上升还是下降」。previousRank 是无歧义的：
    名次数字变小就是上升。拿不到 previousRank 时才退回 rankChange 的绝对值。
    """
    prev, cur = r.get("previous_rank"), r.get("rank")
    if isinstance(prev, int) and isinstance(cur, int) and prev != cur:
        d = prev - cur
        if d > 0:
            return '<span class="pill p-hi">↑%d</span>' % d
        return '<span class="pill p-lo">↓%d</span>' % abs(d)
    d = r.get("rank_change") or 0
    if not d:
        return '<span class="src">—</span>'
    return '<span class="pill p-info">±%d</span>' % abs(d)


def _write_html(task_id: str, p: dict):
    esc = aihot.esc
    rows = p["rows"]
    mine = {r["name"] for m in (p.get("local_models") or []) if m["on_board"]
            for r in rows if r["name"] == m["board_name"]}

    # 榜单表：列与榜单页一致，末两列是本地口径（表头已标注）。
    trs = []
    for r in rows:
        name_cell = '<span class="mname">%s</span>' % esc(r["name"])
        if r["detail_url"]:
            name_cell = '<a href="%s" target="_blank" rel="noreferrer">%s</a>' % (
                esc(r["detail_url"]), name_cell)
        if r["name"] in mine:
            name_cell += ' <span class="pill p-mine">本机在用</span>'
        conf = r["confidence"]
        comp = ("%.1f%%" % r["completeness"]) if r["completeness"] is not None else "—"
        val = ("%.2f" % r["value"]) if r["value"] else "—"
        trs.append(
            "<tr>"
            f'<td class="rank">{r["rank"]:02d}</td>'
            f'<td>{name_cell}<div class="mprov">{esc(r["provider"])}</div></td>'
            f'<td>{esc(r["released"] or "—")}</td>'
            f'<td class="num">{comp}</td>'
            f'<td class="num">{esc(_fmt_price(r, "price_in"))}</td>'
            f'<td class="num">{esc(_fmt_price(r, "price_out"))}</td>'
            f'<td class="num"><b>{r["score"]:.1f}</b></td>'
            f'<td><span class="pill {_CONF_CLS.get(conf, "p-info")}">'
            f'{_CONF_CN.get(conf, conf or "—")}</span></td>'
            f'<td class="num">{val}</td>'
            f"<td>{_delta(r)}</td>"
            "</tr>")
    # 列序与榜单页一致（排名→模型→上线→完整度→输入→输出→共识分→可信度）；
    # 厂商作为小字放在模型名下面（同榜单页），末两列「性价比 / 变化」是本机加的。
    table = (
        '<div class="tw"><table><thead><tr>'
        "<th>排名</th><th>模型 / 厂商</th><th>上线日期</th>"
        '<th class="num">评测完整度</th><th class="num">输入成本</th>'
        '<th class="num">输出成本</th>'
        '<th class="num">AIHOT 共识分</th><th>证据可信度</th>'
        '<th class="num">性价比*</th><th>变化</th>'
        "</tr></thead><tbody>%s</tbody></table></div>" % "".join(trs))

    # 价格明细单独一张表：输出价 + 上下文 + 价格出处（原榜把出处放 tooltip 里）
    prs = []
    for r in rows:
        src = esc(r["price_source"] or "—")
        if r["price_source_url"]:
            src = '<a href="%s" target="_blank" rel="noreferrer">%s</a>' % (
                esc(r["price_source_url"]), src)
        prs.append(
            "<tr><td>%s</td><td class=\"num\">%s</td><td class=\"num\">%s</td>"
            "<td class=\"num\">%s</td><td class=\"num\">%s</td><td>%s</td><td>%s</td></tr>" % (
                esc(r["name"]), esc(_fmt_price(r, "price_in")), esc(_fmt_price(r, "price_out")),
                ("¥%.2f" % r["price_blended"]) if r["price_blended"] else "—",
                _ctx_label(r["context_tokens"]), src, esc(r["price_verified_at"] or "—"),
            ))
    price_table = (
        '<div class="tw"><table><thead><tr><th>模型</th><th class="num">输入价</th>'
        '<th class="num">输出价</th><th class="num">混合价*</th><th class="num">上下文</th>'
        "<th>价格出处</th><th>核验日期</th></tr></thead><tbody>%s</tbody></table></div>"
        % "".join(prs))

    body = ['<div class="note">下表 1–8 列照搬 AIHOT 榜单页口径；'
            "<b>性价比</b> 与 <b>混合价</b> 带 * 的两列是本机计算："
            "混合价 =（输入价 + %g×输出价）÷ %g（假设输入:输出 ≈ 1:%g），"
            "性价比 = 共识分 ÷ 混合价，即「每元买到多少分」。价格待核验的模型不参与该口径。</div>"
            % (p["in_out_ratio"], 1 + p["in_out_ratio"], p["in_out_ratio"])]

    body.append('<div class="card"><h2>模型榜<span class="n">前 %d 名 · 共识分排序</span></h2>%s</div>'
                % (len(rows), table))

    # 本机对照
    local = p.get("local_models") or []
    if local:
        lis = []
        for m in local:
            if m["on_board"]:
                lis.append("<li><b>%s档</b>（<code>%s</code>）= <code>%s</code> → 榜内 <b>第 %s 名</b>，"
                           "共识分 %.1f，性价比 %s，可信度 %s。<span class=\"src\">用途：%s</span></li>"
                           % (esc(m["tier"]), esc(m["setting"]), esc(m["model"]), m["rank"],
                              m["score"], ("%.2f" % m["value"]) if m["value"] else "—",
                              _CONF_CN.get(m["confidence"], "—"), esc(m["usage"])))
            else:
                lis.append("<li><b>%s档</b>（<code>%s</code>）= <code>%s</code> → "
                           '<span class="pill p-md">未上榜</span> '
                           "<span class=\"src\">用途：%s</span></li>"
                           % (esc(m["tier"]), esc(m["setting"]), esc(m["model"]), esc(m["usage"])))
        body.append('<div class="card"><h2>本机在用的模型<span class="n">来自 backend/.env</span></h2>'
                    "<ul>%s</ul></div>" % "".join(lis))

    # 选型建议
    a = p.get("advice") or {}
    if a.get("picks") or a.get("headline"):
        sec = []
        if a.get("headline"):
            sec.append("<p><b>%s</b></p>" % esc(a["headline"]))
        for it in a.get("picks") or []:
            sec.append("<h3>%s · %s <span class=\"src\">%s</span></h3><p>%s</p>"
                       "<p class=\"src\">代价：%s</p>"
                       % (esc(it["tier_zh"]), esc(it["model"]), esc(it["provider"]),
                          esc(it["why"]), esc(it["tradeoff"] or "—")))
        for it in a.get("keep_or_switch") or []:
            cls = {"keep": "p-hi", "switch": "p-md", "unknown": "p-info"}.get(it["verdict"], "p-info")
            vz = {"keep": "建议保留", "switch": "建议更换", "unknown": "无法判断"}[it["verdict"]]
            tgt = ("→ <code>%s</code> " % esc(it["target"])) if it["target"] else ""
            sec.append("<p><span class=\"pill %s\">%s</span> <code>%s</code>（%s档）%s— %s</p>"
                       % (cls, vz, esc(it["current"]), esc(it["tier"]), tgt, esc(it["reason"])))
        if a.get("watch"):
            sec.append("<h3>值得盯的信号</h3><ul>%s</ul>"
                       % "".join("<li>%s</li>" % esc(x) for x in a["watch"]))
        if a.get("caveats"):
            sec.append("<h3>这份建议的局限</h3><ul>%s</ul>"
                       % "".join("<li>%s</li>" % esc(x) for x in a["caveats"]))
        body.append('<div class="card"><h2>选型建议<span class="n">%s · 由 %s 生成</span></h2>%s</div>'
                    % (esc(_FOCUS_ZH.get(p["focus"], "")), esc(settings.text_model), "".join(sec)))
    elif a.get("error"):
        body.append('<div class="card"><h2>选型建议</h2><p class="src">本次未生成（%s）。'
                    "榜单数据不受影响。</p></div>" % esc(a["error"]))

    body.append('<div class="card"><h2>价格与上下文明细<span class="n">价格出处来自 AIHOT 核验</span></h2>%s</div>'
                % price_table)

    subtitle = ("AIHOT 共识榜 · 榜单更新于 %s · 综合 %s 家公开榜单 · 本机抓取 %s"
                % (esc(p["updated_label"] or "未知"), p["board_count"] or "?",
                   esc(p["fetched_at"] or "刚刚")))
    footer = aihot.attribution_footer(
        p.get("policy") or {}, source_url=p["source_url"],
        extra="共识分 / 完整度 / 可信度 / 官方价均为 AIHOT 口径；性价比与混合价为本机计算，"
              "口径见正文首段。榜单页未提供公开 API，本页数据由榜单页 SSR 数据解析得到。",
    )
    html = aihot.render_page(title="AIHOT 大模型排行榜", subtitle=subtitle,
                             body="\n".join(body), footer=footer)
    path = settings.draft_path / ("%s.html" % task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


register_agent(AihotModelsAgent())
