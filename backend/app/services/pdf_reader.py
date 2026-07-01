"""PDF 读取与确定性抽取 —「PDF 识别」Agent 的底座。

职责（全部确定性，不调模型）：
  1) download()  从飞书云盘按 file_token 下载 PDF 到本地临时目录。
  2) extract()   用 PyMuPDF(fitz) 打开 PDF：
       - 逐页取文字层（page.get_text）；
       - 文字层稀疏（疑似扫描件）→ 标记 needs_ocr 并渲染该页为 PNG，交给 Agent 走视觉 OCR；
       - 有图但有文字层的页 → 在预算内也渲染，供 Agent 生成「图表说明」；
       - 用 page.find_tables() 抽表格成行列（确定性，不靠模型猜数字）。

沿用全站「规则算准 + LLM 增强」：文字、表格单元格、页数都由 Python 抽取；
模型只负责摘要、关键字段归纳、逐页要点与图表说明。
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

try:
    import fitz  # PyMuPDF
    _FITZ_AVAILABLE = True
except Exception:  # noqa: BLE001
    fitz = None  # type: ignore[assignment]
    _FITZ_AVAILABLE = False

# 调参
MAX_PAGES = 80            # 单次最多分析的页数（控制 token 与耗时）。多数报告/合同 ≤80 页
MAX_VISION_PAGES = 40     # 单份 PDF 最多触发的视觉调用数（OCR + 图表说明合计）。视觉调用最慢/最贵，
                          # 故比页数上限更保守：扫描件超过此数的后续页只取文字层、不再 OCR
OCR_TEXT_THRESHOLD = 12   # 一页文字层去空白后少于这么多字 → 视为扫描件，走 OCR
# 有图片但文字层少于这么多字 → 正文多半藏在图里（图片版订单/报价表/盖章件），整页 OCR 转写而非只描述
IMAGE_OCR_TEXT_MAX = 1500
RENDER_LONG_SIDE = 1600   # 渲染页面图片的长边像素（OCR 清晰度与体积折中）
MAX_TABLE_ROWS = 60       # 单张表最多保留的数据行
MAX_TABLE_COLS = 16       # 单张表最多保留的列


def available() -> bool:
    return _FITZ_AVAILABLE


async def download(lark, file_token: str, dest_dir: Path) -> Path:
    """从飞书云盘下载 PDF，返回本地路径。失败抛异常。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    info = await lark.drive_download_file(file_token, dest_dir, "source.pdf")
    path = Path(info["path"])
    if not path.is_file():
        raise RuntimeError("下载完成但找不到文件")
    return path


def extract(
    pdf_path: str | Path,
    *,
    max_pages: int = MAX_PAGES,
    max_vision_pages: int = MAX_VISION_PAGES,
    force_ocr: bool = False,
    want_figures: bool = True,
) -> dict:
    """打开 PDF 并做确定性抽取。返回结构见模块顶部说明。

    pages[i] = {index, text, char_count, needs_ocr, has_images,
                image_b64(None|data-url), is_figure_page}
    needs_ocr 的页：image_b64 用于 OCR，Agent 回填 ocr_text；
    is_figure_page 的页（有图但有文字层）：image_b64 用于生成图表说明。
    """
    if not _FITZ_AVAILABLE:
        raise RuntimeError("PyMuPDF(fitz) 未安装，无法解析 PDF")

    doc = fitz.open(str(pdf_path))
    try:
        page_count = doc.page_count
        analyzed = min(page_count, max(1, max_pages))
        pages: list[dict] = []
        tables: list[dict] = []
        vision_budget = max(0, max_vision_pages)

        for i in range(analyzed):
            page = doc.load_page(i)
            text = (page.get_text() or "").strip()
            nchar = len(re.sub(r"\s+", "", text))
            try:
                has_images = len(page.get_images(full=True)) > 0
            except Exception:  # noqa: BLE001
                has_images = False
            text_sparse = nchar < OCR_TEXT_THRESHOLD
            # 整页 OCR 转写触发：强制 / 真·扫描件 / 有图但文字层很薄（正文多半在图里，
            # 如图片版订单、报价表、盖章扫描页）——只描述图会丢掉里面的数字。
            do_full_ocr = force_ocr or text_sparse or (has_images and nchar < IMAGE_OCR_TEXT_MAX)
            # 仅图示说明：有图但文字层完整（普通图文页），只需一句话描述图表。
            do_describe = want_figures and has_images and not do_full_ocr

            image_b64 = None
            is_figure_page = False
            if (do_full_ocr or do_describe) and vision_budget > 0:
                try:
                    image_b64 = _render_page(page)
                    vision_budget -= 1
                    is_figure_page = do_describe
                except Exception:  # noqa: BLE001
                    image_b64 = None

            needs_ocr = do_full_ocr

            # 表格抽取（确定性；find_tables 是启发式，谨慎兜底）
            try:
                finder = page.find_tables()
                for ti, t in enumerate(getattr(finder, "tables", []) or []):
                    norm = _normalize_table(t.extract())
                    if norm:
                        norm["page"] = i + 1
                        norm["index"] = ti
                        tables.append(norm)
            except Exception:  # noqa: BLE001
                pass

            pages.append({
                "index": i + 1,
                "text": text,
                "char_count": nchar,
                "needs_ocr": needs_ocr,
                "text_sparse": text_sparse,
                "has_images": has_images,
                "image_b64": image_b64,
                "is_figure_page": is_figure_page,
            })

        # "扫描页"仅指文字层为空的真·扫描件；图片版正文页归到 OCR 页统计。
        scanned = sum(1 for p in pages if p.get("text_sparse"))
        return {
            "page_count": page_count,
            "analyzed_pages": analyzed,
            "truncated": page_count > analyzed,
            "scanned_pages": scanned,
            "table_count": len(tables),
            "pages": pages,
            "tables": tables,
        }
    finally:
        doc.close()


def assemble_full_text(pages: list[dict], *, per_page_cap: int = 6000) -> str:
    """把逐页文字（OCR 页用回填的 ocr_text）拼成带页码标记的全文。"""
    parts: list[str] = []
    for p in pages:
        body = (p.get("ocr_text") or p.get("text") or "").strip()
        if not body:
            continue
        if len(body) > per_page_cap:
            body = body[:per_page_cap] + " …（本页过长已截断）"
        tag = "（OCR）" if p.get("needs_ocr") else ""
        parts.append(f"=== 第 {p['index']} 页{tag} ===\n{body}")
    return "\n\n".join(parts)


def _render_page(page) -> str:
    """把一页渲染成 PNG 的 data URL（长边约 RENDER_LONG_SIDE 像素）。"""
    rect = page.rect
    long_side = max(rect.width, rect.height) or 1.0
    zoom = min(3.0, max(1.0, RENDER_LONG_SIDE / long_side))
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    png = pix.tobytes("png")
    b64 = base64.b64encode(png).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _normalize_table(data) -> dict | None:
    """把 find_tables().extract() 的二维数组清洗成 {headers, rows, n_rows, n_cols}。

    清洗：单元格转字符串（多行内容合并为单行）、去掉全空行/列、首个非空行作表头；行列截断。
    并加「质量闸」滤掉把信息图/图形误判成表格的假阳性——find_tables 是启发式，遇到
    阶梯图、矩阵热力图、带框线的信息图常吐出：1 列文本块、大半是空格的对角网格、或把整段
    标签塞进一个表头格。这类「表格」展示给用户只会是一坨乱码，宁可不显示。
    """
    # 单元格内换行统一成空格——多行表头/单元格在网页里渲染会错位，且常是信息图特征。
    rows = [[("" if c is None else " ".join(str(c).split())) for c in (row or [])] for row in (data or [])]
    rows = [r for r in rows if any(c for c in r)]
    if len(rows) < 2:
        return None
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    keep = [j for j in range(ncol) if any(r[j] for r in rows)]
    if not keep:
        return None
    rows = [[r[j] for j in keep[:MAX_TABLE_COLS]] for r in rows]
    headers = rows[0]
    body = rows[1:1 + MAX_TABLE_ROWS]
    if not headers or not body:
        return None

    # ── 质量闸 ──
    ncols = len(headers)
    grid = [headers] + body
    nrows = len(grid)
    # 1) 单列不成表（多是被框线圈住的一段信息图文字）。
    if ncols < 2:
        return None
    # 2) 填充率过低 → 多为对角/阶梯信息图（每行只有 1 格有值）。真·数据表通常填得满。
    nonempty = sum(1 for r in grid for c in r if c)
    fill = nonempty / float(ncols * nrows)
    if fill < 0.35:
        return None
    # 3) 表头某格异常长（把整段标签拼进一个格）且整体偏空 → 信息图，不是表。
    if any(len(h) > 80 for h in headers) and fill < 0.6:
        return None
    return {
        "headers": headers,
        "rows": body,
        "n_rows": len(rows) - 1,
        "n_cols": len(headers),
    }
