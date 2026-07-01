"""会议纪要 / 妙记 Agent — Phase B 实现。

指向一段会议内容（妙记，或一篇会议记录文档），读正文 → LLM 结构化整理：
  1) 会议摘要
  2) 决策列表
  3) 行动项（负责人 / 截止 / 备注）
  4) 风险与阻塞

沿用「内容由飞书确定性抽取，LLM 只做语义整理」：转写 / 正文都是 lark-cli 拉的真文本，
模型不编造没出现的人名、日期与决策。完成后提交一份「会议纪要总结文档」写回提议
（create_doc，复用已验证的 docs +create 链路），经用户确认后沉淀回飞书。

输入 inputs：
  - asset_id / doc_token / token   妙记或文档 token（必填，支持飞书链接，自动抽 token）
  - asset_type                     'meeting' | 'docx' | 'doc' | 'wiki'（可选，缺省从索引查）
  - title                          覆盖标题（可选）
  - describe_images                True（默认）则对文档内嵌图片做 OCR + 图示并写入正文
  - max_images                     最多识别多少张内嵌图片（默认 0=全部）
  - skip_llm                       True 则只抽正文、跳过 LLM 整理（调试用）
"""
from __future__ import annotations

import json
import re

from .base import AgentContext, AgentResult, register_agent
from ..llm.prompts import build_meeting_minutes_prompt
from ..services import index_service, doc_images

# 飞书链接里的 token：/minutes/<tok>、/docx/<tok>、/wiki/<tok> 等，取最后一段长串。
_TOKEN_IN_URL = re.compile(r"/([A-Za-z0-9]{12,})(?:[?#/]|$)")

_DOC_TYPES = ("docx", "doc", "wiki")


def _resolve_token(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = _TOKEN_IN_URL.search(raw)
    return m.group(1) if m else raw


class MeetingMinutesAgent:
    id = "meeting-minutes"
    name = "会议纪要 Agent"
    description = "读取妙记转写或会议记录文档，整理出会议摘要、决策、行动项与风险。只读分析——沉淀文档 / 派任务 / 发群消息统一在「协作分发」完成。"
    # 只读：会议纪要不再单独写回。所有写飞书的动作（沉淀为文档 / 建任务 / 发群消息）
    # 统一集中到「协作分发」，结果页「分发 / 沉淀飞书」按钮一键带入。
    writeback_allowed = False

    async def run(self, ctx: AgentContext) -> AgentResult:
        inputs = ctx.inputs or {}
        raw_id = (inputs.get("asset_id") or inputs.get("doc_token") or inputs.get("token") or "").strip()
        token = _resolve_token(raw_id)
        if not token:
            return AgentResult(task_id=ctx.task_id, status="failed", error="缺少 asset_id（妙记或会议文档的 token / 链接）")

        # 索引里查元信息（标题 / 类型 / 空间 / 负责人 / url）
        meta = await _find_asset(token)
        asset_type = (inputs.get("asset_type") or (meta.get("type") if meta else "") or "").lower()
        title = (inputs.get("title") or (meta.get("title") if meta else "") or "").strip()
        space = (meta.get("space") if meta else "") or ""
        owner = (meta.get("owner") if meta else "") or ""
        url = (meta.get("url") if meta else "") or ""

        await ctx.log("info", f"开始整理会议「{title or token}」（类型 {asset_type or '未知'}）")

        # ── 读正文 ──
        try:
            content, source_type, extra = await _read_meeting_text(ctx, token, asset_type)
        except Exception as e:  # noqa: BLE001
            return AgentResult(task_id=ctx.task_id, status="failed",
                               error=f"读取会议内容失败：{type(e).__name__}: {str(e)[:200]}")
        if not (content or "").strip():
            return AgentResult(
                task_id=ctx.task_id, status="failed",
                error="未读到会议内容。妙记可能缺少转写读取权限，可改用「会议记录文档」，或在飞书把妙记转写导出为文档后再试。",
            )
        # 妙记元信息可能补全标题 / url
        if extra.get("title") and not title:
            title = extra["title"]
        if extra.get("url") and not url:
            url = extra["url"]

        # 文档正文里的内嵌图片（截图 / 白板 / 图表）做 OCR + 图示，回填进正文——
        # 与「HTML 生成」「PDF 识别」共用 doc_images 统一读图能力。妙记转写是纯文本，
        # 不含 <image> 标签，describe_inline_images 会零成本原样返回。
        figures: list[str] = []
        if inputs.get("describe_images", True):
            try:
                content, figures = await doc_images.describe_inline_images(
                    ctx, content, max_images=int(inputs.get("max_images", 0)),
                )
                if figures:
                    await ctx.log("info", f"已识别 {len(figures)} 张文档内嵌图片并写入正文")
                else:
                    await ctx.log("info", "未发现可识别的内嵌图片（本文档无图，跳过识图）。")
            except Exception as e:  # noqa: BLE001
                await ctx.log("warn", f"图片识别失败（不阻塞，继续整理正文）：{type(e).__name__}")

        await ctx.log("info", f"已读取会议内容（{source_type}），{len(content)} 字，调用 {ctx.llm.text_model} 整理 …")

        # ── LLM 结构化整理 ──
        llm_out: dict = {}
        if inputs.get("skip_llm"):
            llm_out = {"skipped": True}
        else:
            system, user = build_meeting_minutes_prompt(
                title=title, space=space, owner=owner, source_type=source_type, content=content,
            )
            try:
                rawtext = await ctx.llm.text_complete(
                    user, system=system, json_mode=True, max_tokens=2600, timeout=150, retries=1,
                )
                parsed = _safe_parse_json(rawtext) or {}
                llm_out = {
                    "summary": (parsed.get("summary") or "").strip(),
                    "attendees": _clean_str_list(parsed.get("attendees")),
                    "decisions": _clean_str_list(parsed.get("decisions")),
                    "action_items": _clean_actions(parsed.get("action_items")),
                    "risks": _clean_str_list(parsed.get("risks")),
                }
                await ctx.log(
                    "info",
                    f"整理完成：决策 {len(llm_out['decisions'])} · 行动项 {len(llm_out['action_items'])} · 风险 {len(llm_out['risks'])}",
                )
            except Exception as e:  # noqa: BLE001
                await ctx.log("warn", f"LLM 整理失败（不阻塞，仅展示正文预览）：{type(e).__name__}")
                llm_out = {"error": f"{type(e).__name__}: {str(e)[:160]}"}

        payload = {
            "asset_id": token,
            "title": title or "未命名会议",
            "url": url,
            "space": space,
            "owner": owner,
            "asset_type": asset_type or source_type,
            "source_type": source_type,
            "char_count": len(content),
            "duration_ms": extra.get("duration_ms") or "",
            "figures": figures,
            "summary": llm_out.get("summary", ""),
            "attendees": llm_out.get("attendees", []),
            "decisions": llm_out.get("decisions", []),
            "action_items": llm_out.get("action_items", []),
            "risks": llm_out.get("risks", []),
            "llm": {k: v for k, v in llm_out.items() if k in ("skipped", "error")},
            "content_preview": content[:4000],
        }

        # 只读：不再单独写回文档。结果页「分发 / 沉淀飞书」按钮带本任务进「协作分发」，
        # 在那里统一选择「沉淀为飞书文档 / 建任务 / 发群消息」并确认执行。
        return AgentResult(task_id=ctx.task_id, status="done", payload=payload)


# ── 读正文 ─────────────────────────────────────────────────────────

async def _read_meeting_text(ctx, token: str, asset_type: str) -> tuple[str, str, dict]:
    """返回 (正文文本, 来源描述, 附加元信息dict)。

    - 妙记(meeting)：先取转写正文；取不到再退回当文档读；都没有就抛错。
    - 文档(docx/doc/wiki)：直接导出 Markdown 正文。
    - 未知类型：先试文档导出，再试妙记。
    """
    if asset_type == "meeting":
        info = await ctx.lark.minutes_get_content(token)
        transcript = (info.get("transcript") or "").strip()
        if transcript:
            return transcript, "妙记转写", info
        await ctx.log("warn", "未取到妙记转写（可能缺少权限），尝试按文档读取 …")
        md = await _try_doc_export(ctx, token)
        if md:
            return md, "文档正文", info
        raise RuntimeError("妙记无转写、且无法按文档读取")

    if asset_type in _DOC_TYPES or not asset_type:
        md = await _try_doc_export(ctx, token)
        if md:
            return md, "文档正文", {}
        # 兜底：未知类型时再试妙记
        if not asset_type:
            info = await ctx.lark.minutes_get_content(token)
            transcript = (info.get("transcript") or "").strip()
            if transcript:
                return transcript, "妙记转写", info
        raise RuntimeError("文档正文为空")

    # 其它类型一律按文档导出试一次
    md = await _try_doc_export(ctx, token)
    if md:
        return md, "文档正文", {}
    raise RuntimeError(f"不支持的会议来源类型：{asset_type}")


async def _try_doc_export(ctx, token: str) -> str:
    try:
        md = await ctx.lark.docs_export_markdown(token)
        return (md or "").strip()
    except Exception:  # noqa: BLE001
        return ""


async def _find_asset(token: str) -> dict:
    for a in await index_service.list_assets(limit=2000):
        if a.get("asset_id") == token:
            return a
    return {}


# ── 输出清洗 ───────────────────────────────────────────────────────

def _clean_str_list(v) -> list[str]:
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        s = str(x).strip()
        if s:
            out.append(s)
    return out[:30]


def _clean_actions(v) -> list[dict]:
    if not isinstance(v, list):
        return []
    out: list[dict] = []
    for it in v:
        if not isinstance(it, dict):
            continue
        task = (it.get("task") or it.get("title") or "").strip()
        if not task:
            continue
        out.append({
            "task": task,
            "owner": (it.get("owner") or "").strip(),
            "due": (it.get("due") or "").strip(),
            "note": (it.get("note") or "").strip(),
        })
    return out[:40]


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


register_agent(MeetingMinutesAgent())
