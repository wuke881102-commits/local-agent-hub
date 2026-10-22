"""单篇来源的加载：解析元数据 + 按类型抽取正文。

从 agents/html_page.py 抽出来的 —— PPT 输出那条路要用同一份逻辑。
抽出来而不是复制：这段代码分流着 docx/wiki/bitable/sheet/slides/云盘文件 六种
来源，复制一份意味着以后飞书每改一次接口都要在两个地方改，而漏掉一个是静默的。
"""
from __future__ import annotations

import asyncio
import re
import shutil

from ..config import settings
from . import index_service  # noqa: F401  —— load_source 内部按需 import，这里仅表明依赖

async def load_source(ctx, token: str, inputs: dict) -> dict:
    """解析单篇来源的元数据并按类型抽取正文。

    返回 ``{token, asset_type, title, owner, space, updated, url, markdown}``。
    抽取失败抛异常（由调用方决定跳过还是终止）。
    """
    from ..services import index_service

    asset_meta: dict = {}
    for a in await index_service.list_assets(limit=2000):
        if a.get("asset_id") == token:
            asset_meta = a
            break
    asset_type = (inputs.get("asset_type") or asset_meta.get("type") or "docx").lower()

    title = asset_meta.get("title") or inputs.get("title") or "未命名文档"
    owner = asset_meta.get("owner") or "—"
    space = asset_meta.get("space") or "—"
    updated = asset_meta.get("updated") or "—"
    url = asset_meta.get("url") or ""

    if asset_type in ("docx", "doc", "wiki"):
        try:
            meta_raw = await ctx.lark.docs_get(token)
            title = meta_raw.get("title") or meta_raw.get("name") or title
            owner = meta_raw.get("owner") or meta_raw.get("owner_name") or owner
            space = meta_raw.get("space") or meta_raw.get("source_space") or space
            updated = meta_raw.get("updated") or meta_raw.get("updated_time") or updated
            url = meta_raw.get("url") or url
        except Exception as e:  # noqa: BLE001
            await ctx.log("warn", f"docs_get 失败（不阻塞）：{e}")

    # 云盘上传的 Word / HTML 文件（type=file/shortcut）：下载后本地解析成 Markdown。
    from ..services import office_reader
    office_route = office_reader.route_of(title) if asset_type in ("file", "shortcut") else None

    await ctx.log("info", f"抽取「{title}」（{office_route or asset_type}）…")
    if office_route in ("word", "html"):
        markdown = await extract_office_doc(ctx, token, title, office_route)
        # 去掉标题里的扩展名，页面标题更干净
        title = re.sub(r"\.(docx|html?|htm)$", "", title, flags=re.IGNORECASE) or title
    elif asset_type in ("docx", "doc", "wiki"):
        markdown = await ctx.lark.docs_export_markdown(token)
    elif asset_type in ("bitable", "sheet"):
        # 知识库（Wiki）托管的多维表格 / 电子表格：索引存的是 wiki 节点 token，直接调
        # base/sheets API 会报 "param baseToken is invalid"，须先解析底层 obj_token（并校正类型）。
        from ..services import table_reader
        real_token, rkind = await table_reader.resolve_token(ctx.lark, token, asset_type, url)
        if rkind == "sheet":
            markdown = await ctx.lark.sheet_fetch_summary(real_token)
        else:
            markdown = await ctx.lark.bitable_fetch_summary(real_token)
    elif asset_type == "slides":
        markdown = await ctx.lark.slides_fetch_text(token)
    else:
        raise ValueError(
            f"暂不支持的资产类型：{asset_type}"
            "（在线对象仅支持 docx/doc/wiki/bitable/sheet/slides；云盘文件支持 .docx/.html）"
        )
    if not (markdown or "").strip():
        raise ValueError("未能从该文件抽取到任何正文。")

    # 统一兜底：任何来源类型的正文都封顶 MAX_DOC_CHARS，避免超大表格/长文爆上下文。
    # （office 路径已在 _extract_office_doc 内截过，这里对其它类型再统一兜一道。）
    if len(markdown) > MAX_DOC_CHARS:
        await ctx.log("warn", f"来源「{title}」正文较长（{len(markdown)} 字），仅取前 {MAX_DOC_CHARS} 字用于生成。")
        markdown = markdown[:MAX_DOC_CHARS].rstrip() + "\n\n…（内容较长，已截断）"

    return {
        "token": token, "asset_type": asset_type, "title": title,
        "owner": owner, "space": space, "updated": updated, "url": url,
        "markdown": markdown,
    }


# 喂给 LLM 重组的正文上限（字符）。与 prompts.build_html_page_prompt 的「喂给模型闸」（12 万字）
# 对齐，让单篇长文 / 多维表格全表能完整读出；多篇合辑由 prompt 闸再统一兜底。
MAX_DOC_CHARS = 120000


async def extract_office_doc(ctx, token: str, filename: str, route: str) -> str:
    """下载云盘上传的 Word/HTML 文件并本地解析成 Markdown（超长截断）。"""
    from ..services import office_reader

    if not office_reader.available(route):
        lib = {"word": "python-docx", "html": "beautifulsoup4"}.get(route, route)
        raise ValueError(f"服务端未安装 {lib}，无法解析该文件。")
    work = settings.draft_path / f"_doc_{ctx.task_id}"
    try:
        ext = office_reader.ext_of(filename) or (".docx" if route == "word" else ".html")
        path = await office_reader.download(ctx.lark, token, work, ext)
        parser = office_reader.parse_docx if route == "word" else office_reader.parse_html
        res = await asyncio.to_thread(parser, path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    md = (res.get("markdown") or "").strip()
    if len(md) > MAX_DOC_CHARS:
        await ctx.log("warn", f"文件正文较长（{len(md)} 字），仅取前 {MAX_DOC_CHARS} 字用于重组。")
        md = md[:MAX_DOC_CHARS].rstrip() + "\n\n…（内容较长，已截断）"
    return md
