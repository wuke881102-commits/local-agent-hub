"""文字型文档 → 讲稿。**这个模块会打模型**（只有 build_talk 打，其余都是纯函数）。

为什么是「重组成讲稿」而不是「一节一页」：实测章节层级因文档而异（一篇按二级分
18 节，另一篇二级全篇只有 2 个、实际按三级分 13 节），每节中位约 690 字而一页只放
得下约 150 字，原样拆会拆出七十页上下。所以整篇交给模型一次，重组成 8–12 页。

重组最大的风险是**悄悄漏内容** —— HTML 那条路的 prompt 里专门写着「不要遗漏整段
内容」，说明这是真实发生过的。兜底：给章节编号，要求每页标注引用了哪几节，生成后
把一页都没引用的章节列出来（日志 + 末尾一页「未收录章节」）。取舍摆在明面上。
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass

from . import doc_images
from .fact_check import filter_real_metrics, table_is_real
from .pptx_render import TALK_KINDS, Talk, TalkSlide

log = logging.getLogger("services.talk_deck")

MAX_BULLETS = 6
MAX_IMAGES = 6
MAX_METRICS = 4
MAX_TABLE_COLS = 6
MAX_TABLE_ROWS = 8
MIN_SLIDES = 3
CHUNK_CHARS = 1000            # 没有标题的文档按这个长度切块
MIN_PREAMBLE_CHARS = 20       # 首个标题前的内容少于这个就不单独成节（多半只是文档标题）
MIN_REPORT_CHARS = 50         # 比这还短的节不列进「未收录」，列了只是噪音
CAPTION_CHARS = 60

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass
class Section:
    id: str          # "S0"（前言）/ "S1" …
    heading: str
    text: str


# ── 切节 ──────────────────────────────────────────────────────────

def _pick_level(md: str) -> int | None:
    """这篇文档实际用哪一级标题分章节。

    取出现次数最多的那一级（只看 1–4 级、至少出现 2 次），并列取更高一级。
    不能写死二级：实测一篇规划类文档二级全篇只有 2 个，真正的章节是三级 ——
    按二级切只会得到两个巨块，覆盖检查也就没有意义了。
    """
    levels = Counter(len(m.group(1)) for line in md.split("\n")
                     if (m := _HEADING.match(line)) and len(m.group(1)) <= 4)
    cands = {lv: n for lv, n in levels.items() if n >= 2}
    if not cands:
        return None
    return min(cands, key=lambda lv: (-cands[lv], lv))


def _chunks(md: str) -> list[Section]:
    """没有可用标题时按段落切块，让模型有东西可引用。"""
    out: list[Section] = []
    buf: list[str] = []
    size = 0

    def flush():
        nonlocal buf, size
        if buf:
            text = "\n\n".join(buf).strip()
            k = len(out) + 1
            out.append(Section(f"S{k}", f"第 {k} 段：{text[:12]}", text))
        buf, size = [], 0

    for para in re.split(r"\n\s*\n", md):
        p = para.strip()
        if not p:
            continue
        if size and size + len(p) > CHUNK_CHARS:
            flush()
        buf.append(p)
        size += len(p)
    flush()
    return out


def split_sections(md: str) -> list[Section]:
    """按这篇文档自己的章节层级切节并编号。首个章节标题前的有效内容成为 S0。"""
    if not (md or "").strip():
        return []
    level = _pick_level(md)
    if level is None:
        return _chunks(md)

    out: list[Section] = []
    pre: list[str] = []
    cur_head, cur_lines = None, []
    for line in md.split("\n"):
        m = _HEADING.match(line)
        if m and len(m.group(1)) == level:
            if cur_head is not None:
                out.append(Section(f"S{len(out) + 1}", cur_head, "\n".join(cur_lines).strip()))
            # 标题里的 **加粗** 之类要去掉：它会原样出现在「未收录章节」页和提示词里。
            cur_head, cur_lines = _inline(m.group(2)) or m.group(2).strip(), []
        elif cur_head is None:
            pre.append(line)
        else:
            cur_lines.append(line)
    if cur_head is not None:
        out.append(Section(f"S{len(out) + 1}", cur_head, "\n".join(cur_lines).strip()))

    # 前言：去掉标题行后还剩有效内容才单独成节
    body = "\n".join(x for x in pre if not _HEADING.match(x)).strip()
    if len(body) >= MIN_PREAMBLE_CHARS:
        out.insert(0, Section("S0", "前言", body))
    return out


# ── 图片编号 ──────────────────────────────────────────────────────

def mark_images(md: str) -> tuple[str, dict[str, tuple[str, str]]]:
    """把图片标记换成模型读得懂、又能引用的短标记。返回 (新正文, {编号: (token, 说明)})。

    只给**有说明文字**的图编号：模型不可能凭一个 token 判断图里是什么，
    没说明的图就不提供给它挑（和用户约定的：没有图片说明就退回纯文字）。
    """
    images: dict[str, tuple[str, str]] = {}
    by_token: dict[str, str] = {}

    def repl(m: re.Match) -> str:
        tok = doc_images._img_token(m)
        alt = doc_images._img_alt(m)
        if not (tok and alt):
            return "[图]"
        if tok not in by_token:
            by_token[tok] = f"I{len(by_token) + 1}"
            images[by_token[tok]] = (tok, alt)
        return f"[图 {by_token[tok]}：{alt[:CAPTION_CHARS]}]"

    return doc_images.IMG_TAG_RE.sub(repl, md or ""), images


# ── 解析与校验 ────────────────────────────────────────────────────

def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    body = t.split("\n", 1)[1] if "\n" in t else ""
    return body.rsplit("```", 1)[0].strip()


CELL_MAX = 40                 # 表格单元格超过这个长度就截断，全文挪进备注
CELL_KEEP = 30


def _inline(text) -> str:
    """把原文的行内标记变成纯文字。

    实测：表格的「状态更新」格子里原样印着 **调研准备阶段：**<br>**项目实施阶段** ——
    飞书用 im-markdown 导出时，表格单元格里的换行就是 <br>，粗体就是 **。模型照抄进来，
    渲染时就成了字面文字。
    """
    t = str(text or "")
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.IGNORECASE)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("**", "").replace("__", "").replace("`", "")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{2,}", "\n", t).strip()


def _strs(v) -> list[str]:
    return [_inline(x) for x in v if _inline(x)] if isinstance(v, list) else []


def parse_talk(raw: str, source_md: str, image_ids: set[str]) -> Talk:
    """解析模型的讲稿输出，并做确定性校验：数字、表格要在原文里对得上，图要真实存在。

    丢了什么、为什么都记进 ``dropped``，由调用方写进任务日志 —— 静默丢弃等于撒谎。
    """
    m = re.search(r"\{[\s\S]*\}", _strip_fence(raw))
    if not m:
        raise ValueError(f"讲稿输出不是 JSON：{(raw or '')[:80]}")
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ValueError(f"讲稿输出不是 JSON：{e}") from e

    talk = Talk(title=_inline(d.get("title")), subtitle=_inline(d.get("subtitle")))
    used_images = 0
    for raw_s in d.get("slides") or []:
        if not isinstance(raw_s, dict):
            continue
        kind = str(raw_s.get("kind") or "bullets")
        if kind not in TALK_KINDS:
            kind = "bullets"
        s = TalkSlide(kind=kind, title=_inline(raw_s.get("title")),
                      notes=_inline(raw_s.get("notes")),
                      sources=[x for x in _strs(raw_s.get("sources")) if re.fullmatch(r"S\d+", x)])

        bullets = _strs(raw_s.get("bullets"))
        if len(bullets) > MAX_BULLETS:
            # 截到 6 条，多出来的不丢，挪进备注
            extra = "\n".join(f"· {b}" for b in bullets[MAX_BULLETS:])
            s.notes = (s.notes + "\n\n" if s.notes else "") + "（页面放不下的要点）\n" + extra
            bullets = bullets[:MAX_BULLETS]
        s.bullets = bullets

        img = raw_s.get("image")
        if img:
            img = str(img).strip()
            if img not in image_ids:
                talk.dropped.append(f"「{s.title}」引用了不存在的图 {img}，已去掉")
            elif used_images >= MAX_IMAGES:
                talk.dropped.append(f"「{s.title}」的配图 {img} 超出 {MAX_IMAGES} 张上限，已去掉")
            else:
                s.image = img
                used_images += 1

        if kind == "metrics":
            asked = [x for x in (raw_s.get("metrics") or []) if isinstance(x, dict)]
            s.metrics = filter_real_metrics(asked, source_md, cap=MAX_METRICS)
            if len(s.metrics) < len(asked):
                talk.dropped.append(f"「{s.title}」有 {len(asked) - len(s.metrics)} 个数字在原文里找不到，已丢弃")
            if not s.metrics:
                talk.dropped.append(f"数据页「{s.title}」的数字都在原文里对不上，整页丢弃")
                continue

        if kind == "table":
            raw_cols = [str(c) for c in (raw_s.get("columns") or [])][:MAX_TABLE_COLS]
            raw_rows = [[str(c) for c in r][:MAX_TABLE_COLS]
                        for r in (raw_s.get("rows") or []) if isinstance(r, list)]
            # 先拿**原样**的单元格去原文里对，再清理标记 —— 原文里也带着 ** 和 <br>，
            # 先清理再对就对不上了。
            if not table_is_real(raw_cols, raw_rows, source_md):
                talk.dropped.append(f"表格「{s.title}」的内容在原文里对不上，整页丢弃")
                continue
            cols = [_inline(c) for c in raw_cols]
            rows = [[_inline(c) for c in r] for r in raw_rows]
            if len(rows) > MAX_TABLE_ROWS:
                s.notes = (s.notes + "\n\n" if s.notes else "") + \
                    f"（表格另有 {len(rows) - MAX_TABLE_ROWS} 行未放进页面，见原文）"
                rows = rows[:MAX_TABLE_ROWS]
            # 几百字塞进一个格子只能缩成看不见的字（实测「状态更新」列就是这样）。
            # 格子里留开头，全文挪进备注。
            spill = []
            for ri, r in enumerate(rows, 1):
                for ci, c in enumerate(r):
                    if len(c) > CELL_MAX:
                        head = cols[ci] if ci < len(cols) else f"第 {ci + 1} 列"
                        spill.append(f"【{head} · 第 {ri} 行】\n{c}")
                        r[ci] = c[:CELL_KEEP].rstrip() + "…（见备注）"
            if spill:
                s.notes = (s.notes + "\n\n" if s.notes else "") + "\n\n".join(spill)
            s.columns, s.rows = cols, rows

        if kind in ("bullets", "closing") and not (s.bullets or s.title):
            continue
        talk.slides.append(s)

    if len(talk.slides) < MIN_SLIDES:
        raise ValueError(f"讲稿只有 {len(talk.slides)} 页，太少，不像是正常结果")
    return talk


# ── 覆盖检查 ──────────────────────────────────────────────────────

def uncovered(sections: list[Section], slides: list[TalkSlide]) -> list[Section]:
    """没有被任何一页引用的章节。依据的是模型自报的 sources —— 可能不准，但比不查强。"""
    cited = {x for s in slides for x in s.sources}
    return [sec for sec in sections
            if sec.id not in cited and len(sec.text) >= MIN_REPORT_CHARS]


# ── 打模型 ────────────────────────────────────────────────────────

TALK_SYSTEM = """你是企业内部演示文稿的编辑，把一篇飞书文档重组成一份可以直接拿去讲的讲稿。

硬性要求：
1. 只使用原文里有的信息，不补充、不推测、不润色出原文没有的结论。
2. 数字只能用原文里真实出现过的；原文没有数字，就不要做数据页。
3. 表格只照抄原文里确实存在的表，列和行都来自原文；不要自己「整理」出一张新表。
4. 每一页都必须在 sources 里列出它的内容来自哪几个章节编号（原文里的【S…】）。
5. 原文的细节、例子、步骤写进 notes（演讲者备注），不要因为页面放不下就丢掉。"""

TALK_USER = """把下面这篇文档重组成讲稿。

# 文档标题
{title}

# 正文（共 {n} 个章节，每节前的【S…】是章节编号；【图 I…】是可选的配图及其说明）
{body}

# 组织方式
- 共 8–12 页（不含封面），按讲的顺序组织：核心要点 → 各个主题 → 关键数据（原文有才放）→ 下一步 / 行动项。
- 可以合并相近的章节，也可以把一个大章节拆成两页；但尽量让每个章节都被某一页用到。
- 每页 bullets 最多 6 条，每条尽量 30 字以内，写成短句。
- 可以给要点页配图：image 填图片编号（如 "I3"），只能用正文里出现过的【图 I…】，整份最多 6 张，只在图确实有助于理解时才配。
- 表格页最多 6 列 8 行。

# 输出 JSON（只输出这一个 JSON 对象，不要代码块）
{{
  "title": "讲稿标题",
  "subtitle": "一句话副标题",
  "slides": [
    {{"kind": "bullets", "title": "页标题", "bullets": ["要点"], "image": null, "notes": "原文细节", "sources": ["S1"]}},
    {{"kind": "metrics", "title": "页标题", "metrics": [{{"value": "80%", "label": "说明"}}], "notes": "", "sources": ["S2"]}},
    {{"kind": "table", "title": "页标题", "columns": ["列1"], "rows": [["值"]], "notes": "", "sources": ["S3"]}},
    {{"kind": "closing", "title": "下一步", "bullets": ["行动项"], "notes": "", "sources": ["S4"]}}
  ]
}}"""

TALK_MAX_TOKENS = 8000
TALK_TIMEOUT = 300


async def build_talk(ctx, md: str, title: str) -> tuple[Talk, list[Section], dict[str, tuple[str, str]]]:
    """整篇交给模型一次，重组成讲稿。解析不了就带着原因重试一次，再失败抛 ValueError。

    先给图编号、再切节：图片标记换成「[图 I3：…]」之后不含 #，切出来的节和原文一致，
    而图的编号在全文范围内统一（同一张图在两节里出现也只有一个编号）。
    """
    marked, images = mark_images(md)
    sections = split_sections(marked)
    body = "\n\n".join(f"【{s.id}】{s.heading}\n{s.text}" for s in sections)
    user = TALK_USER.format(title=title or "未命名文档", n=len(sections), body=body)

    last: Exception | None = None
    for attempt in range(2):
        prompt = user if attempt == 0 else (
            user + f"\n\n注意：上一次的输出无法使用（{last}）。请严格只输出一个 JSON 对象。")
        raw = await ctx.llm.text_complete(prompt, system=TALK_SYSTEM, json_mode=True,
                                          max_tokens=TALK_MAX_TOKENS, timeout=TALK_TIMEOUT, retries=1)
        try:
            return parse_talk(raw, md, set(images)), sections, images
        except ValueError as e:
            last = e
            await ctx.log("warn", f"第 {attempt + 1} 次生成的讲稿无法使用：{e}")
    raise ValueError(f"讲稿生成失败：{last}")
