"""内容生成 · PPT 输出。按图文密度分两条路：

- **按图还原**（图片为主）：会议展板照片 → 一张图一页还原 + 一页原图供核对。
  识别在 services/slide_rebuild，渲染在 services/pptx_render.render_deck。
- **重组成讲稿**（文字为主，内部 mode 名叫 outline）：整篇交给模型重组成 8–12 页，
  配 ≤6 张原文关键图，末尾列出没被用上的章节。
  重组在 services/talk_deck，渲染在 services/pptx_render.render_talk。
"""
from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

from .base import AgentContext, AgentResult, register_agent
from ..config import settings
from ..feishu import explain_error
from ..services import doc_images, doc_source
from ..services.pptx_render import DeckItem, render_deck, render_talk
from ..services.slide_rebuild import rebuild_slide
from ..services.talk_deck import build_talk, excerpt_sections, uncovered

# 图文密度阈值（字 / 张）。实测：图片型 58、59；文字型 509、4741 —— 两类之间
# 差一个数量级，阈值取在中间，不是拍的。
DENSITY_THRESHOLD = 150
MIN_IMAGES_FOR_RESTORE = 3


def pick_mode(chars: int, images: int) -> str:
    """按图文密度分流：restore（按图还原）| outline（重组成讲稿）。"""
    if images < MIN_IMAGES_FOR_RESTORE:
        return "outline"
    return "restore" if (chars / images) < DENSITY_THRESHOLD else "outline"


class SlidesPageAgent:
    id = "slides-page"
    name = "PPT 幻灯片生成"
    description = ("把文档里的会议展板照片还原成可编辑的 .pptx；一图一页，"
                   "另附一页原图供核对。只还原网格结构，箭头与图标不还原。")
    writeback_allowed = False

    async def run(self, ctx: AgentContext) -> AgentResult:
        inputs = ctx.inputs or {}
        token = (inputs.get("doc_token") or inputs.get("asset_id") or "").strip()
        if not token:
            return AgentResult(task_id=ctx.task_id, status="failed",
                               error="缺少 doc_token 参数")

        try:
            src = await doc_source.load_source(ctx, token, inputs)
        except Exception as e:  # noqa: BLE001
            return AgentResult(task_id=ctx.task_id, status="failed",
                               error=f"读取来源失败：{e}")

        md = src["markdown"]
        tokens = []
        seen: set[str] = set()
        for m in doc_images.IMG_TAG_RE.finditer(md):
            t = doc_images._img_token(m)
            if t and t not in seen:
                seen.add(t)
                tokens.append(t)
        text_chars = len(doc_images.IMG_TAG_RE.sub("", md))

        mode = (inputs.get("ppt_mode") or "auto").lower()
        if mode == "auto":
            mode = pick_mode(text_chars, len(tokens))
            density = text_chars / max(1, len(tokens))
            await ctx.log("info",
                          f"正文 {text_chars} 字 / 图片 {len(tokens)} 张 = {density:.0f} 字每张"
                          f" → {'按图还原' if mode == 'restore' else '重组成讲稿'}")

        if mode == "outline":
            return await self._run_talk(ctx, src)
        if not tokens:
            return AgentResult(task_id=ctx.task_id, status="failed",
                               error="这篇文档里没有可还原的图片")

        img_dir = settings.draft_path / f"_ppt_{ctx.task_id}"
        items: list[DeckItem] = []
        failures: list[str] = []
        try:
            for i, tok in enumerate(tokens, 1):
                await ctx.log("info", f"第 {i}/{len(tokens)} 张：下载并识别 …")
                try:
                    info = await ctx.lark.docs_download_media(tok, img_dir, f"img{i}")
                    photo = Path(info["path"])
                except Exception as e:  # noqa: BLE001
                    # 原因要翻出来，不能只写异常类名 —— 8.2 在 doc_images 里修过一模一样
                    # 的毛病，这里又写了一遍：十行「下载失败，已跳过：LarkCLIError」，
                    # 一个字的真实原因都没有。同一个原因只完整说一次，后面不刷屏。
                    why = explain_error(e)
                    await ctx.log("warn", f"第 {i} 张下载失败，已跳过："
                                          + (why if why not in failures else "原因同上"))
                    failures.append(why)
                    continue
                plan = await rebuild_slide(ctx, photo)
                items.append(DeckItem(plan=plan, photo=photo))

            if not items:
                top, n = Counter(failures).most_common(1)[0] if failures else ("未知原因", 0)
                return AgentResult(task_id=ctx.task_id, status="failed",
                                   error=f"{n}/{len(tokens)} 张图片都没能下载：{top}")

            out = settings.draft_path / f"{ctx.task_id}.pptx"
            render_deck(items, out)
        finally:
            # 原图已经画进 .pptx 了，临时目录不留 —— 会议照片是用户数据，
            # 没有理由在草稿目录里多存一份。
            shutil.rmtree(img_dir, ignore_errors=True)

        restored = sum(1 for it in items if it.plan.is_slide)
        await ctx.log("info", f"已生成 {out.name}：还原 {restored} 页 + 原图 {len(items)} 页")

        return AgentResult(
            task_id=ctx.task_id, status="preview", result_path=str(out),
            payload={"title": src["title"], "mode": mode,
                     "images": len(tokens), "restored": restored,
                     "slides": restored + len(items)},
        )


    async def _run_talk(self, ctx: AgentContext, src: dict) -> AgentResult:
        """文字型文档 → 讲稿。"""
        md = src["markdown"]
        await ctx.log("info", "文字为主的文档，交给模型重组成讲稿（整篇读一次，约 2–4 分钟）…")
        try:
            talk, sections, images = await build_talk(ctx, md, src["title"])
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            return AgentResult(task_id=ctx.task_id, status="failed",
                               error=msg if msg.startswith("讲稿生成失败") else f"讲稿生成失败：{msg}")
        await ctx.log("info", f"模型给出 {len(talk.slides)} 页讲稿（原文 {len(sections)} 节，"
                              f"{len(images)} 张带说明的图可选）")
        for d in talk.dropped:
            await ctx.log("warn", d)

        img_dir = settings.draft_path / f"_talk_{ctx.task_id}"
        local: dict[str, Path] = {}
        failures: list[str] = []
        try:
            for iid in dict.fromkeys(s.image for s in talk.slides if s.image):
                tok, _alt = images[iid]
                try:
                    info = await ctx.lark.docs_download_media(tok, img_dir, iid)
                    local[iid] = Path(info["path"])
                except Exception as e:  # noqa: BLE001
                    why = explain_error(e)
                    await ctx.log("warn", f"配图 {iid} 下载失败，该页不带图："
                                          + (why if why not in failures else "原因同上"))
                    failures.append(why)

            miss = uncovered(sections, talk.slides)
            if miss:
                await ctx.log("warn", f"有 {len(miss)} 节没有用进讲稿，已列在最后一页："
                                      + "、".join(s.heading for s in miss))
            out = settings.draft_path / f"{ctx.task_id}.pptx"
            render_talk(talk, local, [s.heading for s in miss], out,
                        appendix_notes=excerpt_sections(miss))
        finally:
            # 配图已经画进 pptx，临时目录不留 —— 同按图还原那边的理由
            shutil.rmtree(img_dir, ignore_errors=True)

        pages = 1 + len(talk.slides) + (1 if miss else 0)
        await ctx.log("info", f"已生成 {out.name}：讲稿 {pages} 页，配图 {len(local)} 张")
        return AgentResult(
            task_id=ctx.task_id, status="preview", result_path=str(out),
            payload={"title": talk.title or src["title"], "mode": "outline",
                     "slides": pages, "images": len(local), "uncovered": len(miss),
                     "sections": len(sections)},
        )


register_agent(SlidesPageAgent())
