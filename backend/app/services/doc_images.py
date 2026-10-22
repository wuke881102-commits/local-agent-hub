"""文档内嵌图片的统一 OCR / 识别能力（跨场景共享）。

飞书在线文档（docx / wiki / doc）经 ``docs +fetch`` 导出的正文里，内嵌图片只留下
media token，没有像素。本模块把这些标记**就地**替换成图片的文字内容：

  1. 抽出全部图片标记的 token（按文档顺序，去重保序）
  2. 逐张 ``docs +media-download`` 下载 → base64 data URL
  3. 调视觉模型（``vision_describe``）做 **OCR + 图示说明**：
     图里有文字/表格/数据就逐字转写，是图表/示意图就概括关键数据与结论
  4. 把标签替换为 ``> 🖼 图示：<内容>``，让后续的文本模型（摘要 / 重组 / 整理）
     能"看到"图里的信息

「PDF 识别」走的是另一条路（渲染整页 → 视觉），但 prompt 思路一致：能转写就转写，
是图就描述。本模块服务于「会议纪要」「HTML 生成」等**读在线文档正文**的场景，
让它们与 PDF 一样具备读图能力。

设计为**幂等且廉价**：正文里没有图片标记（如妙记转写纯文本）时立即原样返回，
调用方可无条件调用。
"""
from __future__ import annotations

import asyncio
import base64
import re
import shutil
from pathlib import Path

from ..config import settings
from ..feishu import explain_error

# 内嵌图片的写法随 lark-cli 版本变过，四种都要认：
#   v1（≤1.0.48）                 <image token="xxx" width=.. height=.. align=../>
#   v2 --doc-format markdown      <img name=".." alt=".." src="xxx"/>     src 就是 file_token
#   v2 im-markdown（URL 形）       ![alt](https://feishu.cn/file/xxx)      token 在 URL 末段
#   v2 im-markdown（裸 token 形）  ![alt](xxx)                             括号里直接就是 token
#
# **最后一种是实测出来的，不是照文档抄的**：同一篇文档里两种形状混着出现
# （43 个 URL 形 + 13 个裸 token 形）。只认 URL 形的话，那 13 张会被当成外站图片
# 一声不吭地跳过 —— 日志上看什么都正常，只是少了 13 张图的内容。
#
# 裸 token 形收得很紧：不含 / 和 . 的 20 位以上串才算，所以外站图片
# （![x](https://example.com/a.png)）和相对路径都不会被误伤。
IMG_TAG_RE = re.compile(
    r'<image\b[^>]*?\btoken="([^"]+)"[^>]*?/?>'
    r'|<img\b[^>]*?\bsrc="([^"]+)"[^>]*?/?>'
    r'|!\[([^\]]*)\]\('
    r'\s*(?:(https?://[^)\s]*/file/[A-Za-z0-9_-]+)[^)]*'
    r'|([A-Za-z0-9_-]{20,})\s*)\)',
    re.IGNORECASE,
)
_FILE_URL_RE = re.compile(r"/file/([A-Za-z0-9_-]+)")
_ALT_RE = re.compile(r'\balt="([^"]*)"', re.IGNORECASE)


def _img_token(m: re.Match) -> str:
    """从一处图片标记里取 file_token；取不到返回空串（调用方原样保留这段正文）。"""
    raw = m.group(1) or m.group(2) or m.group(4) or m.group(5) or ""
    hit = _FILE_URL_RE.search(raw)
    if hit:
        return hit.group(1)
    return "" if "://" in raw else raw


def _img_alt(m: re.Match) -> str:
    """飞书自带的图片描述。v2 两种写法都带，v1 没有。"""
    if m.group(3) is not None:
        return m.group(3).strip()
    hit = _ALT_RE.search(m.group(0))
    return hit.group(1).strip() if hit else ""

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
    正文中没有图片标记时原样返回 ``(markdown, [])``（零成本）。
    """
    if not markdown:
        return markdown or "", []

    tokens: list[str] = []
    seen: set[str] = set()
    for m in IMG_TAG_RE.finditer(markdown):
        tok = _img_token(m)
        if tok and tok not in seen:
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

    failures: list[Exception] = []

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
                failures.append(e)

    await asyncio.gather(*[_one(i, t) for i, t in enumerate(targets)])

    # 失败不再逐张刷屏，而是**按原因归并**：一篇文档几十张图时，原先会打出几十行
    # 一模一样的「识别失败：LarkCLIError」——既盖满日志，又一个字的真实原因都没有
    # （异常类名不是原因，原因在 stderr 里，explain_error 才认得）。
    if failures:
        grouped: dict[str, int] = {}
        for err in failures:
            why = explain_error(err)
            grouped[why] = grouped.get(why, 0) + 1
        for why, n in sorted(grouped.items(), key=lambda kv: -kv[1]):
            await ctx.log("warn", f"{n}/{len(targets)} 张图片没能识别：{why}")
        if any(_img_alt(m) for m in IMG_TAG_RE.finditer(markdown)):
            await ctx.log("info", "这些图片改用文档里自带的图片描述，正文不会缺这一段。")

    def _repl(m: re.Match) -> str:
        tok = _img_token(m)
        if not tok:
            return m.group(0)  # 认不出 token（如外站图片），原样留着，别把内容吃掉
        d = descriptions.get(tok)
        if d:
            return f"\n\n> 🖼 图示：{d}\n\n"
        # 没识别成（下载/视觉失败，或超出 max_images）时，退回飞书自带的图片描述。
        # v2 的正文里这段 alt 本来就有，扔掉换成「（图片）」是白丢信息。
        alt = _img_alt(m)
        return f"\n\n> 🖼 图示（文档自带描述）：{alt}\n\n" if alt else "（图片）"

    new_md = IMG_TAG_RE.sub(_repl, markdown)
    shutil.rmtree(img_dir, ignore_errors=True)
    # 按文档顺序返回（供「图示摘录」区忠实保留每张图要点）
    ordered = [descriptions[t] for t in targets if t in descriptions]
    return new_md, ordered
