"""本地内容生产 Agent（读图 + 读文档）。

输入：{ files | images: [本地文件绝对路径...], instruction?, title? }
支持类型：截图/图片（走视觉模型）、PDF / Word / Excel / PPT（确定性抽取文字后走文本模型）。
流程：
  1) 分流：图片 → base64 data URI；文档 → local_extract 抽取 Markdown
  2) 视觉/文本模型直出完整 HTML（套 Lumen-light 设计系统）
       - 仅图片 → 视觉模型读图
       - 仅文档 → 文本模型重组
       - 图文混合 → 视觉模型读图 + 把文档抽取文本一并喂入
  3) 写入 data/drafts/{task_id}.html，供本地预览 / 下载

只产出本地 HTML 草稿，不写回飞书。
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import mimetypes
from pathlib import Path

from ..config import settings
from ..html import get_renderer
from ..llm.prompts import build_image_page_prompt, build_html_freeform_prompt, build_html_page_prompt
from ..services import local_dir, local_extract
from .base import AgentContext, AgentResult, register_agent
from .html_page import _strip_html_fence, _safe_parse_json, _filter_real_metrics

_MAX_IMAGES = 8          # 一次最多读 8 张图，控制 token / 时延
_MAX_DOCS = 6            # 一次最多读 6 个文档
_MAX_EDGE = 1600         # 图片长边超过则等比缩小，省 token

_PAGE_TYPES = ("internal_wiki", "project", "announcement", "custom")
# 截图走视觉直出时，用页面模板做「版面定位」提示（与「内容生产·飞书来源」一致）。
_PAGE_TYPE_HINT = {
    "internal_wiki": "请输出一页内部知识页（专题 Wiki / 制度说明 / FAQ 风格）。",
    "project": "请输出一页项目展示页（项目介绍 / 阶段汇报 / 指标看板风格）。",
    "announcement": "请输出一页公告 / 活动页（活动方案 / 公告 / 通知风格）。",
    "custom": "",
}


def _to_data_uri(path: Path) -> str | None:
    """读单张图片 → data URI；过大则用 Pillow 等比缩小。失败返回 None。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    try:
        from PIL import Image  # noqa: PLC0415
        import io
        with Image.open(io.BytesIO(raw)) as im:
            w, h = im.size
            if max(w, h) > _MAX_EDGE:
                scale = _MAX_EDGE / float(max(w, h))
                im = im.convert("RGB").resize((int(w * scale), int(h * scale)))
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=85)
                raw = buf.getvalue()
                mime = "image/jpeg"
    except Exception:  # noqa: BLE001 —— 缩放失败就用原图
        pass
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


class LocalImageAgent:
    id = "local-image"
    name = "本地内容 Agent"
    description = "读懂本地目录里的截图与文档（PDF/Word/Excel/PPT），AI 重组为可预览的 HTML 页面。"
    writeback_allowed = False
    output_types = ["HTML 页面", "本地预览"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        inputs = ctx.inputs or {}
        raw_paths = inputs.get("files") or inputs.get("images") or []
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        images: list[Path] = []
        docs: list[Path] = []
        seen: set[str] = set()
        for p in raw_paths:
            fp = Path(str(p))
            key = str(fp)
            if key in seen or not fp.is_file():
                continue
            kind = local_dir.kind_of(fp.name)
            if not kind:
                continue
            seen.add(key)
            if kind == "image":
                images.append(fp)
            else:
                docs.append(fp)
        images = images[:_MAX_IMAGES]
        docs = docs[:_MAX_DOCS]
        if not images and not docs:
            return AgentResult(task_id=ctx.task_id, status="failed",
                               error="未提供有效的本地文件（截图或 PDF/Word/Excel/PPT）。")

        instruction = (inputs.get("custom_instruction") or inputs.get("instruction") or "").strip()
        page_type = (inputs.get("page_type") or "internal_wiki").strip()
        if page_type not in _PAGE_TYPES:
            page_type = "internal_wiki"
        # 版式：template（与飞书来源同一条 JSON→模板 渲染线，仅文档可用）| freeform（AI 直出）
        layout_mode = (inputs.get("layout_mode") or "freeform").strip().lower()
        if layout_mode not in ("template", "freeform"):
            layout_mode = "freeform"
        n_total = len(images) + len(docs)
        title = (inputs.get("title") or "").strip() or f"本地内容生成 · {n_total} 个文件"

        # 1) 图片 → data URI
        data_uris: list[str] = []
        if images:
            await ctx.log("info", f"读取 {len(images)} 张图片…")
            for fp in images:
                uri = await asyncio.to_thread(_to_data_uri, fp)
                if uri:
                    data_uris.append(uri)
                else:
                    await ctx.log("warn", f"图片读取失败，已跳过：{fp.name}")

        # 2) 文档 → 抽取 Markdown
        doc_md = ""
        if docs:
            await ctx.log("info", f"解析 {len(docs)} 个文档（PDF/Word/Excel/PPT）…")
            parts: list[str] = []
            for fp in docs:
                kind = local_dir.kind_of(fp.name) or ""
                md = await asyncio.to_thread(local_extract.extract_markdown, fp, kind)
                parts.append(f"# 文件：{fp.name}（{local_dir.KIND_LABELS.get(kind, kind)}）\n\n{md}")
            doc_md = "\n\n---\n\n".join(parts)

        if not data_uris and not doc_md.strip():
            return AgentResult(task_id=ctx.task_id, status="failed",
                               error="所有文件读取/解析失败，无可用内容。")

        # 3) 生成 HTML
        generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        docs_only = bool(doc_md.strip()) and not data_uris

        # 3a) 纯文档 + 套模板：复用「内容生产·飞书来源」完全相同的 JSON→模板 渲染线
        if docs_only and layout_mode == "template":
            try:
                system, user = build_html_page_prompt(
                    page_type=page_type, title=title, space="本地目录",
                    owner="—", updated="—", markdown=doc_md, custom_instruction=instruction,
                )
                await ctx.log("info", f"套模板：调用 {ctx.llm.text_model} 重组为结构化内容并渲染（页面模板：{page_type}）…")
                raw_text = await ctx.llm.text_complete(
                    user, system=system, json_mode=True, max_tokens=8192,
                    timeout=300, retries=1, model=ctx.llm.text_model,
                )
            except Exception as e:  # noqa: BLE001
                await ctx.log("warn", f"内容生成失败：{type(e).__name__}")
                return AgentResult(task_id=ctx.task_id, status="failed", error=f"本地内容生产失败：{e}")
            payload = _safe_parse_json(raw_text)
            if not payload:
                return AgentResult(task_id=ctx.task_id, status="failed",
                                   error="模型输出无法解析为 JSON", payload={"raw": (raw_text or "")[:400]})
            payload.setdefault("title", title)
            payload.setdefault("page_type", page_type)
            payload.setdefault("tags", [])
            payload["metrics"] = _filter_real_metrics(payload.get("metrics") or [], doc_md)
            meta = {
                "owner": "—", "source_space": "本地目录", "updated": "—",
                "agent_name": self.name, "template_name": page_type, "generated_at": generated_at,
                "refs": [{"title": fp.name, "url": "", "note": "本地文件"} for fp in docs],
                "vision_model": ctx.llm.vision_model,
            }
            html = get_renderer().render(page_type, payload, meta)
            draft_path = settings.draft_path / f"{ctx.task_id}.html"
            get_renderer().write(draft_path, html)
            await ctx.log("info", f"已写入草稿 {draft_path.name} ({len(html)} bytes，套模板)")
            return AgentResult(
                task_id=ctx.task_id, status="done", result_path=str(draft_path),
                payload={"title": title, "n_images": 0, "n_docs": len(docs),
                         "page_type": page_type, "layout_mode": "template", "generated_at": generated_at},
            )

        # 3b) 含截图 → 视觉直出；纯文档 + 自由版式 → 文本模型直出（均带页面模板定位）
        try:
            if data_uris:
                if layout_mode == "template":
                    await ctx.log("warn", "截图无法套确定性模板，已改用视觉直出（自由版式），页面模板作为版面定位。")
                hint = _PAGE_TYPE_HINT.get(page_type, "")
                merged = "\n".join(s for s in (hint, instruction) if s).strip()
                system, user = build_image_page_prompt(instruction=merged, n_images=len(data_uris))
                if doc_md.strip():
                    user += ("\n\n# 随附文档抽取内容（请与截图信息一并整合）\n"
                             + doc_md[:80000])
                await ctx.log("info", f"调用视觉模型 {ctx.llm.vision_model} 读图并直出 HTML（最长 ~5 分钟）…")
                raw = await ctx.llm.vision_complete(
                    data_uris, user, system=system, max_tokens=8192, timeout=300, retries=1,
                )
            else:
                # 纯文档 → 文本模型重组为自由版式 HTML（页面模板作为版面定位）
                system, user = build_html_freeform_prompt(
                    page_type=page_type, title=title, space="本地目录",
                    owner="—", updated="—", markdown=doc_md, custom_instruction=instruction,
                )
                await ctx.log("info", f"自由版式：调用文本模型 {ctx.llm.text_model} 重组文档并直出 HTML（最长 ~8 分钟）…")
                raw = await ctx.llm.text_complete(
                    user, system=system, json_mode=False, max_tokens=16000,
                    timeout=480, retries=0, model=ctx.llm.text_model,
                )
        except Exception as e:  # noqa: BLE001
            await ctx.log("warn", f"内容生成失败：{type(e).__name__}")
            return AgentResult(task_id=ctx.task_id, status="failed",
                               error=f"本地内容生产失败：{e}")

        html = _strip_html_fence(raw or "")
        if "<" not in html or "</" not in html.lower():
            import html as _h
            safe = _h.escape(raw or "")
            html = (f"<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
                    f"<title>{_h.escape(title)}</title></head>"
                    f"<body style=\"max-width:820px;margin:40px auto;padding:0 24px;"
                    f"font-family:-apple-system,'Segoe UI',Roboto,sans-serif;line-height:1.7;color:#1F2328\">"
                    f"<h1>{_h.escape(title)}</h1><pre style=\"white-space:pre-wrap\">{safe}</pre></body></html>")
            await ctx.log("warn", "模型未返回 HTML，已用纯文本兜底渲染。")

        draft_path = settings.draft_path / f"{ctx.task_id}.html"
        get_renderer().write(draft_path, html)
        await ctx.log("info", f"已写入草稿 {draft_path.name} ({len(html)} bytes)")
        return AgentResult(
            task_id=ctx.task_id, status="done", result_path=str(draft_path),
            payload={"title": title, "n_images": len(data_uris), "n_docs": len(docs),
                     "page_type": page_type, "layout_mode": "freeform", "generated_at": generated_at},
        )


register_agent(LocalImageAgent())
