"""两段式把展板照片还原成 SlidePlan。**这个模块会打模型。**

为什么分两段（实测，不是推断）：单次识别整张照片会凭空发明文字 ——
「手动 / 流水线」被读成「手动 / 定时触发」，而且明确要求「看不清用 ? 代替」无效。
把同一栏切出来按原生分辨率识别后不再虚构，残留的是形近字，人一眼能认出。

切块用**视觉模型给的栏坐标**，几何等分只是退路。一开始判断反了：以为给坐标是
视觉模型的弱项，就按「几带几栏」等分。等分对不齐展板，是后来一连串杂质的根源 ——
顶部标题被切成竖条混进每一栏（'双' / '通道多Agent协' / '估系统'），相邻栏从重叠带
串味，而靠事后过滤文本根本补不回来（OCR 把「析」读成「折」，子串判定就失效）。

换成 Qwen-VL 之后坐标很准：两个型号独立给出的分栏外接框高度一致，肉眼复核也对得上，
而且它们给的栏顶在 0.33（等分用的 TOP_SKIP 是 0.18，必然切进标题带），栏宽也本来
就不相等（0.05–0.24 / 0.26–0.44 / 0.46–0.72 / 0.74–0.93）。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from pathlib import Path

from ..config import settings
from .pptx_render import Band, Block, Column, SlidePlan

log = logging.getLogger("services.slide_rebuild")

_KINDS = {"box", "group", "note", "callout"}

SKELETON_MAX_PX = 1600          # 骨架只需要看结构，缩到这个尺寸够用且快一倍
REGION_MAX_PX = 1600            # 切片按原生分辨率截出来后再限宽
REGION_CONCURRENCY = 4

OVERLAP = 0.10                  # 相邻切片的重叠比例
TOP_SKIP = 0.18                 # 顶部标题区不参与分栏切块

SKELETON_PROMPT = """这是一张会议展板/幻灯片的照片。请把它**还原成结构**，输出 JSON，不要任何解释文字。

{
  "is_slide": true,
  "title": "主标题",
  "subtitle": "副标题，没有就空字符串",
  "theme": "dark 或 light",
  "bands": [
    {"role": "flow 或 columns",
     "columns": [
       {"index": "00", "heading": "分栏标题",
        "box": [0.05, 0.33, 0.24, 0.89],
        "blocks": [
          {"kind": "box",     "label": "框里的主文字", "sub": "框里的小字", "accent": "blue"},
          {"kind": "group",   "label": "这一组的标题", "items": ["并列项1", "并列项2"]},
          {"kind": "note",    "label": "不带框的说明文字"},
          {"kind": "callout", "value": "↑80%", "label": "说明"}
        ]}
     ]}
  ]
}

规则：
- **box 是这一栏在整张图里的归一化坐标 [x0,y0,x1,y1]（0~1）**，只覆盖该栏的正文区域：不要包含顶部大标题、副标题、会场 logo，也不要和相邻栏重叠。这个坐标会被用来裁图做逐字识别，给准一点。
- bands 是**横向的带**。很多展板是两层：上面一条横向流程（role=flow），下面若干并列栏（role=columns）。看到这种结构一定要分成两条 band，不要压成一层。
- columns 按图里的视觉分栏，有几栏就几栏。
- blocks 顺序 = 图里从上到下。
- 这一段**只要结构**，文字给个大概即可，后面会分区重新逐字识别。
- 如果这张图不是幻灯片/展板（合影、随手拍的白板等），返回 {"is_slide": false, "title": "一句话说明这是什么"}。
- 只输出 JSON。"""

REGION_PROMPT = """这是一张展板照片里的一个区域。请**逐字转写**其中所有文字，
按从上到下的顺序，一行一个文字块。看不清的字用 ? 代替那个字，不要猜、不要补全、
不要润色。只输出文字，不要解释。"""


# ── 骨架解析 ──────────────────────────────────────────────────────

def _strip_fence(text: str) -> str:
    """模型时不时把整份回答裹进代码围栏。和提炼那条路是同一个毛病。"""
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    body = t.split("\n", 1)[1] if "\n" in t else ""
    return body.rsplit("```", 1)[0].strip()


def _has_content(text: str) -> bool:
    """有没有真字 —— 字母、数字或汉字。纯箭头、纯标点不算。"""
    return any(ch.isalnum() for ch in (text or ""))


def _block(d: dict) -> Block | None:
    """一个块。纯箭头的块返回 None，由调用方丢掉。"""
    kind = str(d.get("kind") or "box")
    if kind not in _KINDS:
        log.debug("未知 block kind %r，退成 box", kind)
        kind = "box"
    items = d.get("items")
    label = str(d.get("label") or "")
    value = str(d.get("value") or "")

    # 展板上框与框之间的流程箭头会被骨架读成 callout 的数值，8.3 装机版上
    # 画出来是「↑ / CWE / API 模型」。只有箭头、没有数字的不是指标，退成普通框；
    # 「↑80%」这种带数字的是真指标，箭头留着。
    if value and not _has_content(value):
        value = ""
        if kind == "callout":
            kind = "box"
    if not _has_content(label) and not value:
        return None             # 整块只剩一个箭头

    return Block(
        kind=kind,
        label=label,
        sub=str(d.get("sub") or ""),
        items=[str(x) for x in items] if isinstance(items, list) else [],
        value=value,
        accent=str(d.get("accent") or "plain"),
    )


def _box(v) -> tuple[float, float, float, float] | None:
    """解析归一化坐标，形状不对就返回 None（调用方退回几何等分）。"""
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(x) for x in v)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        return None
    return (x0, y0, x1, y1)


def _column(*, index: str, heading: str, box, blocks) -> Column:
    """建一个 Column，顺便把写进 heading 里的编号挪到 index。

    骨架有时给 heading = "01 Agentic BOT"，而渲染画的是「{index} {heading}」，
    直接用会显示成「00 01 Agentic BOT」。编号以 heading 里那个为准 ——
    它是照着展板上印的读的，比骨架自己排的序号靠谱。
    """
    m = re.match(r"^(\d{1,2})[\s　]+(.+)$", heading.strip())
    if m:
        index, heading = m.group(1), m.group(2).strip()
    return Column(index=index, heading=heading, box=box, blocks=blocks)


def parse_skeleton(raw: str) -> SlidePlan:
    """把骨架那一段的输出解析成 SlidePlan。解析不了就抛 ValueError。"""
    text = _strip_fence(raw)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"骨架输出不是 JSON：{text[:80]}")
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ValueError(f"骨架输出不是 JSON：{e}") from e

    bands: list[Band] = []
    for b in d.get("bands") or []:
        if not isinstance(b, dict):
            continue
        cols = [
            _column(index=str(c.get("index") or ""),
                    heading=str(c.get("heading") or ""),
                    box=_box(c.get("box")),
                    blocks=[b for b in (_block(x) for x in (c.get("blocks") or [])
                                        if isinstance(x, dict)) if b is not None])
            for c in (b.get("columns") or []) if isinstance(c, dict)
        ]
        bands.append(Band(role=str(b.get("role") or "columns"), columns=cols))

    return SlidePlan(
        is_slide=bool(d.get("is_slide", True)),
        title=str(d.get("title") or ""),
        subtitle=str(d.get("subtitle") or ""),
        theme=str(d.get("theme") or "dark"),
        bands=bands,
    )


# ── 几何切块 ──────────────────────────────────────────────────────

def crop_boxes(plan: SlidePlan, width: int, height: int
               ) -> list[tuple[int, int, int, int, int, int]]:
    """按骨架的「几带几栏」几何等分，返回 [(band_i, col_i, x0, y0, x1, y1), ...]。

    带在竖向等分（跳过顶部标题区），栏在横向等分，两个方向都外扩 OVERLAP ——
    骑在分界线上的字，不重叠就会两边都切不全。

    小图上 int() 截断可能让 x0 == x1，PIL.crop 会返回 0 宽的图并让后续识别静默
    返回空串，所以每条边至少留 1 像素。
    """
    out: list[tuple[int, int, int, int, int, int]] = []
    bands = plan.bands
    if not bands:
        return out

    # 视觉模型给了这一栏的坐标就照它裁。等分只是它给不出坐标时的退路 ——
    # 等分对不齐展板正是所有杂质的根源：TOP_SKIP 清不掉标题带（实测栏顶在 0.33
    # 而不是 0.18），等宽也切不对本来就不等宽的栏（实测 0.05–0.24 / 0.26–0.44 /
    # 0.46–0.72 / 0.74–0.93）。模型坐标已经避开标题和相邻栏，不再外扩重叠。
    for bi, band in enumerate(bands):
        if band.columns and all(c.box for c in band.columns):
            for ci, col in enumerate(band.columns):
                bx0, by0, bx1, by1 = col.box            # type: ignore[misc]
                ix0, iy0 = int(bx0 * width), int(by0 * height)
                ix1 = min(width, max(ix0 + 1, int(bx1 * width)))
                iy1 = min(height, max(iy0 + 1, int(by1 * height)))
                out.append((bi, ci, ix0, iy0, ix1, iy1))
    if out:
        return out

    top = int(height * TOP_SKIP)
    band_h = (height - top) / len(bands)
    for bi, band in enumerate(bands):
        n = len(band.columns)
        if n == 0:
            continue
        y0 = top + band_h * bi
        y1 = y0 + band_h
        pad_y = band_h * OVERLAP
        col_w = width / n
        pad_x = col_w * OVERLAP
        for ci in range(n):
            cx0 = col_w * ci
            cx1 = cx0 + col_w
            ix0 = max(0, int(cx0 - pad_x))
            iy0 = max(0, int(y0 - pad_y))
            ix1 = min(width, max(ix0 + 1, int(cx1 + pad_x)))
            iy1 = min(height, max(iy0 + 1, int(y1 + pad_y)))
            out.append((bi, ci, ix0, iy0, ix1, iy1))
    return out


# ── 分区转写与合并 ────────────────────────────────────────────────

def _data_url(img) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _norm(text: str) -> str:
    """比对用的归一化：去掉所有空白和常见标点，只留实字。

    骨架给的标题和块文字常常只差几个空格或全角标点，逐字比对判不出「是同一句」。
    """
    return re.sub(r"[\s　,.，。：:；;、·\-—_/|]+", "", text or "")


MIN_FRAGMENT_LEN = 2        # 比这还短的行不做子串判断，容易误伤
HEADING_MATCH = 0.75        # 与栏标题的相似度到这个程度就算标题回声
HEADING_MATCH_FIRST = 0.45  # 第一行专用：栏的切片本来就从标题开始，先验很强


def _similar(a: str, b: str) -> float:
    """两段文字的相似度 0~1。用字符集合的 Jaccard，不是编辑距离 ——
    这里要挡的是「读岔一两个字的同一句话」，顺序无关，集合比对够用且便宜。"""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _is_chrome(line: str, plan: SlidePlan, col: Column, *, first: bool = False) -> bool:
    """这一行是不是「版面装饰」而非内容 —— 标题碎片、栏编号、栏名。

    标题横跨整张图，按栏切块会把它切成竖条，每栏读出一小段，所以整行比对判不出来：
    实测「双通道多Agent协同的文档质量评估系统」被切成 '双' / '通道多Agent协' /
    'tt协同的文档质量评估系' / '估系统' 四段，分别成了四栏的第一个块。
    判据改成**子串**：碎片必然是标题的子串，而正经内容极少整段落在标题里。

    调 TOP_SKIP 不能解决这个 —— 那是对着一张照片调参，换个构图就又不对了。
    """
    t = _norm(line)
    if not t:
        return True
    if col.index and t.startswith(_norm(col.index)):
        # 「03」和「03　审核与可信输出」都算。用前缀而不是相等，是因为栏名会被
        # OCR 读岔（实测「可复核」→「可信」），按栏名比对就漏了，而编号是稳的。
        return True
    if re.fullmatch(r"\d{1,2}", t):
        # 光秃秃的编号。可能是相邻栏的编号从切片边缘带进来的，和本栏 index 对不上，
        # 所以不能只比 col.index。实测漏进来过 '02' / '03'。
        return True
    head = _norm(col.heading)
    threshold = HEADING_MATCH_FIRST if first else HEADING_MATCH
    if head and (head in t or _similar(t, head) >= threshold):
        # 「01  上下文资产化」这种连写，以及栏标题被 OCR 读岔一两个字的情况 ——
        # 实测「审核与可复核输出」被读成「审核与可信输出」，既没带编号也不是子串，
        # 于是它成了这一栏的第一个框。
        return True
    if len(t) < MIN_FRAGMENT_LEN:
        return True                              # 单字碎片，留着也没意义
    for whole in (_norm(plan.title), _norm(plan.subtitle)):
        if whole and t in whole:
            return True
    return False


def drop_title_echoes(plan: SlidePlan) -> None:
    """删掉只是把大标题/副标题又抄一遍的块。

    骨架经常把顶部的标题和副标题也当成内容块返回，渲染出来整页重复两遍 ——
    真文档跑出来才看见的。
    """
    echoes = {_norm(plan.title), _norm(plan.subtitle)} - {""}
    if not echoes:
        return
    for band in plan.bands:
        for col in band.columns:
            col.blocks = [b for b in col.blocks
                          if _norm(b.value or b.label) not in echoes]


def _merge_region_text(plan: SlidePlan, texts: dict[tuple[int, int], str]) -> None:
    """用分区转写的逐字结果覆盖骨架里的粗略文字。

    按行顺序对位：骨架给了这一栏有几个 block，转写给了这一栏从上到下的文字行。
    行数对不上时只覆盖能对上的部分 —— 宁可留着骨架那版粗文字，也不要错位填充，
    错位比不准更难看出来。
    """
    for bi, band in enumerate(plan.bands):
        for ci, col in enumerate(band.columns):
            lines = [x.strip() for x in (texts.get((bi, ci)) or "").split("\n") if x.strip()]
            if not lines:
                continue
            # **栏标题以骨架为准，这里不改。** 切块为了不漏边缘字是带重叠的，
            # 栏顶上方的大标题、会场 logo 都会被切进来，所以「第一行就是栏标题」
            # 这个假设根本不成立 —— 实测把「数据源接入」改成了「年度大会」，
            # 把「上下文资产化」改成了大标题的一个片段。骨架看的是整张图，
            # 栏标题恰恰是它最可靠的产出。
            #
            # 同理，切进来的标题行不该占掉块的位置，先滤掉。
            lines = [x for i, x in enumerate(lines)
                     if not _is_chrome(x, plan, col, first=(i == 0))]
            # 切片有重叠，同一句连着出现两次是常事，不该占两个框。
            lines = [x for i, x in enumerate(lines) if i == 0 or _norm(x) != _norm(lines[i - 1])]
            for block, line in zip(col.blocks, lines):
                block.label = line
                # 骨架那版的 sub / items 是低保真那一遍的产物，和这里的逐字结果
                # 并非一一对应。留着就会出现「标题来自 A、副题来自 B」——
                # 实测过一次「鉴权/限流」配着「小程序」，看起来完全合理，
                # 比识别不准糟得多。全文在备注里，不怕丢。
                block.sub = ""
                block.items = []


async def _skeleton(ctx, image_url: str) -> str:
    """跑骨架那一段，带一级降级。

    点名用 Qwen-VL 是因为要的是**版面坐标**，那是 grounding 模型的长处
    （逐字转写仍走主视觉模型，读字够用且快得多）。但这个点名不能是硬依赖：
    那几个 VL 模型在开通之前一直是 403，授权随时可能再被收回，网络也可能不通。
    硬依赖的后果是整篇任务被一个跟原因无关的错打死 —— 这个项目在这上面栽过太多次。

    降级：点名的 VL 模型 → 主视觉模型（拿不到坐标，切块退回几何等分）。
    两档都挂就返回空串，由调用方退成「只出原图页」。
    """
    named = (settings.slides_skeleton_model or "").strip()
    if named:
        try:
            return await ctx.llm.vision_describe(
                image_url, SKELETON_PROMPT, max_tokens=3000,
                model=named, provider=settings.slides_skeleton_provider)
        except Exception as e:  # noqa: BLE001
            from ..feishu import explain_error
            await ctx.log("warn", f"骨架模型 {named} 不可用，退回主视觉模型"
                                  f"（拿不到分栏坐标，还原质量会下降）：{explain_error(e)[:120]}")
    try:
        return await ctx.llm.vision_describe(image_url, SKELETON_PROMPT, max_tokens=3000)
    except Exception as e:  # noqa: BLE001
        await ctx.log("warn", f"骨架识别整个失败，这张图只出原图页：{type(e).__name__}: {e}")
        return ""


async def rebuild_slide(ctx, photo: Path) -> SlidePlan:
    """一张展板照片 → SlidePlan。失败时返回 is_slide=False 的兜底，不抛。"""
    from PIL import Image

    with Image.open(photo) as im:
        im = im.convert("RGB")
        full_w, full_h = im.size
        small = im.copy()
        small.thumbnail((SKELETON_MAX_PX, SKELETON_MAX_PX))

        raw = await _skeleton(ctx, _data_url(small))
        try:
            plan = parse_skeleton(raw or "")
        except ValueError as e:
            await ctx.log("warn", f"骨架识别失败，这张图退成原图页：{e}")
            return SlidePlan(is_slide=False, title="", notes=str(raw or "")[:2000])

        if not plan.is_slide:
            return plan

        boxes = crop_boxes(plan, full_w, full_h)
        if not boxes:
            return plan

        sem = asyncio.Semaphore(REGION_CONCURRENCY)
        texts: dict[tuple[int, int], str] = {}
        crops = {(bi, ci): im.crop((x0, y0, x1, y1)) for bi, ci, x0, y0, x1, y1 in boxes}

        async def one(key: tuple[int, int]) -> None:
            async with sem:
                crop = crops[key]
                crop.thumbnail((REGION_MAX_PX, REGION_MAX_PX))
                try:
                    out = await ctx.llm.vision_describe(_data_url(crop), REGION_PROMPT,
                                                        max_tokens=900)
                    texts[key] = (out or "").strip()
                except Exception as e:  # noqa: BLE001
                    texts[key] = ""
                    await ctx.log("warn",
                                  f"第 {key[0] + 1} 带第 {key[1] + 1} 栏识别失败："
                                  f"{type(e).__name__}")

        await asyncio.gather(*[one(k) for k in crops])

    _merge_region_text(plan, texts)
    drop_title_echoes(plan)
    plan.notes = "\n\n".join(
        f"【第 {bi + 1} 带 · 第 {ci + 1} 栏】\n{t}"
        for (bi, ci), t in sorted(texts.items()) if t
    )
    return plan
