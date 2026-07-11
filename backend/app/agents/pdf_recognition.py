"""PDF 识别 Agent — 飞书云盘 PDF 的 AI 识别。

指向飞书云盘里的一份 PDF（file_token），流程：
  1) drive +download 下载 PDF 到本地临时目录          ← feishu.cli.drive_download_file
  2) PyMuPDF 确定性抽取：逐页文字层 + 表格；扫描页渲染成图   ← services.pdf_reader
  3) 视觉模型对扫描页做 OCR、对图表页做图示说明（自动判断：有文字层直接用，无则 OCR）
  4) LLM 解读：文档类型 / 摘要 / 要点 / 关键字段抽取 / 逐页要点 / 表格洞察
  5) 组装 payload（文字、表格、字段都来自确定性抽取，模型只做语义归纳）

只读，不写回。沿用「规则算准 + LLM 增强」。

输入 inputs：
  - asset_id     PDF 的 file_token（必填；也接受含 /file/<token> 的飞书链接）
  - asset_type   'file'（可选）
  - max_pages    最多分析页数（可选，默认 40）
  - force_ocr    True 则所有页都走视觉 OCR（可选）
  - want_figures True 则对含图页生成图示说明（可选，默认 True）
  - finance_mode 'auto'(默认,合同类自动测算) | 'on'(强制) | 'off'(关闭) 合同金额测算
  - template     识别模板（summary/fields/contract/pages，缺省 summary）；contract 默认开启金额测算
  - skip_llm     True 则跳过 LLM 解读（调试用）
"""
from __future__ import annotations

import asyncio
import re
import shutil

from ..config import settings
from ..services import index_service, pdf_reader, contract_finance
from .base import AgentContext, AgentResult, register_agent
from .base_analysis import _safe_parse_json, _clean_list
from ..llm.prompts import (
    build_pdf_recognition_prompt,
    build_contract_finance_prompt,
    PDF_RECOGNITION_TEMPLATES,
)

_OCR_PROMPT = (
    "你是 OCR 与文档理解助手。请**逐字转写**这张文档图片中的全部文字，按自然阅读顺序输出为纯文本。"
    "特别注意：若页面里有表格（如产品/报价/数量/单价/金额/折扣明细），务必把每一行**连同其中的每个数字**"
    "完整转写出来（可用「列1 | 列2 | …」的形式逐行列出），不要省略或概括金额、数量、日期、编号、币种符号。"
    "不要翻译、不要总结、不要寒暄，直接输出转写内容。"
)
_FIGURE_PROMPT = (
    "用一两句简洁中文描述这张文档页里的图表 / 插图 / 示意图：图的类型与关键数据或结论。"
    "只描述图，不要复述正文。"
)

_VISION_CONCURRENCY = 4


class PdfRecognitionAgent:
    id = "pdf-recognition"
    name = "PDF 识别 Agent"
    description = "下载飞书云盘 PDF，做全文抽取（含扫描件 OCR）、关键字段抽取、表格识别与逐页要点 / 图表说明。"
    writeback_allowed = False
    output_types = ["全文与摘要", "关键字段", "表格", "逐页要点"]

    async def run(self, ctx: AgentContext) -> AgentResult:
        inputs = ctx.inputs or {}
        # 数据源二选一：本地目录里的 PDF（local_path）或飞书云盘 PDF（asset_id / file_token）
        local_path = (inputs.get("local_path") or "").strip()
        if local_path:
            file_token = ""
        else:
            raw = (inputs.get("asset_id") or inputs.get("token") or inputs.get("file_token") or "").strip()
            file_token = _extract_file_token(raw)
            if not file_token:
                return AgentResult(task_id=ctx.task_id, status="failed", error="缺少 asset_id（PDF 的 file_token 或飞书链接）")

        template = (inputs.get("template") or "summary").strip().lower()
        if template not in PDF_RECOGNITION_TEMPLATES:
            template = "summary"
        custom_instruction = (inputs.get("custom_instruction") or "").strip()

        if not pdf_reader.available():
            return AgentResult(task_id=ctx.task_id, status="failed",
                               error="服务端未安装 PyMuPDF，无法解析 PDF。请先 pip install pymupdf。")

        # 1) 取得 PDF 文件：本地直接用，云盘则下载到临时目录
        work_dir = settings.draft_path / f"_pdf_{ctx.task_id}"
        if local_path:
            from pathlib import Path  # noqa: PLC0415
            p = Path(local_path)
            if not p.is_file():
                return AgentResult(task_id=ctx.task_id, status="failed", error="本地 PDF 文件不存在")
            if p.suffix.lower() != ".pdf":
                return AgentResult(task_id=ctx.task_id, status="failed", error="请选择 PDF 文件（.pdf）")
            pdf_path = p
            title = p.name
            url = ""
            space = "本地目录"
            size_kb = p.stat().st_size // 1024
            await ctx.log("info", f"读取本地 PDF「{title}」（约 {size_kb} KB），开始解析 …")
        else:
            meta = await _find_asset(file_token)
            title = (meta.get("title") if meta else None) or "未命名 PDF"
            url = (meta.get("url") if meta else "") or ""
            space = (meta.get("space") if meta else "") or ""
            await ctx.log("info", f"开始识别 PDF「{title}」")
            try:
                pdf_path = await pdf_reader.download(ctx.lark, file_token, work_dir)
                size_kb = pdf_path.stat().st_size // 1024
                await ctx.log("info", f"已下载 PDF（约 {size_kb} KB），开始解析 …")
            except Exception as e:  # noqa: BLE001
                shutil.rmtree(work_dir, ignore_errors=True)
                return AgentResult(task_id=ctx.task_id, status="failed",
                                   error=f"下载 PDF 失败：{type(e).__name__}: {str(e)[:200]}")

        try:
            # 2) 确定性抽取（fitz 是 CPU 活，丢到线程里别阻塞事件循环）
            try:
                max_pages = int(inputs.get("max_pages") or pdf_reader.MAX_PAGES)
            except (TypeError, ValueError):
                max_pages = pdf_reader.MAX_PAGES
            try:
                data = await asyncio.to_thread(
                    pdf_reader.extract, str(pdf_path),
                    max_pages=max_pages,
                    force_ocr=bool(inputs.get("force_ocr")),
                    want_figures=inputs.get("want_figures", True) is not False,
                )
            except Exception as e:  # noqa: BLE001
                return AgentResult(task_id=ctx.task_id, status="failed",
                                   error=f"解析 PDF 失败：{type(e).__name__}: {str(e)[:200]}")

            pages, tables = data["pages"], data["tables"]
            await ctx.log(
                "info",
                f"共 {data['page_count']} 页，分析 {data['analyzed_pages']} 页"
                f"{'（已截断）' if data['truncated'] else ''}；疑似扫描页 {data['scanned_pages']} 页，"
                f"抽到表格 {data['table_count']} 张",
            )

            # 3) 视觉：扫描页 OCR + 图表页图示说明
            ocr_pages, figures = await self._run_vision(ctx, pages)

            full_text = pdf_reader.assemble_full_text(pages)
            total_chars = len(re.sub(r"\s+", "", full_text))
            await ctx.log("info", f"正文组装完成，共约 {total_chars} 字（OCR 页 {ocr_pages}，图示 {len(figures)}）")

            if total_chars == 0:
                return AgentResult(task_id=ctx.task_id, status="failed",
                                   error="未能从该 PDF 抽取到任何文字（可能是加密 PDF，或扫描页且视觉模型不可用）。")

            # 4) LLM 解读
            llm_out: dict = {}
            if inputs.get("skip_llm"):
                llm_out = {"skipped": True}
            else:
                system, user = build_pdf_recognition_prompt(
                    title=title, page_count=data["page_count"], analyzed=data["analyzed_pages"],
                    scanned=data["scanned_pages"], full_text=full_text, tables=tables,
                    template=template, custom_instruction=custom_instruction,
                )
                await ctx.log("info", f"按「{PDF_RECOGNITION_TEMPLATES[template]['label']}」模板调用 {ctx.llm.text_model} 做识别归纳 …")
                try:
                    rawout = await ctx.llm.text_complete(
                        user, system=system, json_mode=True, max_tokens=2600,
                        timeout=150, retries=1,
                    )
                    parsed = _safe_parse_json(rawout) or {}
                    llm_out = {
                        "doc_type": (parsed.get("doc_type") or "").strip(),
                        "summary": (parsed.get("summary") or "").strip(),
                        "highlights": _clean_str_list(parsed.get("highlights")),
                        "key_fields": _clean_list(parsed.get("key_fields")),
                        "page_points": _clean_list(parsed.get("page_points")),
                    }
                    _merge_table_insights(tables, _clean_list(parsed.get("table_insights")))
                    await ctx.log(
                        "info",
                        f"识别完成：类型「{llm_out['doc_type'] or '未判定'}」、"
                        f"{len(llm_out['key_fields'])} 个关键字段、{len(llm_out['page_points'])} 页要点",
                    )
                except Exception as e:  # noqa: BLE001
                    await ctx.log("warn", f"LLM 解读失败（不阻塞，仅展示抽取结果）：{type(e).__name__}")
                    llm_out = {"error": f"{type(e).__name__}: {str(e)[:160]}"}

            # 4.5) 合同金额测算：合同类文档自动开启（也可用 finance_mode=on/off 强制）
            finance = None
            finance_mode = str(inputs.get("finance_mode") or "auto").strip().lower()
            doc_type = llm_out.get("doc_type", "") if isinstance(llm_out, dict) else ""
            is_contract = _looks_like_contract(doc_type, full_text)
            # 「合同台账」模板默认开启金额测算（除非用户显式 finance_mode=off）。
            want_finance = finance_mode == "on" or template == "contract"
            do_finance = (
                not inputs.get("skip_llm")
                and finance_mode != "off"
                and (want_finance or is_contract)
            )
            if do_finance:
                finance = await self._run_finance(ctx, title, full_text)

            # 5) 组装 payload
            payload = {
                "asset_id": file_token or local_path,
                "local": bool(local_path),
                "title": title,
                "url": url,
                "space": space,
                "template": template,
                "page_count": data["page_count"],
                "analyzed_pages": data["analyzed_pages"],
                "truncated": data["truncated"],
                "scanned_pages": data["scanned_pages"],
                "ocr_pages": ocr_pages,
                "figure_pages": len(figures),
                "total_chars": total_chars,
                "doc_type": llm_out.get("doc_type", ""),
                "summary": llm_out.get("summary", ""),
                "highlights": llm_out.get("highlights", []),
                "key_fields": llm_out.get("key_fields", []),
                "page_points": llm_out.get("page_points", []),
                "tables": tables,
                "figures": figures,
                "full_text_preview": full_text[:6000],
                "is_contract": bool(is_contract or finance_mode == "on"),
                "finance": finance,
                "llm": llm_out,
            }
            return AgentResult(task_id=ctx.task_id, status="done", payload=payload)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _run_vision(self, ctx: AgentContext, pages: list[dict]) -> tuple[int, list[dict]]:
        """对带渲染图的页跑视觉：扫描页 OCR → ocr_text；图表页 → 图示说明并入页文字。

        返回 (ocr_页数, figures[{page, desc}])。无可视页或视觉 mock 时安静跳过。
        """
        targets = [p for p in pages if p.get("image_b64")]
        if not targets:
            return 0, []

        n_ocr = sum(1 for p in targets if p.get("needs_ocr"))
        n_fig = len(targets) - n_ocr
        await ctx.log(
            "info",
            f"调用 {ctx.llm.vision_model} 处理 {len(targets)} 页：OCR {n_ocr} 页、图示说明 {n_fig} 页（并发 {_VISION_CONCURRENCY}，耗时较长）…",
        )
        sem = asyncio.Semaphore(_VISION_CONCURRENCY)
        figures: list[dict] = []
        ocr_done = 0

        async def _one(p: dict) -> None:
            nonlocal ocr_done
            async with sem:
                try:
                    if p.get("needs_ocr"):
                        text = await ctx.llm.vision_describe(p["image_b64"], _OCR_PROMPT, max_tokens=1500)
                        text = (text or "").strip()
                        if text and "mock" not in text.lower():
                            p["ocr_text"] = text
                            ocr_done += 1
                    else:
                        desc = await ctx.llm.vision_describe(p["image_b64"], _FIGURE_PROMPT, max_tokens=300)
                        desc = (desc or "").strip()
                        if desc and "mock" not in desc.lower() and "失败" not in desc:
                            figures.append({"page": p["index"], "desc": desc})
                            # 把图示并入该页正文，让后续摘要/逐页要点能用上
                            p["text"] = (p.get("text") or "") + f"\n\n> 🖼 图示：{desc}"
                except Exception as e:  # noqa: BLE001
                    await ctx.log("warn", f"第 {p['index']} 页视觉处理失败：{type(e).__name__}")

        await asyncio.gather(*[_one(p) for p in targets])
        # 释放 base64 大字段，避免进 payload
        for p in pages:
            p.pop("image_b64", None)
        figures.sort(key=lambda f: f["page"])
        return ocr_done, figures

    async def _run_finance(self, ctx: AgentContext, title: str, full_text: str) -> dict:
        """合同金额测算：LLM 把每笔款项读成结构化条目 → Python 按年精确加总。

        采「均衡档主跑 + 快档兜底」双档：
          · 主档 text_model（plus）抽取质量好——这是全流程最重的一次调用（把上万字正文里每一笔
            款项逐条拆成十几字段的 JSON、输出 token 也最多），plus 才读得准。单次长超时(300s，
            长扫描件合同实测 180s 会超)、不做双重重试，避免像最强档 text_model_best(preview)
            那样两次 150s 干等 5 分钟。
          · 万一 plus 超时/出错，回退 text_model_fast（flash）再跑一次(120s)，保证永不再卡死。
        实测教训：纯 flash 虽快(~87s 不超时)，但在扫描件合同上会**漏抽金额**（210万的采购协议
        被它报「未找到带金额款项」），故 flash 只作兜底、不作主跑。精确加总始终由 Python
        compute() 负责，模型只管逐笔抽取。
        """
        system, user = build_contract_finance_prompt(title=title, full_text=full_text)

        async def _call(model: str, timeout: float) -> str:
            return await ctx.llm.text_complete(
                user, system=system, json_mode=True, max_tokens=2600,
                timeout=timeout, retries=0, model=model,
            )

        primary, fallback = ctx.llm.text_model, ctx.llm.text_model_fast
        await ctx.log("info", f"识别为合同类文档，调用 {primary} 提炼各笔金额并按年测算 …")
        raw: str | None = None
        try:
            raw = await _call(primary, 300)
        except Exception as e:  # noqa: BLE001
            await ctx.log("warn", f"{primary} 金额抽取超时/失败，回退 {fallback} 重试：{type(e).__name__}")
            try:
                raw = await _call(fallback, 120)
            except Exception as e2:  # noqa: BLE001
                await ctx.log("warn", f"合同金额抽取失败（不阻塞）：{type(e2).__name__}")
                return {"error": f"{type(e2).__name__}: {str(e2)[:160]}"}

        parsed = _safe_parse_json(raw) or {}
        money_items = _clean_list(parsed.get("money_items"))
        conditional = _clean_list(parsed.get("conditional_items"))
        if not money_items and not conditional:
            await ctx.log("info", "未在合同中抽到带具体金额的款项。")
            return {"empty": True}

        result = contract_finance.compute(money_items, conditional)
        n_years = max((len(c["years"]) for c in result["by_currency"]), default=0)
        await ctx.log(
            "info",
            f"金额测算完成：{result['item_count']} 笔款项、{len(result['by_currency'])} 种币种、覆盖 {n_years} 个年度",
        )
        return result


# ── 工具 ─────────────────────────────────────────────────────────

# 触发金额测算的文档类型：合同/协议，以及订单/报价/发票/采购单等带金额条款的单据（中英）。
_FINANCE_TYPE_RE = re.compile(
    r"合同|协议|契约|订单|订购|采购|报价|发票|结算|"
    r"contract|agreement|lease|order\s*form|order\s*confirmation|purchase\s*order|"
    r"\bP\.?O\.?\b|quotation|\bquote\b|invoice|statement\s*of\s*work|\bSOW\b",
    re.I,
)
_PARTY_RE = re.compile(
    r"甲方|乙方|承租|出租|发包|承包|本协议|本合同|签署|盖章|双方|当事人|"
    r"\bpart(?:y|ies)\b|seller|buyer|vendor|customer|supplier|licens(?:ee|or)|client",
    re.I,
)
_MONEY_RE = re.compile(
    r"金额|价款|租金|费用|付款|支付|总价|价格|单价|折扣|小计|合计|￥|¥|元|万元|"
    r"\$|USD|RMB|CNY|EUR|HKD|JPY|GBP|price|amount|total|subtotal|\bfee\b|payment|discount|qty|quantity|unit\s*price",
    re.I,
)


def _looks_like_contract(doc_type: str, text: str) -> bool:
    """是否对该文档做金额测算：命中合同/订单等单据类型，或同时具备「当事方 + 金额」信号（中英通吃）。"""
    blob = (doc_type or "") + "\n" + (text or "")
    if _FINANCE_TYPE_RE.search(blob):
        return True
    return bool(_PARTY_RE.search(blob) and _MONEY_RE.search(blob))

_FILE_URL_RE = re.compile(r"/file/([A-Za-z0-9]+)")


def _extract_file_token(s: str) -> str:
    s = (s or "").strip()
    m = _FILE_URL_RE.search(s)
    return m.group(1) if m else s


async def _find_asset(asset_id: str) -> dict:
    for a in await index_service.list_assets(limit=2000):
        if a.get("asset_id") == asset_id:
            return a
    return {}


def _clean_str_list(v) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def _merge_table_insights(tables: list[dict], insights: list[dict]) -> None:
    """把 LLM 给的 {index, title, insight} 按 index 合并进确定性抽取的表。"""
    by_index = {}
    for it in insights:
        idx = it.get("index")
        if isinstance(idx, int):
            by_index[idx] = it
    for i, t in enumerate(tables):
        hit = by_index.get(t.get("index")) or by_index.get(i)
        if hit:
            if hit.get("title"):
                t["title"] = str(hit["title"]).strip()
            if hit.get("insight"):
                t["insight"] = str(hit["insight"]).strip()


register_agent(PdfRecognitionAgent())
