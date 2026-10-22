import json

import pytest

from app.services.pptx_render import TalkSlide
from app.services.talk_deck import mark_images, parse_talk, split_sections, uncovered


# ── 切节 ──────────────────────────────────────────────────────────

H2_DOC = """# 产品使用培训

开场白，介绍这次培训的背景和目的，大约几十个字的一段话。

## 一、能做什么
能力介绍正文。

### 小节 a
小节内容。

## 二、怎么开通
开通步骤正文。

## 三、常见问题
FAQ 正文。
"""

H3_DOC = """# 2026 信息安全 Focus

## 总体目标
总体说明。

### 数据安全
数据安全正文。

### 应用安全
应用安全正文。

### 终端安全
终端安全正文。

## 附录
附录说明。

### 术语
术语正文。
"""


def test_split_sections_uses_the_level_this_doc_actually_uses():
    """二级为主的文档按二级切 —— 三级小节并进所属的二级节里。"""
    secs = split_sections(H2_DOC)
    heads = [s.heading for s in secs if s.id != "S0"]
    assert heads == ["一、能做什么", "二、怎么开通", "三、常见问题"]
    assert "小节内容" in next(s for s in secs if s.heading == "一、能做什么").text


def test_split_sections_picks_h3_when_h2_is_only_a_wrapper():
    """实测：一篇规划类文档二级标题全篇只有 2 个，真正的章节是三级。
    按二级切只会得到两个巨块，覆盖检查也就失去意义。"""
    heads = [s.heading for s in split_sections(H3_DOC) if s.id != "S0"]
    assert heads == ["数据安全", "应用安全", "终端安全", "术语"]


def test_split_sections_keeps_meaningful_preamble_as_s0():
    s0 = [s for s in split_sections(H2_DOC) if s.id == "S0"]
    assert s0 and "开场白" in s0[0].text


def test_split_sections_drops_a_preamble_that_is_just_the_title():
    doc = "# 只有标题\n\n## 第一节\n正文\n\n## 第二节\n正文"
    assert [s.id for s in split_sections(doc)] == ["S1", "S2"]


def test_split_sections_ids_are_sequential():
    assert [s.id for s in split_sections(H2_DOC) if s.id != "S0"] == ["S1", "S2", "S3"]


def test_split_sections_chunks_a_doc_without_headings():
    """没有标题的文档按段落切成约 1000 字的块，模型才有东西可引用。"""
    doc = "\n\n".join("段落" + "字" * 400 for _ in range(6))
    secs = split_sections(doc)
    assert len(secs) >= 2
    assert all(len(s.text) <= 1500 for s in secs)
    assert all(s.heading.startswith("第") for s in secs)


def test_split_sections_empty_doc():
    assert split_sections("") == []


# ── 图片编号 ──────────────────────────────────────────────────────

def test_mark_images_numbers_images_that_have_captions():
    md = "前文\n![界面截图，左侧是消息列表](https://feishu.cn/file/AAAAAAAAAAAAAAAAAAAAAAAA)\n后文"
    text, imgs = mark_images(md)
    assert "[图 I1：界面截图，左侧是消息列表]" in text
    assert imgs == {"I1": ("AAAAAAAAAAAAAAAAAAAAAAAA", "界面截图，左侧是消息列表")}


def test_mark_images_does_not_number_images_without_captions():
    """没有说明文字的图模型无从判断，按约定不提供给它挑。"""
    md = "![](https://feishu.cn/file/BBBBBBBBBBBBBBBBBBBBBBBB)"
    text, imgs = mark_images(md)
    assert text.strip() == "[图]"
    assert imgs == {}


def test_mark_images_dedupes_the_same_image():
    md = ("![说明](https://feishu.cn/file/CCCCCCCCCCCCCCCCCCCCCCCC)\n"
          "![说明](https://feishu.cn/file/CCCCCCCCCCCCCCCCCCCCCCCC)")
    _text, imgs = mark_images(md)
    assert list(imgs) == ["I1"]


def test_mark_images_truncates_long_captions():
    md = "![" + "很长的说明" * 30 + "](https://feishu.cn/file/DDDDDDDDDDDDDDDDDDDDDDDD)"
    text, _ = mark_images(md)
    marker = text.strip()
    assert len(marker) < 80


# ── 解析与校验 ────────────────────────────────────────────────────

SRC = ("本季度客服响应效率提升 80%，拦截恶意请求 1200 万次。\n"
       "| 阶段 | 负责人 |\n| 开发 | 张三 |\n| 测试 | 李四 |")


def _talk(slides, **kw):
    return json.dumps({"title": kw.get("title", "讲稿标题"), "subtitle": "副标题",
                       "slides": slides}, ensure_ascii=False)


def _b(title, n=3, **kw):
    return {"kind": "bullets", "title": title, "bullets": [f"要点{i}" for i in range(n)],
            "sources": ["S1"], **kw}


def test_parse_talk_basic():
    t = parse_talk(_talk([_b("一"), _b("二"), _b("三")]), SRC, set())
    assert t.title == "讲稿标题"
    assert [s.title for s in t.slides] == ["一", "二", "三"]
    assert t.slides[0].sources == ["S1"]


def test_parse_talk_strips_code_fence():
    raw = "```json\n" + _talk([_b("一"), _b("二"), _b("三")]) + "\n```"
    assert len(parse_talk(raw, SRC, set()).slides) == 3


def test_parse_talk_rejects_garbage():
    with pytest.raises(ValueError, match="不是 JSON"):
        parse_talk("抱歉，我无法完成这个任务。", SRC, set())


def test_parse_talk_rejects_too_few_slides():
    """两页的「讲稿」多半是模型没读完或出错了，别当成功交出去。"""
    with pytest.raises(ValueError, match="太少"):
        parse_talk(_talk([_b("一"), _b("二")]), SRC, set())


def test_parse_talk_unknown_kind_becomes_bullets():
    t = parse_talk(_talk([_b("一", kind="timeline"), _b("二"), _b("三")]), SRC, set())
    assert t.slides[0].kind == "bullets"


def test_parse_talk_caps_bullets_and_moves_the_rest_to_notes():
    """超过 6 条就截到 6 条，但多出来的不能丢 —— 挪进备注。"""
    t = parse_talk(_talk([_b("一", n=9), _b("二"), _b("三")]), SRC, set())
    s = t.slides[0]
    assert len(s.bullets) == 6
    assert "要点8" in s.notes


def test_parse_talk_drops_reference_to_unknown_image():
    t = parse_talk(_talk([_b("一", image="I9"), _b("二"), _b("三")]), SRC, {"I1"})
    assert t.slides[0].image is None
    assert any("I9" in d for d in t.dropped)


def test_parse_talk_caps_images_at_six():
    ids = {f"I{i}" for i in range(1, 10)}
    slides = [_b(f"页{i}", image=f"I{i}") for i in range(1, 9)]
    t = parse_talk(_talk(slides), SRC, ids)
    assert sum(1 for s in t.slides if s.image) == 6


def test_parse_talk_drops_invented_metrics():
    m = {"kind": "metrics", "title": "数据", "sources": ["S1"],
         "metrics": [{"value": "80%", "label": "效率"}, {"value": "97%", "label": "编的"}]}
    t = parse_talk(_talk([m, _b("二"), _b("三")]), SRC, set())
    assert [x["label"] for x in t.slides[0].metrics] == ["效率"]


def test_parse_talk_drops_a_metrics_slide_with_no_real_numbers():
    m = {"kind": "metrics", "title": "数据", "sources": ["S1"],
         "metrics": [{"value": "97%", "label": "编的"}]}
    t = parse_talk(_talk([m, _b("二"), _b("三"), _b("四")]), SRC, set())
    assert all(s.kind != "metrics" for s in t.slides)
    assert any("数据" in d for d in t.dropped)


def test_parse_talk_keeps_real_table():
    tb = {"kind": "table", "title": "分工", "sources": ["S1"],
          "columns": ["阶段", "负责人"], "rows": [["开发", "张三"], ["测试", "李四"]]}
    t = parse_talk(_talk([tb, _b("二"), _b("三")]), SRC, set())
    assert t.slides[0].kind == "table" and t.slides[0].rows == [["开发", "张三"], ["测试", "李四"]]


def test_parse_talk_drops_invented_table():
    tb = {"kind": "table", "title": "营收", "sources": ["S1"],
          "columns": ["季度", "营收"], "rows": [["Q1", "300 万"], ["Q2", "420 万"]]}
    t = parse_talk(_talk([tb, _b("二"), _b("三"), _b("四")]), SRC, set())
    assert all(s.kind != "table" for s in t.slides)
    assert any("营收" in d for d in t.dropped)


def test_parse_talk_caps_table_size_and_notes_the_rest():
    rows = [["开发", "张三"]] * 12
    tb = {"kind": "table", "title": "分工", "sources": ["S1"],
          "columns": ["阶段", "负责人"], "rows": rows}
    t = parse_talk(_talk([tb, _b("二"), _b("三")]), SRC, set())
    assert len(t.slides[0].rows) == 8
    assert "4 行" in t.slides[0].notes


def test_parse_talk_ignores_malformed_source_ids():
    t = parse_talk(_talk([_b("一", sources=["S2", "第三节", 7]), _b("二"), _b("三")]), SRC, set())
    assert t.slides[0].sources == ["S2"]


# ── 覆盖检查 ──────────────────────────────────────────────────────

def test_uncovered_lists_sections_no_slide_cited():
    from app.services.talk_deck import Section
    body = "正文" * 40                      # 够长，不会被「太短不报」过滤掉
    secs = [Section("S1", "一、能做什么", body), Section("S2", "二、怎么开通", body),
            Section("S3", "三、常见问题", body)]
    slides = [TalkSlide(kind="bullets", title="x", sources=["S1", "S3"])]
    assert [s.heading for s in uncovered(secs, slides)] == ["二、怎么开通"]


def test_uncovered_ignores_tiny_sections():
    """二十来字的节（「本节略」之类）列进「未收录」只是噪音。"""
    from app.services.talk_deck import Section
    secs = [Section("S1", "有内容", "正文" * 60), Section("S2", "占位", "本节略")]
    assert [s.id for s in uncovered(secs, [])] == ["S1"]


def test_uncovered_all_cited():
    secs = split_sections(H2_DOC)
    slides = [TalkSlide(kind="bullets", title="x", sources=[s.id for s in secs])]
    assert uncovered(secs, slides) == []


# ── 真文档跑出来的：表格单元格里是原文的原始标记，长文字挤成蚊子腿 ──

STATUS_SRC = ("| 序号 | 具体事项 | 状态更新 |\n| 1 | 项目进度 | **调研准备阶段：**<br>"
              "**项目实施阶段**：2026.6 试点-供应商A2026.7 试点-供应商B2026.7 试点-供应商C"
              "2026.8 试点-供应商D2026.8 选型2026.9 平台开发对接接口2026.10 输出评估报告 |")


def test_parse_talk_strips_markdown_and_br_from_cells():
    """实测：「状态更新」单元格里原样印着 **调研准备阶段：**<br>**项目实施阶段** —— 粗体标记
    和换行标签都成了字面文字。原文用 im-markdown 导出时表格单元格里的换行就是 <br>。"""
    tb = {"kind": "table", "title": "进展", "sources": ["S1"], "columns": ["序号", "具体事项", "状态更新"],
          "rows": [["1", "项目进度", "**调研准备阶段：**<br>**项目实施阶段**：2026.6 试点"]]}
    b = {"kind": "bullets", "title": "要点", "bullets": ["**重点**：完成迁移"], "sources": ["S1"]}
    t = parse_talk(json.dumps({"title": "t", "slides": [tb, b, b]}, ensure_ascii=False), STATUS_SRC, set())
    cell = t.slides[0].rows[0][2]
    assert "**" not in cell and "<br>" not in cell
    assert "调研准备阶段" in cell and "项目实施阶段" in cell
    assert t.slides[1].bullets == ["重点：完成迁移"]


def test_parse_talk_moves_overlong_cells_to_notes():
    """几百字塞进一个表格格子只能缩成看不见的字。格子里留开头，全文挪进备注。"""
    long = "2026.6 试点-供应商A2026.7 试点-供应商B2026.7 试点-供应商C2026.8 试点-供应商D2026.8 选型"
    tb = {"kind": "table", "title": "进展", "sources": ["S1"], "columns": ["序号", "具体事项", "状态更新"],
          "rows": [["1", "项目进度", long]]}
    b = {"kind": "bullets", "title": "要点", "bullets": ["x"], "sources": ["S1"]}
    t = parse_talk(json.dumps({"title": "t", "slides": [tb, b, b]}, ensure_ascii=False), STATUS_SRC, set())
    s = t.slides[0]
    assert len(s.rows[0][2]) <= 45
    assert "见备注" in s.rows[0][2]
    assert long in s.notes


def test_split_sections_strips_markdown_from_headings():
    """实测：「## **现场体验…**」的星号原样出现在了「未收录章节」页上。"""
    body = "正文内容足够长，" * 10
    md = f"## **现场体验**\n{body}\n## 第二节\n{body}\n## 第三节\n{body}"
    heads = [s.heading for s in split_sections(md)]
    assert "现场体验" in heads and not any("**" in h for h in heads)
