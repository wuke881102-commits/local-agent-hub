"""上传到云盘的 Office / HTML 文件解析（下载 → 本地解析成文本 / 表格）。

飞书云盘里 ``type=file`` 的 ``.xlsx/.docx/.html`` 是**上传的二进制文件**，不是飞书
原生在线对象（在线文档 / 电子表格 / 多维表格），没有内容 API，只能
``drive +download`` 拿原始字节后**本地解析**。三条路线：

- ``.xlsx/.xlsm/.xls`` → openpyxl，按工作表拆成 ``[{id,name,headers,rows,sampled}]``，
  形状与 ``table_reader`` 的单表一致，直接喂「表格分析」出图。
- ``.docx``           → python-docx，段落（含标题层级）+ 表格 → Markdown，喂「生成 HTML」。
- ``.html/.htm``      → BeautifulSoup(html.parser)，取正文 → Markdown，喂「生成 HTML」。

解析库都是**可选依赖**：缺了就降级（``available()`` 反映状态），不影响其它功能。
单元格规范化：数值保留数值（供统计），日期 → ISO 字符串，其余 → 文本。
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any

from . import table_profile

try:  # openpyxl：解析 .xlsx
    import openpyxl  # noqa: F401
    _XLSX = True
except Exception:  # noqa: BLE001
    _XLSX = False

try:  # python-docx：解析 .docx（import 名是 docx）
    import docx  # noqa: F401
    _DOCX = True
except Exception:  # noqa: BLE001
    _DOCX = False

try:  # BeautifulSoup：解析 .html（解析器用 stdlib html.parser，无需 lxml）
    from bs4 import BeautifulSoup  # noqa: F401
    _HTML = True
except Exception:  # noqa: BLE001
    _HTML = False


EXCEL_EXTS = (".xlsx", ".xlsm", ".xls")
WORD_EXTS = (".docx",)
HTML_EXTS = (".html", ".htm")

MAX_SHEET_ROWS = 500   # 每个工作表最多取的数据行（与 table_reader.SAMPLE_ROWS 对齐）
MAX_SHEET_COLS = 50    # 最多取的列数


def ext_of(name: str) -> str:
    """文件名 → 小写扩展名（含点），无扩展名返回空串。"""
    name = (name or "").strip().lower()
    dot = name.rfind(".")
    return name[dot:] if dot >= 0 else ""


def route_of(name: str) -> str | None:
    """文件名 → 处理路线：``'excel'`` | ``'word'`` | ``'html'`` | ``None``（不支持）。"""
    e = ext_of(name)
    if e in EXCEL_EXTS:
        return "excel"
    if e in WORD_EXTS:
        return "word"
    if e in HTML_EXTS:
        return "html"
    return None


def available(route: str) -> bool:
    """该路线的解析库是否就绪。"""
    return {"excel": _XLSX, "word": _DOCX, "html": _HTML}.get(route, False)


async def download(lark, file_token: str, dest_dir: Path, ext: str) -> Path:
    """下载云盘文件到 ``dest_dir/source<ext>``（复用 PDF 识别同款 drive +download）。

    用固定 ASCII 文件名（避免中文文件名在 lark-cli 沙箱 ``--output`` 处出问题），
    扩展名保留以便解析器识别格式。
    """
    safe = "source" + (ext if ext else "")
    info = await lark.drive_download_file(file_token, dest_dir, safe)
    return Path(info["path"])


# ── Excel ──────────────────────────────────────────────────────────

def parse_xlsx(path: Path, *, max_rows: int = MAX_SHEET_ROWS, max_cols: int = MAX_SHEET_COLS) -> list[dict]:
    """解析 .xlsx → ``[{id, name, headers, rows, sampled}]``，每个可见工作表一项。

    第 0 行作表头，其余为数据；裁掉右侧整列全空、尾部整行全空的噪声。空表（无表头）跳过。
    """
    import openpyxl
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    out: list[dict] = []
    try:
        for si, ws in enumerate(wb.worksheets):
            if getattr(ws, "sheet_state", "visible") != "visible":
                continue
            grid: list[list] = []
            for ri, row in enumerate(ws.iter_rows(values_only=True)):
                if ri >= max_rows + 1:  # +1 给表头
                    break
                grid.append([_norm_cell(c) for c in row[:max_cols]])
            headers, rows = _split_grid(grid)
            if not headers:
                continue
            out.append({
                "id": f"xlsx-{si}",
                "name": ws.title or f"工作表{si + 1}",
                "headers": headers,
                "rows": rows,
                "sampled": len(rows) >= max_rows,
            })
    finally:
        wb.close()
    return out


def _norm_cell(v: Any) -> Any:
    """openpyxl 单元格值 → 规范标量：数值保留，日期 → ISO 字符串，其余 → 文本/None。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, _dt.datetime):
        if v.hour == 0 and v.minute == 0 and v.second == 0:
            return v.date().isoformat()
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, _dt.date):
        return v.isoformat()
    s = str(v).strip()
    return s or None


def _split_grid(grid: list[list]) -> tuple[list[str], list[list]]:
    """第 0 行作表头，其余为数据；裁掉右侧全空列、尾部全空行。与 table_reader.split_header 同逻辑。"""
    if not grid:
        return [], []

    def nonempty(v: Any) -> bool:
        fv = table_profile.flatten_cell(v)
        return fv is not None and str(fv).strip() != ""

    width = max((len(r) for r in grid), default=0)
    used_cols = 0
    for c in range(width):
        if any(c < len(r) and nonempty(r[c]) for r in grid):
            used_cols = c + 1
    grid = [[(r[c] if c < len(r) else None) for c in range(used_cols)] for r in grid]

    while grid and not any(nonempty(c) for c in grid[-1]):
        grid.pop()
    if not grid:
        return [], []

    headers = [str(table_profile.flatten_cell(c) or f"列{i + 1}") for i, c in enumerate(grid[0])]
    return headers, grid[1:]


# ── Word（.docx）────────────────────────────────────────────────────

def parse_docx(path: Path) -> dict:
    """解析 .docx → ``{markdown, title}``。按文档顺序保留段落（含标题层级）与表格。"""
    import docx
    d = docx.Document(str(path))
    lines: list[str] = []
    title = ""
    for block in _iter_block_items(d):
        kind = block.__class__.__name__
        if kind == "Paragraph":
            text = (block.text or "").strip()
            if not text:
                continue
            style = (block.style.name if block.style else "") or ""
            sl = style.lower()
            if sl.startswith("heading"):
                lvl = _heading_level(style)
                lines.append("#" * lvl + " " + text)
                if not title and lvl <= 2:
                    title = text
            elif sl.startswith("title"):
                lines.append("# " + text)
                if not title:
                    title = text
            elif sl.startswith("list"):
                lines.append("- " + text)
            else:
                lines.append(text)
        else:  # Table
            md = _docx_table_md(block)
            if md:
                lines.append(md)
    return {"markdown": "\n\n".join(lines), "title": title}


def _heading_level(style_name: str) -> int:
    m = re.search(r"(\d+)", style_name or "")
    return min(int(m.group(1)), 6) if m else 1


def _docx_table_md(table) -> str:
    rows: list[list[str]] = []
    for r in table.rows:
        rows.append([re.sub(r"\s+", " ", (c.text or "")).strip() for c in r.cells])
    rows = [r for r in rows if any(cell for cell in r)]
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * ncol) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _iter_block_items(doc):
    """按文档顺序产出段落与表格（python-docx 官方推荐写法，公共 API 不保证顺序）。"""
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    for child in doc.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, doc)
        elif isinstance(child, CT_Tbl):
            yield Table(child, doc)


# ── HTML（.html/.htm）──────────────────────────────────────────────

def parse_html(path: Path) -> dict:
    """解析 .html → ``{markdown, title}``。剥脚本/样式，标题转 #，其余取纯文本并压缩空行。"""
    from bs4 import BeautifulSoup
    raw = path.read_bytes()
    soup = BeautifulSoup(raw, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()

    # 标题标签转 Markdown 前缀，便于后续重组保留层级
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            txt = h.get_text(" ", strip=True)
            h.string = ("\n" + "#" * level + " " + txt + "\n") if txt else ""

    body = soup.body or soup
    text = body.get_text("\n")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    md = "\n".join(ln for ln in lines if ln)
    if not title:
        first = next((ln for ln in md.splitlines() if ln.strip()), "")
        title = first.lstrip("# ").strip()[:80]
    return {"markdown": md, "title": title}
