"""把 bitable / sheet 统一读成 (headers, rows) 结构的共享加载器 + 短期行缓存。

被两处复用：
- ``agents.base_analysis``（一次性分析任务，强制读最新数据）
- ``routes.base``（「问数据」交互，复用缓存让追问秒回）

缓存键 = ``asset_id::analyzed_id``，TTL 15 分钟。``load_table`` 总是写缓存；
``use_cache`` 只控制是否**读**缓存（分析任务传 False 以保证新鲜）。
"""
from __future__ import annotations

import asyncio
import csv
import io
import shutil
import time
from pathlib import Path
from typing import Any

from ..config import settings
from . import office_reader, table_profile

SAMPLE_ROWS = 500   # 每张表最多采样行数（bitable 单页上限 ~500）
SHEET_COLS = 50     # 电子表格最多读取列数


def detect_kind(asset_type: str | None, title: str | None = None) -> str:
    """资产类型(+文件名) → 表分析路线：``'bitable'`` | ``'sheet'`` | ``'xlsx'`` | ``''``。

    - 飞书原生多维表格 → ``bitable``，原生电子表格 → ``sheet``
    - 云盘上传的 Excel 文件（type=file/shortcut 且文件名 .xlsx/.xls）→ ``xlsx``（本地解析）
    - 其余无法判别 → ``''``（调用方自行兜底）
    """
    at = (asset_type or "").lower()
    if at in ("base", "bitable"):
        return "bitable"
    if at == "sheet":
        return "sheet"
    if at in ("file", "shortcut") and office_reader.route_of(title or "") == "excel":
        return "xlsx"
    return ""


_CACHE: dict[str, dict] = {}
_TTL = 900  # 15 分钟


def _key(asset_id: str, target_id: str | None) -> str:
    return f"{asset_id}::{target_id or ''}"


def cache_get(asset_id: str, target_id: str | None) -> dict | None:
    e = _CACHE.get(_key(asset_id, target_id))
    if e and time.time() - e["at"] <= _TTL:
        return e["data"]
    return None


def cache_put(asset_id: str, target_id: str | None, data: dict) -> None:
    _CACHE[_key(asset_id, target_id)] = {"data": data, "at": time.time()}


def split_header(grid: list[list]) -> tuple[list[str], list[list]]:
    """电子表格：第 0 行作表头，其余为数据；裁掉整列 / 尾部整行全空的噪声。"""
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


async def _load_bitable(lark, app_token: str, table_id: str | None) -> dict:
    tables = await lark.base_list_tables(app_token)
    if not tables:
        raise RuntimeError("该多维表格下没有可读的数据表")
    chosen = next((t for t in tables if t["table_id"] == table_id), None) or tables[0]
    headers, rows = await lark.base_table_records(app_token, chosen["table_id"], limit=SAMPLE_ROWS)
    return {
        "kind": "bitable",
        "targets": [{"id": t["table_id"], "name": t["name"]} for t in tables],
        "analyzed": {"id": chosen["table_id"], "name": chosen["name"]},
        "headers": headers, "rows": rows, "sampled": len(rows) >= SAMPLE_ROWS,
    }


async def _load_sheet(lark, token: str, sheet_id: str | None) -> dict:
    sheets = await lark.sheet_list_sheets(token)
    if not sheets:
        raise RuntimeError("该电子表格下没有可读的工作表")
    chosen = next((s for s in sheets if s["sheet_id"] == sheet_id), None) or sheets[0]
    grid = await lark.sheet_read_grid(token, chosen["sheet_id"], max_rows=SAMPLE_ROWS, max_cols=SHEET_COLS)
    headers, rows = split_header(grid)
    return {
        "kind": "sheet",
        "targets": [{"id": s["sheet_id"], "name": s["title"]} for s in sheets],
        "analyzed": {"id": chosen["sheet_id"], "name": chosen["title"]},
        "headers": headers, "rows": rows, "sampled": len(rows) >= SAMPLE_ROWS - 1,
    }


async def _resolve_token(lark, asset_id: str, kind: str, url: str | None) -> tuple[str, str]:
    """把"知识库托管"的表 token 解析成 base/sheets API 能用的底层 obj_token。

    知识库（Wiki）里的多维表格 / 电子表格，索引存的是 **wiki 节点 token**（url 含
    ``/wiki/``），直接拿去调 base/sheets API 会被拒（``param baseToken is invalid``）。
    这里识别出 wiki 资产并解析出真实 obj_token，顺便用解析返回的 obj_type 校正 kind
    （以底层对象为准，比索引里的 type 更可靠）。直连的 ``/base/`` ``/sheets/`` token
    无需解析，原样返回。
    """
    if not url or "/wiki/" not in url:
        return asset_id, kind
    getter = getattr(lark, "wiki_get_node", None)
    if getter is None:  # Mock 等不支持解析时，原样返回
        return asset_id, kind
    node = await getter(url)
    obj = node.get("obj_token") if isinstance(node, dict) else None
    if not obj:
        return asset_id, kind
    ot = node.get("obj_type") or ""
    if ot in ("bitable", "base"):
        kind = "bitable"
    elif ot == "sheet":
        kind = "sheet"
    return obj, kind


async def resolve_token(lark, asset_id: str, kind: str, url: str | None = None) -> tuple[str, str]:
    """公开入口：把知识库（Wiki）托管的表 token 解析成底层 obj_token，并校正 kind。

    供需要直接调 base/sheets API 的其它模块（如「生成 HTML」抽取 bitable/sheet 来源）复用，
    避免拿 wiki 节点 token 调 API 被拒（``param baseToken is invalid``）。详见 ``_resolve_token``。
    """
    return await _resolve_token(lark, asset_id, kind, url)


def _parse_local_csv(path: Path) -> list[dict]:
    """本地 CSV → 单工作表 ``[{id,name,headers,rows,sampled}]``。"""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    headers = [str(h).strip() for h in rows[0]]
    body = [[("" if c is None else str(c)) for c in r] for r in rows[1:SAMPLE_ROWS + 1]]
    return [{"id": "csv", "name": path.stem, "headers": headers, "rows": body,
             "sampled": len(rows) - 1 > SAMPLE_ROWS}]


async def _read_xlsx_sheets(lark, file_token: str, filename: str | None,
                            local_path: str | None = None) -> list[dict]:
    """解析 Excel/CSV → 工作表列表 ``[{id,name,headers,rows,sampled}]``。

    ``local_path`` 给定时直接读本地文件（本地目录数据源），否则下载云盘 ``file_token``。
    """
    if not office_reader.available("excel"):
        raise RuntimeError("服务端未安装 openpyxl，无法解析 Excel 文件。")
    if local_path:
        p = Path(local_path)
        if not p.is_file():
            raise RuntimeError("本地表格文件不存在")
        if p.suffix.lower() == ".csv":
            sheets = await asyncio.to_thread(_parse_local_csv, p)
        else:
            sheets = await asyncio.to_thread(office_reader.parse_xlsx, p)
    else:
        work = settings.draft_path / f"_xlsx_{file_token[:16]}"
        try:
            ext = office_reader.ext_of(filename or "") or ".xlsx"
            path = await office_reader.download(lark, file_token, work, ext)
            sheets = await asyncio.to_thread(office_reader.parse_xlsx, path)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if not sheets:
        raise RuntimeError("该表格文件里没有可读的工作表 / 数据。")
    return sheets


def _xlsx_entry(sheet: dict, targets: list[dict]) -> dict:
    """单个 Excel 工作表 → 与 bitable/sheet 同形的结果块（kind 标为 sheet 复用下游全部逻辑）。"""
    return {
        "kind": "sheet",
        "targets": targets,
        "analyzed": {"id": sheet["id"], "name": sheet["name"]},
        "headers": sheet["headers"], "rows": sheet["rows"], "sampled": sheet["sampled"],
    }


async def load_table(
    lark, asset_id: str, kind: str, *,
    table_id: str | None = None, sheet_id: str | None = None,
    use_cache: bool = True, url: str | None = None, filename: str | None = None,
) -> dict:
    """读一张表 → {kind, targets, analyzed, headers, rows, sampled}。

    use_cache=True 时优先命中缓存（按请求的 target 或已解析的 analyzed.id）。
    无论如何都会把结果写回缓存（键用解析出的 analyzed.id），供后续问数据复用。

    ``url`` 用于识别知识库托管的表并解析底层 obj_token（见 _resolve_token）。
    ``kind='xlsx'`` 时 ``filename`` 用于确定扩展名，``asset_id`` 即云盘 file_token。
    """
    target_id = table_id if kind == "bitable" else sheet_id
    if use_cache:
        cached = cache_get(asset_id, target_id)
        if cached:
            return cached

    if kind == "xlsx":
        sheets = await _read_xlsx_sheets(lark, asset_id, filename)
        targets = [{"id": s["id"], "name": s["name"]} for s in sheets]
        chosen = next((s for s in sheets if s["id"] == target_id), None) or sheets[0]
        data = _xlsx_entry(chosen, targets)
        cache_put(asset_id, chosen["id"], data)
        return data

    real_token, kind = await _resolve_token(lark, asset_id, kind, url)

    if kind == "bitable":
        data = await _load_bitable(lark, real_token, table_id)
    else:
        data = await _load_sheet(lark, real_token, sheet_id)

    # 缓存键仍用原始 asset_id（前端/索引用它），不用解析后的 obj_token。
    cache_put(asset_id, data["analyzed"]["id"], data)
    return data


MAX_TABLES = 25  # 「一次分析全部表」的上限，避免超大多维表格跑飞（耗时 / Token）


async def load_all_tables(
    lark, asset_id: str, kind: str, *,
    url: str | None = None, filename: str | None = None, max_tables: int = MAX_TABLES,
    local_path: str | None = None,
) -> dict:
    """读多维表格 / 电子表格里的【所有】子表 → {kind, targets, tables, truncated}。

    ``tables`` 是逐张表的完整数据，与单表 ``load_table`` 同形
    （{kind, targets, analyzed, headers, rows, sampled}），且每张都写入行缓存
    （键=该表自己的 id），供「问数据」逐表复用。超过 ``max_tables`` 的部分截断，
    ``truncated=True``。分析任务专用：总读最新，不读缓存。

    ``kind='xlsx'`` 时走本地解析云盘上传的 Excel（``asset_id`` = file_token，
    ``filename`` 定扩展名），每个工作表当作一张子表。
    """
    if kind == "xlsx":
        sheets = await _read_xlsx_sheets(lark, asset_id, filename, local_path=local_path)
        targets = [{"id": s["id"], "name": s["name"]} for s in sheets]
        truncated = len(targets) > max_tables
        tables: list[dict] = []
        for s in sheets[:max_tables]:
            entry = _xlsx_entry(s, targets)
            cache_put(asset_id, s["id"], entry)
            tables.append(entry)
        return {"kind": "sheet", "targets": targets, "tables": tables, "truncated": truncated}

    real_token, kind = await _resolve_token(lark, asset_id, kind, url)

    if kind == "bitable":
        metas = await lark.base_list_tables(real_token)
        if not metas:
            raise RuntimeError("该多维表格下没有可读的数据表")
        targets = [{"id": t["table_id"], "name": t["name"]} for t in metas]
    else:
        metas = await lark.sheet_list_sheets(real_token)
        if not metas:
            raise RuntimeError("该电子表格下没有可读的工作表")
        targets = [{"id": s["sheet_id"], "name": s["title"]} for s in metas]

    truncated = len(targets) > max_tables
    tables: list[dict] = []
    for i, _t in enumerate(targets[:max_tables]):
        tid = _t["id"]
        if kind == "bitable":
            headers, rows = await lark.base_table_records(real_token, tid, limit=SAMPLE_ROWS)
            sampled = len(rows) >= SAMPLE_ROWS
        else:
            grid = await lark.sheet_read_grid(real_token, tid, max_rows=SAMPLE_ROWS, max_cols=SHEET_COLS)
            headers, rows = split_header(grid)
            sampled = len(rows) >= SAMPLE_ROWS - 1
        entry = {
            "kind": kind,
            "targets": targets,
            "analyzed": {"id": tid, "name": _t["name"]},
            "headers": headers, "rows": rows, "sampled": sampled,
        }
        cache_put(asset_id, tid, entry)   # 与单表缓存同键，问数据可逐表命中
        tables.append(entry)

    return {"kind": kind, "targets": targets, "tables": tables, "truncated": truncated}
