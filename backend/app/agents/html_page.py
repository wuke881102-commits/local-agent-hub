"""HTML 页面生成 Agent — Phase A 核心实现。

输入：{ doc_token / doc_tokens, page_type, template_options? }
流程：
  1) lark.docs_get(token)          → 拿元数据
  2) lark.docs_export_markdown(token) → 拿正文
  3) （可选）扫描图片，调 llm.vision_describe 生成 alt（Phase A 暂跳过外链图片下载，仅记录）
  4) llm.text_complete(..., json_mode=True) → 拿结构化 payload
  5) renderer.render(page_type, payload, meta) → 单文件 HTML
  6) 写入 data/drafts/{task_id}.html，仅供本地预览 / 下载

注：内容生产不写回飞书——只产出本地 HTML 草稿，不创建飞书文档。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import shutil

from ..config import settings
from ..html import get_renderer
from ..llm import get_llm
from ..llm.prompts import build_html_page_prompt, build_html_freeform_prompt
from ..services import doc_images
from .base import Agent, AgentContext, AgentResult, register_agent


class HtmlPageAgent:
    id = "html-page"
    name = "HTML 页面生成 Agent"
    description = "把飞书文档套入 Lumen-light 模板，生成可预览的企业内部 HTML 页面。"
    writeback_allowed = False
    # 内容重组走均衡档（text_model，默认 qwen3.7-plus）：原本用最强 text_model_best
    # （qwen3.7-max），但 preview 模型在 UI 任务里经常超时（4 分钟仍 Request timed out）。
    # 均衡档又快又稳、重组质量足够，故固定走它。实际路由见 run() 里的 model= 传参。
    default_model = "qwen3.7-plus"
    output_types = ["HTML 页面", "本地预览", "来源引用清单", "生成说明"]

    PAGE_TYPES = ["internal_wiki", "project", "announcement", "custom"]
    MAX_SOURCES = 3  # 一次最多合并 3 篇来源文档

    async def run(self, ctx: AgentContext) -> AgentResult:
        inputs = ctx.inputs or {}
        page_type = inputs.get("page_type", "internal_wiki")
        if page_type not in self.PAGE_TYPES:
            page_type = "internal_wiki"
        custom_instruction = (inputs.get("custom_instruction") or "").strip()
        # 版式：template（默认，套 Lumen-light 模板，稳定一致）| freeform（AI 直出完整 HTML，版式丰富）
        layout_mode = (inputs.get("layout_mode") or "template").lower()
        if layout_mode not in ("template", "freeform"):
            layout_mode = "template"

        # 收集来源 token：支持 doc_tokens 列表（最多 3），向后兼容单个 doc_token/asset_id。
        raw_tokens = inputs.get("doc_tokens")
        if isinstance(raw_tokens, str):
            raw_tokens = [raw_tokens]
        tokens: list[str] = [t for t in (raw_tokens or []) if t]
        single = inputs.get("doc_token") or inputs.get("asset_id")
        if single and single not in tokens:
            tokens.insert(0, single)
        # 去重保序，最多 3 篇
        seen: set[str] = set()
        tokens = [t for t in tokens if not (t in seen or seen.add(t))][:self.MAX_SOURCES]

        if not tokens:
            return AgentResult(
                task_id=ctx.task_id, status="failed", error="缺少 doc_token / doc_tokens 参数"
            )

        await ctx.log("info", f"开始 HTML 页面生成，{len(tokens)} 篇来源，模板 {page_type}")

        # 逐篇解析元数据 + 抽取正文；单篇失败则跳过，全部失败才终止。
        sources: list[dict] = []
        for tok in tokens:
            try:
                sources.append(await _load_source(ctx, tok, inputs))
            except Exception as e:  # noqa: BLE001
                await ctx.log("warn", f"来源 {tok[:12]}… 抽取失败，已跳过：{e}")
        if not sources:
            return AgentResult(
                task_id=ctx.task_id, status="failed", error="所有来源抽取失败，无可用内容",
            )

        # 合并多篇来源为一份 Markdown，带清晰的来源分隔；单篇则直接沿用。
        if len(sources) == 1:
            s0 = sources[0]
            title, owner, space, updated = (
                s0["title"], s0["owner"], s0["space"], s0["updated"],
            )
            markdown = s0["markdown"]
        else:
            parts = [
                f"# 来源 {i}：{s['title']}（{s['asset_type']}）\n\n{s['markdown']}"
                for i, s in enumerate(sources, 1)
            ]
            markdown = "\n\n---\n\n".join(parts)
            primary = sources[0]
            title = f"{primary['title']} 等 {len(sources)} 篇合辑"
            owner, space, updated = (
                primary["owner"], primary["space"], primary["updated"],
            )

        await ctx.log("info", f"已合并 {len(sources)} 篇来源，总长度 {len(markdown)} 字符")

        # 3. 图片识别：文档内嵌图片以 <image token=".."/> 内联，用视觉模型
        #    （gpt-4.1-mini）逐张做 OCR + 图示，把内容回填进 Markdown，供后续重组与渲染。
        #    与「会议纪要」「PDF 识别」共用 services.doc_images 的统一读图能力。
        figures: list[str] = []
        describe_images = inputs.get("describe_images", True)
        if describe_images:
            try:
                # max_images=0 → 识别全部内嵌图片（默认）
                markdown, figures = await doc_images.describe_inline_images(
                    ctx, markdown, max_images=int(inputs.get("max_images", 0)),
                )
                if figures:
                    await ctx.log("info", f"已用 {ctx.llm.vision_model} 识别 {len(figures)} 张图片并写入正文")
            except Exception as e:  # noqa: BLE001
                await ctx.log("warn", f"图片识别整体失败（不阻塞）：{e}")

        # 4'. 自由版式：让模型直出完整 HTML（套内置 Lumen-light 设计系统），不走 JSON+模板。
        if layout_mode == "freeform":
            return await self._run_freeform(
                ctx, page_type=page_type, title=title, space=space, owner=owner,
                updated=updated, markdown=markdown, custom_instruction=custom_instruction,
                sources=sources, figures=figures,
            )

        # 4. LLM 重组（走均衡档 text_model，又快又稳；preview 模型常超时已弃用）
        await ctx.log("info", f"调用模型 {ctx.llm.text_model} 重组内容（最长 ~4 分钟）…")
        system, user_prompt = build_html_page_prompt(
            page_type=page_type, title=title, space=space, owner=owner, updated=updated, markdown=markdown,
            custom_instruction=custom_instruction,
        )
        # max_tokens 8192：尽量装下更饱满的章节正文。输入上限放宽到 12 万字后，prefill 变长，
        # timeout 同步 240→300s 留余量（输出 8192≈170s + 大输入 prefill ~15–30s，300s 仍宽裕）。
        raw_text = await ctx.llm.text_complete(
            user_prompt, system=system, json_mode=True, max_tokens=8192,
            timeout=300, retries=1, model=ctx.llm.text_model,
        )

        payload = _safe_parse_json(raw_text)
        if not payload:
            return AgentResult(
                task_id=ctx.task_id, status="failed",
                error="模型输出无法解析为 JSON",
                payload={"raw": raw_text[:400]},
            )
        # 补字段
        payload.setdefault("title", title)
        payload.setdefault("page_type", page_type)
        if "tags" not in payload:
            payload["tags"] = []
        # 图示摘录：由视觉识别得到，独立于 LLM 重写，忠实保留每张图要点
        if figures:
            payload["figures"] = figures

        # 指标兜底：丢弃原文中不存在的编造数字
        raw_metric_n = len(payload.get("metrics") or [])
        payload["metrics"] = _filter_real_metrics(payload.get("metrics") or [], markdown)
        dropped = raw_metric_n - len(payload["metrics"])
        if dropped > 0:
            await ctx.log("info", f"指标兜底：丢弃 {dropped} 个原文无据的编造指标")

        await ctx.log("info", f"模型返回章节数 {len(payload.get('sections') or [])}，保留指标 {len(payload.get('metrics') or [])}，图示 {len(figures)}")

        # 5. 渲染
        meta = {
            "owner": owner,
            "source_space": space,
            "updated": updated,
            "agent_name": self.name,
            "template_name": page_type,
            "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "refs": [
                {"title": s["title"], "url": s["url"], "note": "原始飞书文档"}
                for s in sources
            ],
            "vision_model": ctx.llm.vision_model,
        }
        html = get_renderer().render(page_type, payload, meta)

        # 6. 写文件
        draft_path = settings.draft_path / f"{ctx.task_id}.html"
        get_renderer().write(draft_path, html)
        await ctx.log("info", f"已写入草稿 {draft_path.name} ({len(html)} bytes)")

        # 内容生产只产出本地 HTML 草稿（预览 / 下载），不再写回飞书。
        return AgentResult(
            task_id=ctx.task_id,
            status="done",
            result_path=str(draft_path),
            payload=payload,
        )


    async def _run_freeform(
        self, ctx: AgentContext, *, page_type: str, title: str, space: str, owner: str,
        updated: str, markdown: str, custom_instruction: str, sources: list[dict], figures: list[str],
    ) -> AgentResult:
        """自由版式：模型直出完整单文件 HTML，套内置 Lumen-light 设计系统。

        与套模板路线的取舍：版式更丰富（表格/卡片/徽章/提示自由组合），代价是更慢、
        偶尔可能崩版或编内容——故用强约束 prompt + 成品 HTML 的可见文本数字核验兜底。
        """
        await ctx.log("info", f"自由版式：调用 {ctx.llm.text_model} 直出 HTML（套内置 Lumen-light 设计系统，最长 ~5 分钟）…")
        system, user = build_html_freeform_prompt(
            page_type=page_type, title=title, space=space, owner=owner,
            updated=updated, markdown=markdown, custom_instruction=custom_instruction,
        )
        # 关键：让「生成上限」与「超时」匹配——整页 HTML 是逐 token 吐出的，max_tokens 越大耗时越长。
        # 早期 16000tokens 配 240s 必然超时，retries=1 还把等待翻倍到 ~8 分钟才报错。改为单次长超时
        # + 不重试(retries=0)：实测 qwen3.7-plus 约 48 tok/s，16000 tokens≈333s。输入上限放到 12 万字后
        # prefill 多 ~15–30s，原 360s 余量被吃掉，故 timeout 提到 480s（333s 解码 + 大输入 prefill，仍留余量）。
        try:
            raw = await ctx.llm.text_complete(
                user, system=system, json_mode=False, max_tokens=16000,
                timeout=480, retries=0, model=ctx.llm.text_model,
            )
        except Exception as e:  # noqa: BLE001
            await ctx.log("warn", f"自由版式生成超时/失败：{type(e).__name__}")
            return AgentResult(
                task_id=ctx.task_id, status="failed",
                error=("自由版式生成超时。内容较大（尤其表格很多的电子表格/多维表格）时，"
                       "AI 直出整页 HTML 容易超时。建议改用「套模板」，或减少来源 / 缩短文档后重试。"),
            )
        html = _strip_html_fence(raw or "")
        if "<" not in html or "</" not in html.lower():
            return AgentResult(
                task_id=ctx.task_id, status="failed",
                error="模型未返回有效 HTML（自由版式）", payload={"raw": (raw or "")[:400]},
            )
        # 截断检测：缺 </html> 多半是被 max_tokens 截断，提示但不阻塞（仍可预览大部分）。
        if "</html>" not in html.lower():
            await ctx.log("warn", "HTML 似乎被截断（未见 </html>），页面尾部可能不完整；可改用套模板或缩短来源。")

        # 数字核验：只在「可见文本」上做（剥离 <style>/<script>/标签），避免 CSS 的 px/十六进制色误报。
        suspects = _suspect_numbers(html, markdown)
        if suspects:
            await ctx.log("warn", f"自由版式数字核验：以下数字在原文中未找到，请人工复核是否模型编造——{('、'.join(suspects))}")

        # 追加统一「来源」页脚，保证可追溯（无论模型是否自己写了来源）。
        html = _inject_sources_footer(html, sources)

        draft_path = settings.draft_path / f"{ctx.task_id}.html"
        get_renderer().write(draft_path, html)
        await ctx.log("info", f"已写入草稿 {draft_path.name} ({len(html)} bytes，自由版式)")
        return AgentResult(
            task_id=ctx.task_id, status="done", result_path=str(draft_path),
            payload={"title": title, "page_type": page_type, "layout_mode": "freeform",
                     "figures": figures, "suspect_numbers": suspects},
        )


async def _load_source(ctx, token: str, inputs: dict) -> dict:
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
        markdown = await _extract_office_doc(ctx, token, title, office_route)
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

    # 统一兜底：任何来源类型的正文都封顶 _MAX_DOC_CHARS，避免超大表格/长文爆上下文。
    # （office 路径已在 _extract_office_doc 内截过，这里对其它类型再统一兜一道。）
    if len(markdown) > _MAX_DOC_CHARS:
        await ctx.log("warn", f"来源「{title}」正文较长（{len(markdown)} 字），仅取前 {_MAX_DOC_CHARS} 字用于生成。")
        markdown = markdown[:_MAX_DOC_CHARS].rstrip() + "\n\n…（内容较长，已截断）"

    return {
        "token": token, "asset_type": asset_type, "title": title,
        "owner": owner, "space": space, "updated": updated, "url": url,
        "markdown": markdown,
    }


# 喂给 LLM 重组的正文上限（字符）。与 prompts.build_html_page_prompt 的「喂给模型闸」（12 万字）
# 对齐，让单篇长文 / 多维表格全表能完整读出；多篇合辑由 prompt 闸再统一兜底。
_MAX_DOC_CHARS = 120000


async def _extract_office_doc(ctx, token: str, filename: str, route: str) -> str:
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
    if len(md) > _MAX_DOC_CHARS:
        await ctx.log("warn", f"文件正文较长（{len(md)} 字），仅取前 {_MAX_DOC_CHARS} 字用于重组。")
        md = md[:_MAX_DOC_CHARS].rstrip() + "\n\n…（内容较长，已截断）"
    return md


def _filter_real_metrics(metrics: list, source_md: str, cap: int = 24) -> list:
    """只保留"数字确实出现在原文里"的指标，丢弃模型凭空编造的。

    判定：从 metric.value 抽取数字 token（如 70 / 18.4 / 3000），只有当其中
    某个**两位及以上**的数字串确实出现在原文中才保留。无数字的"伪指标"直接丢弃。
    这是对 prompt 指令的确定性兜底——模型有时仍会补凑假数据。
    """
    if not isinstance(metrics, list) or not metrics:
        return []
    kept = []
    for m in metrics:
        if not isinstance(m, dict):
            continue
        val = str(m.get("value", "")).strip()
        nums = re.findall(r"\d+(?:\.\d+)?", val)
        # 只用 ≥2 位的数字串判定，避免单个 "1/2/3" 在长文里到处误命中
        sig = [n for n in nums if len(n.replace(".", "")) >= 2]
        if not sig:
            continue
        if any(n in source_md for n in sig):
            kept.append(m)
        if len(kept) >= cap:
            break
    return kept


def _strip_html_fence(text: str) -> str:
    """去掉模型可能加的 ```html ... ``` 代码块包裹，并裁掉 <!doctype 之前的废话。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:html)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    # 模型偶尔在 HTML 前后加解释；从第一个 <!doctype 或 <html 截起。
    m = re.search(r"<!doctype html|<html", s, flags=re.IGNORECASE)
    if m and m.start() > 0:
        s = s[m.start():]
    return s.strip()


def _extract_visible_text(html: str) -> str:
    """剥离 <style>/<script> 与所有标签，得到用户可见文本。

    用于数字核验：CSS（16px / #00AA4F / rgba(...)）与脚本里的数字不应参与判定，
    只看真正展示给读者的文本节点。"""
    s = re.sub(r"<(style|script)\b[^>]*>[\s\S]*?</\1>", " ", html, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)            # 去标签（连同 style="..." 等属性）
    import html as _h
    return _h.unescape(s)


def _suspect_numbers(html: str, source_md: str, cap: int = 12) -> list[str]:
    """成品 HTML 可见文本里、原文中找不到的「≥3 位」数字串（疑似编造）。

    阈值取 3 位：避免年份分段、序号 1/2/3、百分比里的个位等噪音；3 位以上的
    金额/数量/统计若凭空出现，更值得人工复核。仅告警、不删改（自由 HTML 难精确改写）。"""
    text = _extract_visible_text(html)
    nums = re.findall(r"\d[\d,\.]{2,}", text)   # 至少 3 个字符的数字串（含千分位/小数）
    seen: set[str] = set()
    out: list[str] = []
    for n in nums:
        digits = n.replace(",", "").replace(".", "")
        if len(digits) < 3:
            continue
        if n in seen:
            continue
        seen.add(n)
        # 原文里以「带分隔符原样」或「纯数字」任一形式出现即视为有据。
        if n in source_md or digits in source_md.replace(",", ""):
            continue
        out.append(n)
        if len(out) >= cap:
            break
    return out


def _inject_sources_footer(html: str, sources: list[dict]) -> str:
    """在 </body> 前追加统一「来源」页脚，保证可追溯（不依赖模型自觉）。"""
    if not sources:
        return html
    import html as _h
    items = []
    for s in sources:
        title = _h.escape(s.get("title") or "未命名文档")
        url = _h.escape(s.get("url") or "")
        items.append(
            f'<li><a href="{url}" target="_blank" rel="noreferrer" '
            f'style="color:#006845;text-decoration:none">{title}</a></li>' if url
            else f"<li>{title}</li>"
        )
    footer = (
        '<footer style="max-width:1200px;margin:48px auto 24px;padding:16px 32px;'
        'border-top:1px solid #DDE3EA;color:#737A82;font-size:13px;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif\">"
        '<div style="font-weight:600;margin-bottom:6px">来源 · 飞书原始文档</div>'
        f'<ul style="margin:0;padding-left:20px;line-height:1.8">{"".join(items)}</ul>'
        "</footer>"
    )
    if re.search(r"</body>", html, flags=re.IGNORECASE):
        return re.sub(r"</body>", footer + "</body>", html, count=1, flags=re.IGNORECASE)
    return html + footer


def _safe_parse_json(text: str) -> dict | None:
    if not text:
        return None
    # 去掉可能的 ```json fence
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # 尝试抓第一个 {…}
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


register_agent(HtmlPageAgent())
