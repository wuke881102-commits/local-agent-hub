import json

import pytest

from app.services.pptx_render import Band, Column, SlidePlan
from app.services.slide_rebuild import crop_boxes, parse_skeleton

SKELETON = {
    "is_slide": True, "title": "标题", "subtitle": "副标题", "theme": "dark",
    "bands": [
        {"role": "flow", "columns": [{"index": "", "heading": "终端用户", "blocks": []}]},
        {"role": "columns", "columns": [
            {"index": "01", "heading": "第一栏", "blocks": [
                {"kind": "box", "label": "框", "sub": "小字", "accent": "blue"},
                {"kind": "group", "label": "组", "items": ["A", "B"]},
            ]},
        ]},
    ],
}


# ── 骨架解析 ──────────────────────────────────────────────────────

def test_parse_skeleton_builds_slideplan():
    p = parse_skeleton(json.dumps(SKELETON, ensure_ascii=False))
    assert isinstance(p, SlidePlan)
    assert p.title == "标题"
    assert len(p.bands) == 2
    assert p.bands[0].role == "flow"
    assert p.bands[1].columns[0].index == "01"
    assert p.bands[1].columns[0].blocks[1].items == ["A", "B"]


def test_parse_skeleton_strips_code_fence():
    """模型时不时把 JSON 裹进 ```json 围栏，和提炼那条路是同一个毛病。"""
    fenced = "```json\n" + json.dumps(SKELETON, ensure_ascii=False) + "\n```"
    assert parse_skeleton(fenced).title == "标题"


def test_parse_skeleton_rejects_garbage():
    with pytest.raises(ValueError, match="不是 JSON"):
        parse_skeleton("对不起，我无法识别这张图片。")


def test_parse_skeleton_tolerates_unknown_kind():
    """模型编出没见过的 kind 时退成 box，不要整页失败。"""
    bad = json.loads(json.dumps(SKELETON))
    bad["bands"][1]["columns"][0]["blocks"][0]["kind"] = "diamond"
    assert parse_skeleton(json.dumps(bad)).bands[1].columns[0].blocks[0].kind == "box"


def test_parse_skeleton_missing_fields_get_defaults():
    p = parse_skeleton('{"title": "只有标题"}')
    assert p.title == "只有标题"
    assert p.bands == []
    assert p.is_slide is True


def test_parse_skeleton_not_a_slide():
    p = parse_skeleton('{"is_slide": false, "title": "现场合影"}')
    assert p.is_slide is False


def test_parse_skeleton_survives_non_dict_blocks():
    """模型偶尔把 blocks 写成字符串数组，别让它整页崩掉。"""
    bad = json.loads(json.dumps(SKELETON))
    bad["bands"][1]["columns"][0]["blocks"] = ["就一个字符串", {"kind": "box", "label": "好的"}]
    p = parse_skeleton(json.dumps(bad, ensure_ascii=False))
    assert [b.label for b in p.bands[1].columns[0].blocks] == ["好的"]


# ── 几何切块 ──────────────────────────────────────────────────────

def _plan_2x3():
    return SlidePlan(bands=[
        Band(role="flow", columns=[Column(), Column()]),
        Band(role="columns", columns=[Column(), Column(), Column()]),
    ])


def test_crop_boxes_one_per_column():
    assert len(crop_boxes(_plan_2x3(), width=4000, height=3000)) == 5      # 2 + 3


def test_crop_boxes_are_inside_the_image():
    for (_b, _c, x0, y0, x1, y1) in crop_boxes(_plan_2x3(), width=4000, height=3000):
        assert 0 <= x0 < x1 <= 4000
        assert 0 <= y0 < y1 <= 3000


def test_crop_boxes_overlap_neighbours():
    """相邻切片要有重叠带，否则骑在分界线上的字两边都切不全。"""
    boxes = [b for b in crop_boxes(_plan_2x3(), width=4000, height=3000) if b[0] == 1]
    assert boxes[1][2] < boxes[0][4], "第二栏的左边缘应当落在第一栏右边缘之内"


def test_crop_boxes_empty_plan():
    assert crop_boxes(SlidePlan(), width=100, height=100) == []


def test_crop_boxes_skips_columnless_band():
    plan = SlidePlan(bands=[Band(columns=[]), Band(columns=[Column()])])
    assert len(crop_boxes(plan, width=1000, height=1000)) == 1


def test_crop_boxes_tiny_image_still_valid():
    """小图上 int() 截断可能让 x0 == x1，那样 PIL.crop 会返回 0 宽的图。"""
    for (_b, _c, x0, y0, x1, y1) in crop_boxes(_plan_2x3(), width=40, height=30):
        assert x1 > x0 and y1 > y0


# ── 编排（视觉调用换成假的）────────────────────────────────────────

from pathlib import Path

from PIL import Image

from app.services.slide_rebuild import rebuild_slide


class FakeLLM:
    vision_model = "fake-vision"

    def __init__(self, skeleton: str, region: str = "第一行\n第二行"):
        self.skeleton, self.region, self.calls = skeleton, region, 0

    async def vision_describe(self, url, prompt, max_tokens=0, model=None, provider="dashscope"):
        self.calls += 1
        return self.skeleton if self.calls == 1 else self.region


class Ctx:
    task_id = "t-1"

    def __init__(self, llm):
        self.llm, self.lines = llm, []

    async def log(self, level, msg):
        self.lines.append(msg)


def _photo(tmp_path: Path, color=(12, 20, 40)) -> Path:
    f = tmp_path / "poster.png"
    Image.new("RGB", (1200, 900), color).save(f)
    return f


async def test_rebuild_slide_calls_once_per_column_plus_skeleton(tmp_path):
    llm = FakeLLM(json.dumps(SKELETON, ensure_ascii=False))
    plan = await rebuild_slide(Ctx(llm), _photo(tmp_path))
    # SKELETON：band0 有 1 栏，band1 有 1 栏 → 1 次骨架 + 2 次分区
    assert llm.calls == 3
    assert plan.title == "标题"


async def test_rebuild_slide_fills_notes_with_transcript(tmp_path):
    llm = FakeLLM(json.dumps(SKELETON, ensure_ascii=False), region="逐字转写内容")
    plan = await rebuild_slide(Ctx(llm), _photo(tmp_path))
    assert "逐字转写内容" in plan.notes


async def test_rebuild_slide_non_slide_skips_region_pass(tmp_path):
    """不是幻灯片就别再花 N 次调用去切块识别。"""
    llm = FakeLLM('{"is_slide": false, "title": "现场合影"}')
    plan = await rebuild_slide(Ctx(llm), _photo(tmp_path))
    assert plan.is_slide is False
    assert llm.calls == 1


async def test_rebuild_slide_bad_skeleton_falls_back(tmp_path):
    """骨架解析不了时退成单页兜底，不抛给上层让整篇失败。"""
    llm = FakeLLM("我无法识别这张图片")
    plan = await rebuild_slide(Ctx(llm), _photo(tmp_path))
    assert plan.is_slide is False
    assert plan.bands == []


async def test_rebuild_slide_region_failure_does_not_kill_the_page(tmp_path):
    """某一栏识别失败，其余栏照常，整页仍然产出。"""
    class Flaky(FakeLLM):
        async def vision_describe(self, url, prompt, max_tokens=0, model=None, provider="dashscope"):
            self.calls += 1
            if self.calls == 1:
                return self.skeleton
            if self.calls == 2:
                raise RuntimeError("视觉服务抖了一下")
            return self.region

    llm = Flaky(json.dumps(SKELETON, ensure_ascii=False))
    ctx = Ctx(llm)
    plan = await rebuild_slide(ctx, _photo(tmp_path))
    assert plan.is_slide is True
    assert any("识别失败" in x for x in ctx.lines)


def test_merge_clears_stale_sub_when_label_is_replaced():
    """逐字结果改写 label 时，骨架那版的 sub 必须一起清掉。

    真文档跑出来才看见：骨架的粗文字和逐字结果不是一一对应的，只改 label 会让
    一个框的标题来自 A、副题来自 B —— 「鉴权/限流」配着「小程序」。
    这比识别不准糟得多，因为它看起来完全合理。
    """
    from app.services.pptx_render import Block, Column
    from app.services.slide_rebuild import _merge_region_text

    plan = SlidePlan(bands=[Band(columns=[Column(index="00", heading="旧标题", blocks=[
        Block(kind="box", label="骨架给的框一", sub="骨架给的小字一"),
        Block(kind="box", label="骨架给的框二", sub="骨架给的小字二"),
    ])])])
    # 逐字结果里不含栏标题那一行（含的话会被按标题过滤掉，见另一条测试）
    _merge_region_text(plan, {(0, 0): "逐字框一\n逐字框二"})
    blocks = plan.bands[0].columns[0].blocks
    assert blocks[0].label == "逐字框一"
    assert blocks[0].sub == "", "骨架的 sub 必须清掉，否则标题和副题来自不同来源"
    assert blocks[1].sub == ""


def test_merge_keeps_skeleton_text_when_no_region_result():
    """这一栏没识别出东西时，保留骨架那版粗文字，别把内容清空。"""
    from app.services.pptx_render import Block, Column
    from app.services.slide_rebuild import _merge_region_text

    plan = SlidePlan(bands=[Band(columns=[Column(blocks=[
        Block(kind="box", label="骨架文字", sub="骨架小字"),
    ])])])
    _merge_region_text(plan, {(0, 0): ""})
    assert plan.bands[0].columns[0].blocks[0].label == "骨架文字"
    assert plan.bands[0].columns[0].blocks[0].sub == "骨架小字"


def test_merge_leaves_extra_blocks_untouched():
    """逐字行数少于块数时，多出来的块保持骨架原样，不许错位填充。"""
    from app.services.pptx_render import Block, Column
    from app.services.slide_rebuild import _merge_region_text

    plan = SlidePlan(bands=[Band(columns=[Column(blocks=[
        Block(label="一", sub="s1"), Block(label="二", sub="s2"), Block(label="三", sub="s3"),
    ])])])
    _merge_region_text(plan, {(0, 0): "新一"})
    labels = [b.label for b in plan.bands[0].columns[0].blocks]
    assert labels == ["新一", "二", "三"]
    assert plan.bands[0].columns[0].blocks[2].sub == "s3"


def test_merge_does_not_let_the_index_line_become_a_block():
    """逐字结果里那行孤零零的栏编号不该占掉第一个块。

    这条原本断言的是「拿逐字结果第一行当栏标题、并剥掉行首编号」——
    那个做法本身就是 bug：切块带重叠，切进来的第一行可能是大标题或会场 logo，
    实测把「数据源接入」改成了「年度大会」。现在栏标题一律以骨架为准，
    前提没了，断言跟着改。
    """
    from app.services.pptx_render import Block, Column
    from app.services.slide_rebuild import _merge_region_text

    plan = SlidePlan(bands=[Band(columns=[
        Column(index="01", heading="Agentic BOT", blocks=[Block(label="x")]),
    ])])
    _merge_region_text(plan, {(0, 0): "01\nAgentic BOT\n真正的内容"})
    col = plan.bands[0].columns[0]
    assert col.heading == "Agentic BOT"
    assert col.blocks[0].label == "真正的内容"


def test_merge_drops_blocks_that_just_repeat_the_title():
    """骨架常把大标题/副标题又塞成一个块，渲染出来整页重复两遍。"""
    from app.services.pptx_render import Block, Column
    from app.services.slide_rebuild import drop_title_echoes

    plan = SlidePlan(
        title="某大模型公司：公有云 MaaS 业务架构与防护",
        subtitle="WAF 接入模型服务公网入口",
        bands=[Band(columns=[Column(blocks=[
            Block(label="某大模型公司：公有云MaaS业务架构与防护"),   # 少了空格，仍是同一句
            Block(label="WAF接入模型服务公网入口"),
            Block(label="真正的内容"),
        ])])])
    drop_title_echoes(plan)
    assert [b.label for b in plan.bands[0].columns[0].blocks] == ["真正的内容"]


def test_drop_title_echoes_keeps_everything_when_title_is_empty():
    from app.services.pptx_render import Block, Column
    from app.services.slide_rebuild import drop_title_echoes

    plan = SlidePlan(title="", bands=[Band(columns=[Column(blocks=[
        Block(label="内容一"), Block(label="内容二")])])])
    drop_title_echoes(plan)
    assert len(plan.bands[0].columns[0].blocks) == 2


async def test_rebuild_slide_applies_title_echo_drop(tmp_path):
    """光有 drop_title_echoes 不算数，rebuild_slide 得真的调它。"""
    skel = {"is_slide": True, "title": "重复的标题", "subtitle": "",
            "bands": [{"role": "columns", "columns": [
                {"index": "", "heading": "", "blocks": [
                    {"kind": "box", "label": "重复的标题"},
                    {"kind": "box", "label": "真内容"},
                ]}]}]}
    llm = FakeLLM(json.dumps(skel, ensure_ascii=False), region="重复的标题\n真内容")
    plan = await rebuild_slide(Ctx(llm), _photo(tmp_path))
    labels = [b.label for b in plan.bands[0].columns[0].blocks]
    assert "重复的标题" not in labels, f"标题回声没被去掉：{labels}"


def test_merge_never_overwrites_the_column_heading():
    """栏标题以骨架为准，逐字结果不许改它。

    切块为了不漏边缘字是带重叠的，栏顶上方的大标题、会场 logo 都会被切进来，
    于是「第一行就是栏标题」这个假设根本不成立：实测把「数据源接入」改成了
    「年度大会」，把「上下文资产化」改成了大标题的一个片段。
    骨架那一遍看的是整张图，栏标题恰恰是它最可靠的产出。
    """
    from app.services.pptx_render import Block, Column
    from app.services.slide_rebuild import _merge_region_text

    plan = SlidePlan(title="双通道多Agent协同的文档质量评估系统", bands=[Band(columns=[
        Column(index="00", heading="数据源接入", blocks=[Block(label="占位")]),
    ])])
    _merge_region_text(plan, {(0, 0): "年度大会\n00\n数据源接入\n多入口统一获取"})
    col = plan.bands[0].columns[0]
    assert col.heading == "数据源接入", f"栏标题被逐字结果改坏了：{col.heading!r}"


def test_merge_skips_lines_that_echo_heading_or_title():
    """切进来的标题/编号/栏名不该占掉第一个块的位置。"""
    from app.services.pptx_render import Block, Column
    from app.services.slide_rebuild import _merge_region_text

    plan = SlidePlan(title="大标题", bands=[Band(columns=[
        Column(index="00", heading="数据源接入",
               blocks=[Block(label="占位一"), Block(label="占位二")]),
    ])])
    _merge_region_text(plan, {(0, 0): "大标题\n00\n数据源接入\n多入口统一获取\n统一数据快照"})
    labels = [b.label for b in plan.bands[0].columns[0].blocks]
    assert labels == ["多入口统一获取", "统一数据快照"], labels


def test_merge_drops_title_fragments_sliced_across_columns():
    """标题横跨整张图，按栏切块会把它切成竖条，每栏读出一小段。

    实测：标题「双通道多Agent协同的文档质量评估系统」被切成
    '双' / '通道多Agent协' / 'tt协同的文档质量评估系' / '估系统' 四段，
    分别成了四栏的第一个块。整行比对判不出来 —— 它们是标题的**子串**。
    调 TOP_SKIP 只是对着一张照片调参，换张构图就又不对了。
    """
    from app.services.pptx_render import Block, Column
    from app.services.slide_rebuild import _merge_region_text

    plan = SlidePlan(
        title="双通道多Agent协同的文档质量评估系统",
        subtitle="统一获取多类数据源，沉淀项目级上下文",
        bands=[Band(columns=[Column(index="00", heading="数据源接入", blocks=[
            Block(label="占位一"), Block(label="占位二")])])])
    _merge_region_text(plan, {(0, 0): "双通道多Agent协\n沉淀项目级上\n多入口统一获取\n统一数据快照"})
    labels = [b.label for b in plan.bands[0].columns[0].blocks]
    assert labels == ["多入口统一获取", "统一数据快照"], labels


def test_merge_drops_index_plus_heading_line():
    """「01  上下文资产化」这种编号+栏名连写的行也要滤掉。"""
    from app.services.pptx_render import Block, Column
    from app.services.slide_rebuild import _merge_region_text

    plan = SlidePlan(bands=[Band(columns=[
        Column(index="01", heading="上下文资产化", blocks=[Block(label="占位")]),
    ])])
    _merge_region_text(plan, {(0, 0): "01  上下文资产化\n六类事实统一索引"})
    assert plan.bands[0].columns[0].blocks[0].label == "六类事实统一索引"


def test_merge_keeps_a_label_that_merely_shares_a_word_with_the_title():
    """只共享个别词不算标题碎片，别把正经内容误删。"""
    from app.services.pptx_render import Block, Column
    from app.services.slide_rebuild import _merge_region_text

    plan = SlidePlan(title="双通道多Agent协同的文档质量评估系统",
                     bands=[Band(columns=[Column(blocks=[Block(label="占位")])])])
    _merge_region_text(plan, {(0, 0): "规则引擎与LLM引擎双向证据交换"})
    assert plan.bands[0].columns[0].blocks[0].label == "规则引擎与LLM引擎双向证据交换"


# ── 模型给的栏坐标 ────────────────────────────────────────────────

def test_parse_skeleton_reads_column_box():
    d = json.loads(json.dumps(SKELETON))
    d["bands"][1]["columns"][0]["box"] = [0.05, 0.33, 0.24, 0.89]
    assert parse_skeleton(json.dumps(d)).bands[1].columns[0].box == (0.05, 0.33, 0.24, 0.89)


def test_parse_skeleton_rejects_malformed_box():
    """坐标不合法就当没有，退回几何等分 —— 不要拿着坏坐标去裁图。"""
    for bad in ([0.5, 0.1, 0.2, 0.9], [0, 0, 1], "0,0,1,1", [0, 0, 1, 2], None):
        d = json.loads(json.dumps(SKELETON))
        d["bands"][1]["columns"][0]["box"] = bad
        assert parse_skeleton(json.dumps(d)).bands[1].columns[0].box is None, bad


def test_crop_boxes_uses_model_box_when_present():
    """有模型坐标就照它裁，不再等分 —— 等分对不齐展板是所有杂质的根源。"""
    from app.services.pptx_render import Column
    plan = SlidePlan(bands=[Band(columns=[
        Column(box=(0.05, 0.33, 0.24, 0.89)),
        Column(box=(0.26, 0.33, 0.44, 0.89)),
    ])])
    boxes = crop_boxes(plan, width=1000, height=1000)
    assert len(boxes) == 2
    (_b0, _c0, x0, y0, x1, y1) = boxes[0]
    assert (x0, y0, x1, y1) == (50, 330, 240, 890)
    # 模型坐标已经避开了标题带和相邻栏，不该再外扩重叠
    assert boxes[1][2] >= boxes[0][4], "用模型坐标时不应再加重叠"


def test_crop_boxes_falls_back_to_even_split_without_boxes():
    from app.services.pptx_render import Column
    plan = SlidePlan(bands=[Band(columns=[Column(), Column()])])
    assert len(crop_boxes(plan, width=1000, height=1000)) == 2


def test_crop_boxes_mixed_boxes_and_none():
    """只有一部分栏给了坐标时，两种都得能用。"""
    from app.services.pptx_render import Column
    plan = SlidePlan(bands=[Band(columns=[Column(box=(0.1, 0.3, 0.3, 0.9)), Column()])])
    boxes = crop_boxes(plan, width=1000, height=1000)
    assert len(boxes) == 2
    for (_b, _c, x0, y0, x1, y1) in boxes:
        assert 0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000


def test_is_chrome_catches_index_prefixed_heading_echo():
    """「03　审核与可信输出」这种「编号+栏名」要滤掉，即使栏名被 OCR 读岔了。

    实测栏标题是「审核与可复核输出」，OCR 读成「审核与可信输出」，
    按栏名包含判不出来 —— 但它以栏编号开头，这一点是稳的。
    """
    from app.services.pptx_render import Column
    from app.services.slide_rebuild import _is_chrome

    plan = SlidePlan(title="某标题")
    col = Column(index="03", heading="审核与可复核输出")
    assert _is_chrome("03　审核与可信输出", plan, col)
    assert _is_chrome("03", plan, col)
    assert not _is_chrome("候选风险集合", plan, col)


def test_is_chrome_does_not_eat_content_when_index_is_empty():
    from app.services.pptx_render import Column
    from app.services.slide_rebuild import _is_chrome

    assert not _is_chrome("03 是个正经的内容行", SlidePlan(title="x"), Column(index=""))


def test_is_chrome_drops_bare_numbers():
    """光秃秃的编号不是内容。实测漏进来过 '02' / '03' —— 那是相邻栏的编号从
    切片边缘带进来的，和本栏的 index 对不上，所以按 index 比对判不出来。"""
    from app.services.pptx_render import Column
    from app.services.slide_rebuild import _is_chrome

    plan, col = SlidePlan(title="x"), Column(index="01", heading="CC 防护")
    for n in ("02", "03", "00", "1"):
        assert _is_chrome(n, plan, col), n
    assert not _is_chrome("30% 的资损下降", plan, col)


def test_is_chrome_catches_ocr_variant_of_the_heading():
    """栏标题被 OCR 读岔一两个字，仍要认出它是标题回声。

    实测栏标题「审核与可复核输出」被读成「审核与可信输出」，既没带编号、
    又不是子串，两条判据都落空，于是它成了这一栏的第一个框。
    """
    from app.services.pptx_render import Column
    from app.services.slide_rebuild import _is_chrome

    plan = SlidePlan(title="某标题")
    col = Column(index="03", heading="审核与可复核输出")
    assert _is_chrome("审核与可信输出", plan, col)
    assert not _is_chrome("候选风险集合", plan, col)
    assert not _is_chrome("证据一致性：代码、变量、文件位置相互印证", plan, col)


def test_merge_uses_a_looser_match_for_the_first_line():
    """栏的切片是从栏标题开始的，所以**第一行**本来就该是标题 —— 这个位置先验很强，
    可以用更松的阈值，而不必把全局阈值放宽。

    实测「审核与可复核输出」被读成「审校与可信输出」，字符集相似度只有 0.56，
    卡在全局阈值外面，于是成了这一栏的第一个框。
    """
    from app.services.pptx_render import Block, Column
    from app.services.slide_rebuild import _merge_region_text

    plan = SlidePlan(bands=[Band(columns=[
        Column(index="03", heading="审核与可复核输出",
               blocks=[Block(label="占位一"), Block(label="占位二")]),
    ])])
    _merge_region_text(plan, {(0, 0): "审校与可信输出\n候选风险集合\n证据一致性"})
    labels = [b.label for b in plan.bands[0].columns[0].blocks]
    assert labels == ["候选风险集合", "证据一致性"], labels


def test_merge_collapses_consecutive_duplicate_lines():
    """同一句连着出现两次是切片重叠的产物，不该占两个框。"""
    from app.services.pptx_render import Block, Column
    from app.services.slide_rebuild import _merge_region_text

    plan = SlidePlan(bands=[Band(columns=[Column(blocks=[
        Block(label="a"), Block(label="b")])])])
    _merge_region_text(plan, {(0, 0): "减少恶意调用带来的资损\n减少恶意调用带来的资损\n百万级"})
    labels = [b.label for b in plan.bands[0].columns[0].blocks]
    assert labels == ["减少恶意调用带来的资损", "百万级"], labels


def test_parse_skeleton_moves_a_leading_number_out_of_the_heading():
    """骨架有时把编号写进 heading（'01 Agentic BOT'），而渲染画的是
    「{index} {heading}」，直接用会显示成「00 01 Agentic BOT」。"""
    d = json.loads(json.dumps(SKELETON))
    d["bands"][1]["columns"][0]["index"] = "00"
    d["bands"][1]["columns"][0]["heading"] = "01 Agentic BOT"
    col = parse_skeleton(json.dumps(d, ensure_ascii=False)).bands[1].columns[0]
    assert col.index == "01"
    assert col.heading == "Agentic BOT"


async def test_rebuild_slide_falls_back_when_the_vl_model_is_unavailable(tmp_path):
    """骨架那一次调用挂了（授权被收回 / 网络不通），不能打死整篇。

    那三个 VL 模型昨天还是 403 —— 这不是假想的故障。降级顺序：
    指定的 VL 模型 → 主视觉模型（没有坐标，切块退回几何等分）→ 只出原图页。
    """
    calls = []

    class Flaky:
        vision_model = "fake-vision"

        async def vision_describe(self, url, prompt, max_tokens=0, model=None, provider="dashscope"):
            calls.append(model)
            if model:                       # 点名的 VL 模型不可用
                raise RuntimeError("Model.AccessDenied")
            return json.dumps(SKELETON, ensure_ascii=False)

    ctx = Ctx(Flaky())
    plan = await rebuild_slide(ctx, _photo(tmp_path))
    assert plan.is_slide is True, "退到主视觉模型后仍该出还原页"
    assert plan.title == "标题"
    assert calls[0] is not None and calls[1] is None, f"没有走降级：{calls[:2]}"
    assert any("骨架" in x for x in ctx.lines), "降级要在日志里说明白"


async def test_rebuild_slide_photo_only_when_both_models_fail(tmp_path):
    """两档都挂了就只出原图页，仍然不抛给上层。"""
    class Dead:
        vision_model = "fake-vision"

        async def vision_describe(self, url, prompt, max_tokens=0, model=None, provider="dashscope"):
            raise RuntimeError("端点不可达")

    ctx = Ctx(Dead())
    plan = await rebuild_slide(ctx, _photo(tmp_path))
    assert plan.is_slide is False
    assert plan.bands == []


# ── 8.3 装机版上肉眼看到的：流程箭头被读成 callout 数值 ──────────────

def test_parse_skeleton_arrow_only_callout_becomes_a_box():
    """展板上框与框之间的流程箭头「↑」被骨架当成了 callout 的数值，
    画出来是「↑ / CWE / API 模型」。只有箭头没有数字的不是指标，退成普通框。"""
    d = json.loads(json.dumps(SKELETON))
    d["bands"][1]["columns"][0]["blocks"] = [
        {"kind": "callout", "value": "↑", "label": "CWE / API 模型"},
        {"kind": "callout", "value": "→ ↓", "label": "可达性证明"},
    ]
    blocks = parse_skeleton(json.dumps(d, ensure_ascii=False)).bands[1].columns[0].blocks
    assert [(b.kind, b.value, b.label) for b in blocks] == [
        ("box", "", "CWE / API 模型"), ("box", "", "可达性证明")]


def test_parse_skeleton_keeps_real_callouts():
    """「↑80%」「百万级」是真指标，带箭头也得留着。"""
    d = json.loads(json.dumps(SKELETON))
    d["bands"][1]["columns"][0]["blocks"] = [
        {"kind": "callout", "value": "↑80%", "label": "客服响应效率"},
        {"kind": "callout", "value": "百万级", "label": "CC 攻击防护"},
    ]
    blocks = parse_skeleton(json.dumps(d, ensure_ascii=False)).bands[1].columns[0].blocks
    assert [(b.kind, b.value) for b in blocks] == [("callout", "↑80%"), ("callout", "百万级")]


def test_parse_skeleton_drops_blocks_that_are_only_arrows():
    """整块只有一个箭头、没有任何文字的，直接不要。"""
    d = json.loads(json.dumps(SKELETON))
    d["bands"][1]["columns"][0]["blocks"] = [
        {"kind": "note", "label": "↓"},
        {"kind": "box", "label": "统一数据快照"},
        {"kind": "callout", "value": "↑", "label": ""},
    ]
    blocks = parse_skeleton(json.dumps(d, ensure_ascii=False)).bands[1].columns[0].blocks
    assert [b.label for b in blocks] == ["统一数据快照"]
