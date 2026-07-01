"""文档内嵌图片的统一 OCR / 识别能力（跨场景共享）。

飞书在线文档（docx / wiki / doc）经 ``docs +fetch`` 导出的 Markdown 里，内嵌图片以
``<image token="xxx" width=.. height=.. align=../>`` 标签内联——只有图片的 media token，
没有像素。本模块把这些标签**就地**替换成图片的文字内容：

  1. 抽出全部 ``<image token=.../>`` 的 token（按文档顺序，去重保序）
  2. 逐张 ``docs +media-download`` 下载 → base64 data URL
  3. 调视觉模型（``vision_describe``）做 **OCR + 图示说明**：
     图里有文字/表格/数据就逐字转写，是图表/示意图就概括关键数据与结论
  4. 把标签替换为 ``> 🖼 图示：<内容>``，让后续的文本模型（摘要 / 重组 / 整理）
     能"看到"图里的信息

「PDF 识别」走的是另一条路（渲染整页 → 视觉），但 prompt 思路一致：能转写就转写，
是图就描述。本模块服务于「会议纪要」「HTML 生成」等**读在线文档正文**的场景，
让它们与 PDF 一样具备读图能力。

设计为**幂等且廉价**：正文里没有 ``<image>`` 标签（如妙记转写纯文本）时立即原样返回，
调用方可无条件调用。
"""
from __future__ import annotations

import asyncio
import base64
import re
import shutil
from pathlib import Path

from ..config import settings

# 文档正文里内嵌图片的形式：<image token="xxx" width="W" height="H" align="center"/>
IMG_TAG_RE = re.compile(r'<image\b[^>]*?token="([^"]+)"[^>]*?/?>', re.IGNORECASE)

# 统一的 OCR + 图示 prompt：既转写文字（含表格/数字），又能概括图表——与 PDF 识别的
# OCR 思路一致，覆盖"截图里有文字/表格"和"纯图表"两类。
OCR_DESCRIBE_PROMPT = (
    "你是企业文档助手，请识别这张文档内嵌图片：\n"
    "- 若图中含文字、表格或数据（如截图、报表、流程清单），请**逐字转写**其中的文字，"
    "表格按「列1 | 列2 | …」逐行列出，不要省略或概括金额、数量、日期、编号；\n"
    "- 若为图表 / 架构图 / 示意图，用简洁中文概括其类型与关键数据或结论。\n"
    "不要翻译、不要寒暄，直接输出内容。"
)


async def describe_inline_images(
    ctx,
    markdown: str,
    *,
    max_images: int = 0,
    concurrency: int = 4,
    prompt: str | None = None,
    max_tokens: int = 1200,
) -> tuple[str, list[str]]:
    """识别 Markdown 正文里的内嵌图片，并把识别结果回填进正文。

    参数：
      - ``max_images``：最多识别多少张；``<=0`` 表示全部。超出部分标签替换为「（图片）」占位。
      - ``concurrency``：视觉调用并发数（默认 4）。
      - ``prompt`` / ``max_tokens``：视觉 prompt 与输出上限，缺省走 OCR+图示通用 prompt。

    返回 ``(新markdown, 按文档顺序排列的识别结果列表)``。
    正文中没有 ``<image>`` 标签时原样返回 ``(markdown, [])``（零成本）。
    """
    if not markdown:
        return markdown or "", []

    tokens: list[str] = []
    seen: set[str] = set()
    for m in IMG_TAG_RE.finditer(markdown):
        tok = m.group(1)
        if tok not in seen:
            seen.add(tok)
            tokens.append(tok)
    if not tokens:
        return markdown, []

    total = len(tokens)
    targets = tokens if max_images <= 0 else tokens[:max_images]
    scope = "全部" if len(targets) == total else f"前 {len(targets)}/{total}"
    await ctx.log(
        "info",
        f"检测到 {total} 张内嵌图片，调用 {ctx.llm.vision_model} 识别{scope} {len(targets)} 张"
        f"（OCR + 图示，并发 {concurrency}，耗时较长）…",
    )
    if len(targets) < total:
        await ctx.log("info", f"为控制耗时，本次跳过其余 {total - len(targets)} 张图片（可调高 max_images）。")

    img_dir = settings.draft_path / f"_img_{ctx.task_id}"
    sem = asyncio.Semaphore(concurrency)
    use_prompt = prompt or OCR_DESCRIBE_PROMPT
    descriptions: dict[str, str] = {}

    async def _one(idx: int, token: str) -> None:
        async with sem:
            try:
                info = await ctx.lark.docs_download_media(token, img_dir, f"img{idx}")
                raw = Path(info["path"]).read_bytes()
                if not raw:
                    return
                b64 = base64.b64encode(raw).decode("ascii")
                data_url = f"data:{info.get('content_type', 'image/png')};base64,{b64}"
                desc = await ctx.llm.vision_describe(data_url, use_prompt, max_tokens=max_tokens)
                desc = (desc or "").strip()
                if desc and "失败" not in desc and "mock" not in desc.lower():
                    descriptions[token] = desc
            except Exception as e:  # noqa: BLE001
                await ctx.log("warn", f"图片 {token[:8]}… 识别失败：{type(e).__name__}")

    await asyncio.gather(*[_one(i, t) for i, t in enumerate(targets)])

    def _repl(m: re.Match) -> str:
        d = descriptions.get(m.group(1))
        return f"\n\n> 🖼 图示：{d}\n\n" if d else "（图片）"

    new_md = IMG_TAG_RE.sub(_repl, markdown)
    shutil.rmtree(img_dir, ignore_errors=True)
    # 按文档顺序返回（供「图示摘录」区忠实保留每张图要点）
    ordered = [descriptions[t] for t in targets if t in descriptions]
    return new_md, ordered
