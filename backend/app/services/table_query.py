"""自然语言问表 → 结构化查询的确定性执行器。

分工延续「规则算准 + LLM 翻译」：LLM 只把问题翻译成 query spec
（filters / group_by / metric / sort / limit），真正的筛选与聚合由这里用
Python 在已加载的行上**精确计算**——保证「数字不会被模型编错」。纯函数、无 IO。

spec（来自 llm.prompts.build_table_query_prompt 的输出）：
  {
    "explanation": "一句话说明怎么算",
    "intent": "aggregate" | "list" | "count",
    "filters": [{"column": str, "op": str, "value": Any}],
    "group_by": [列名, ...],
    "metric": {"agg": str, "column": 列名 | None},
    "sort": "desc" | "asc",
    "limit": int
  }
op  ∈ eq, ne, gt, gte, lt, lte, contains, in, not_empty, empty
agg ∈ count, count_distinct, sum, avg, min, max
"""
from __future__ import annotations

import datetime as _dt
import re
import statistics
from collections import OrderedDict
from typing import Any

from .table_profile import _as_number, flatten_cell

_AGGS = {"count", "count_distinct", "sum", "avg", "min", "max"}
_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "not_empty", "empty"}
_AGG_CN = {
    "count": "记录数", "count_distinct": "去重计数",
    "sum": "求和", "avg": "平均", "min": "最小", "max": "最大",
}

_DATE_RE = re.compile(r"^\s*(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")


def _as_date(v: Any) -> _dt.date | None:
    if v is None:
        return None
    if isinstance(v, _dt.date):
        return v
    m = _DATE_RE.match(str(v).strip())
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _round(x: Any) -> Any:
    return round(x, 2) if isinstance(x, float) else x


def _resolve_col(name: Any, headers: list[str]) -> int | None:
    """列名 → 索引，宽松匹配（精确 → 去空格大小写不敏感）。"""
    if name is None:
        return None
    target = str(name).strip()
    if not target:
        return None
    for i, h in enumerate(headers):
        if str(h).strip() == target:
            return i
    low = target.lower()
    for i, h in enumerate(headers):
        if str(h).strip().lower() == low:
            return i
    return None


def _safe_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _match(cell: Any, op: str, value: Any) -> bool:
    fv = flatten_cell(cell)
    if op == "empty":
        return fv is None or str(fv).strip() == ""
    if op == "not_empty":
        return fv is not None and str(fv).strip() != ""
    if fv is None:
        return False
    if op == "contains":
        return str(value).strip().lower() in str(fv).lower()
    if op == "in":
        vals = value if isinstance(value, list) else [value]
        wanted = {str(x).strip().lower() for x in vals}
        return str(fv).strip().lower() in wanted
    if op in ("eq", "ne"):
        na, nb = _as_number(fv), _as_number(value)
        if na is not None and nb is not None:
            res = na == nb
        else:
            da, db = _as_date(fv), _as_date(value)
            if da and db:
                res = da == db
            else:
                res = str(fv).strip().lower() == str(value).strip().lower()
        return res if op == "eq" else not res

    # 有序比较 gt/gte/lt/lte：数值 → 日期 → 字符串
    a: Any
    b: Any
    na, nb = _as_number(fv), _as_number(value)
    if na is not None and nb is not None:
        a, b = na, nb
    else:
        da, db = _as_date(fv), _as_date(value)
        if da and db:
            a, b = da, db
        else:
            a, b = str(fv), str(value)
    try:
        if op == "gt":
            return a > b
        if op == "gte":
            return a >= b
        if op == "lt":
            return a < b
        if op == "lte":
            return a <= b
    except TypeError:
        return False
    return False


def _aggregate(group_rows: list[list], agg: str, col_idx: int | None) -> Any:
    if agg == "count":
        return len(group_rows)
    vals = [
        flatten_cell(r[col_idx]) if (col_idx is not None and col_idx < len(r)) else None
        for r in group_rows
    ]
    if agg == "count_distinct":
        return len({str(v) for v in vals if v is not None and str(v).strip() != ""})
    nums = [n for n in (_as_number(v) for v in vals) if n is not None]
    if not nums:
        return None
    if agg == "sum":
        return _round(sum(nums))
    if agg == "avg":
        return _round(statistics.fmean(nums))
    if agg == "min":
        return _round(min(nums))
    if agg == "max":
        return _round(max(nums))
    return None


def execute_query(headers: list[str], rows: list[list], spec: dict) -> dict:
    """在 (headers, rows) 上执行 query spec，返回标量或表格结果。"""
    notes: list[str] = []
    spec = spec or {}
    intent = str(spec.get("intent") or "aggregate").lower()

    # —— 解析度量 ——
    metric = spec.get("metric") or {}
    agg = str(metric.get("agg") or "count").lower()
    if agg not in _AGGS:
        agg = "count"
    metric_name = metric.get("column")
    metric_idx = _resolve_col(metric_name, headers) if metric_name else None
    if metric_name and metric_idx is None:
        notes.append(f"未找到度量列「{metric_name}」，改为按记录数统计")
        agg = "count"
        metric_name = None
    if agg != "count" and metric_idx is None:
        notes.append("缺少度量列，改为按记录数统计")
        agg = "count"

    # —— 应用筛选 ——
    filters = spec.get("filters") or []

    def _passes(row: list) -> bool:
        for f in filters:
            if not isinstance(f, dict):
                continue
            ci = _resolve_col(f.get("column"), headers)
            if ci is None:
                notes.append(f"忽略未知筛选列「{f.get('column')}」")
                continue
            op = str(f.get("op") or "eq").lower()
            if op not in _OPS:
                op = "eq"
            cell = row[ci] if ci < len(row) else None
            if not _match(cell, op, f.get("value")):
                return False
        return True

    filtered = [r for r in rows if _passes(r)]
    matched, total = len(filtered), len(rows)

    # —— intent=list：直接列出命中行 ——
    if intent == "list":
        limit = max(1, min(_safe_int(spec.get("limit"), 20), 50))
        out_rows = []
        for r in filtered[:limit]:
            out_rows.append([
                (str(flatten_cell(r[c])) if c < len(r) and flatten_cell(r[c]) is not None else "")[:80]
                for c in range(len(headers))
            ])
        return {
            "ok": True, "result_type": "table", "columns": list(headers),
            "rows": out_rows, "matched_rows": matched, "total_rows": total,
            "note": "；".join(dict.fromkeys(notes)),
        }

    agg_label = _AGG_CN["count"] if agg == "count" else f"{metric_name}（{_AGG_CN.get(agg, agg)}）"

    # —— 解析分组列 ——
    g_idx: list[tuple[str, int]] = []
    for g in (spec.get("group_by") or []):
        ci = _resolve_col(g, headers)
        if ci is None:
            notes.append(f"忽略未知分组列「{g}」")
        else:
            g_idx.append((str(g), ci))

    # —— 无分组 → 标量 ——
    if not g_idx:
        val = _aggregate(filtered, agg, metric_idx)
        return {
            "ok": True, "result_type": "scalar", "scalar": val,
            "scalar_label": agg_label, "matched_rows": matched, "total_rows": total,
            "note": "；".join(dict.fromkeys(notes)),
        }

    # —— 分组聚合 → 表格 ——
    groups: "OrderedDict[tuple, list]" = OrderedDict()
    for r in filtered:
        key = tuple(
            (str(flatten_cell(r[ci])) if ci < len(r) and flatten_cell(r[ci]) is not None else "（空）")
            for _, ci in g_idx
        )
        groups.setdefault(key, []).append(r)

    table = [list(key) + [_aggregate(grp, agg, metric_idx)] for key, grp in groups.items()]

    sort = str(spec.get("sort") or "desc").lower()
    if sort in ("desc", "asc"):
        table.sort(
            key=lambda row: (row[-1] is None, row[-1] if isinstance(row[-1], (int, float)) else 0),
            reverse=(sort == "desc"),
        )
    limit = max(1, min(_safe_int(spec.get("limit"), 20), 100))
    table = table[:limit]

    return {
        "ok": True, "result_type": "table",
        "columns": [g for g, _ in g_idx] + [agg_label],
        "rows": table, "matched_rows": matched, "total_rows": total,
        "note": "；".join(dict.fromkeys(notes)),
    }
