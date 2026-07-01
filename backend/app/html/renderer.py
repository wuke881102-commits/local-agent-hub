"""Lumen-light HTML 渲染器 — 把 Agent 产出的 JSON 套入单文件 HTML 模板。"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape


def _template_dir() -> Path:
    # PyInstaller frozen bundle stores data files under sys._MEIPASS.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "app" / "html" / "templates"
    return Path(__file__).parent / "templates"


TEMPLATE_DIR = _template_dir()

TEMPLATE_MAP = {
    "internal_wiki": "internal_wiki.html",
    "project": "project_show.html",
    "announcement": "announcement.html",
}


def _markdown_inline(text: str) -> str:
    """inline Markdown：**bold** / *italic* / `code` / [text](url)。

    安全策略：先 escape，再把已被 escape 的标记符还原为 HTML。
    """
    if not text:
        return ""
    import html
    out = html.escape(text)
    # 链接 [text](http...)：只允许 http/https，避免 javascript: 注入。
    out = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2" target="_blank" rel="noreferrer">\1</a>',
        out,
    )
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*(?!\*)", r"<em>\1</em>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return out


# 表格行：以 | 开头/结尾或含多个 |。分隔行形如 |---|:--:|---|。
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


# 行内表格样式（内联，避免依赖模板 CSS；与 Lumen-light 表格观感一致）。
_TBL = 'style="width:100%;border-collapse:collapse;margin:8px 0;font-size:14px"'
_TH = ('style="background:var(--surface-subtle,#F0F1F3);text-align:left;padding:8px 12px;'
       'border-bottom:1px solid var(--border-default,#DDE3EA);font-weight:600;color:var(--text-secondary,#555B61)"')
_TD = 'style="padding:8px 12px;border-bottom:1px solid var(--border-subtle,#E8ECF0);color:var(--text-secondary,#555B61)"'


def _render_table(header: str, rows: list[str]) -> str:
    ths = "".join(f"<th {_TH}>{_markdown_inline(c)}</th>" for c in _split_row(header))
    body = []
    for r in rows:
        tds = "".join(f"<td {_TD}>{_markdown_inline(c)}</td>" for c in _split_row(r))
        body.append(f"<tr>{tds}</tr>")
    return (f'<div style="overflow-x:auto"><table {_TBL}><thead><tr>{ths}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _markdown_block(text: str) -> str:
    """块级 Markdown：标题(#~####)、GFM 表格、有序/无序列表、段落。

    够覆盖飞书文档导出与 LLM 重组里常见的结构；不追求完整 CommonMark。
    """
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").split("\n")
    parts: list[str] = []
    i, n = 0, len(lines)

    def is_ul(s: str) -> bool:
        return s.lstrip().startswith(("- ", "* ", "• "))

    def is_ol(s: str) -> bool:
        return bool(re.match(r"\s*\d+[.)]\s+", s))

    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        # 标题
        m = re.match(r"(#{1,6})\s+(.*)$", stripped)
        if m:
            level = min(len(m.group(1)) + 1, 6)  # # → h2，最深 h6
            parts.append(f"<h{level}>{_markdown_inline(m.group(2).strip())}</h{level}>")
            i += 1
            continue
        # 表格：当前行像表头且下一行是分隔行
        if "|" in stripped and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            header = stripped
            j = i + 2
            body_rows: list[str] = []
            while j < n and "|" in lines[j] and lines[j].strip():
                body_rows.append(lines[j])
                j += 1
            parts.append(_render_table(header, body_rows))
            i = j
            continue
        # 有序列表
        if is_ol(stripped):
            items = []
            while i < n and is_ol(lines[i]):
                items.append(f"<li>{_markdown_inline(re.sub(r'^\s*\d+[.)]\s+', '', lines[i]))}</li>")
                i += 1
            parts.append(f"<ol>{''.join(items)}</ol>")
            continue
        # 无序列表
        if is_ul(stripped):
            items = []
            while i < n and is_ul(lines[i]):
                items.append(f"<li>{_markdown_inline(lines[i].lstrip()[2:].strip())}</li>")
                i += 1
            parts.append(f"<ul>{''.join(items)}</ul>")
            continue
        # 段落：连续非空、非块级起始的行合并
        para: list[str] = []
        while i < n and lines[i].strip() and not re.match(r"#{1,6}\s", lines[i].strip()) \
                and not is_ul(lines[i]) and not is_ol(lines[i]) \
                and not ("|" in lines[i].strip() and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1])):
            para.append(lines[i].strip())
            i += 1
        parts.append("<p>" + "<br/>".join(_markdown_inline(l) for l in para) + "</p>")
    return "\n".join(parts)


class HtmlRenderer:
    def __init__(self) -> None:
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters["md_inline"] = _markdown_inline
        self.env.filters["md_block"] = _markdown_block

    def render(self, page_type: str, payload: dict, meta: dict) -> str:
        tpl_name = TEMPLATE_MAP.get(page_type, "internal_wiki.html")
        tpl = self.env.get_template(tpl_name)
        return tpl.render(p=payload, meta=meta)

    def write(self, path: Path, html: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")


_instance: HtmlRenderer | None = None


def get_renderer() -> HtmlRenderer:
    global _instance
    if _instance is None:
        _instance = HtmlRenderer()
    return _instance
