"""把 AI 规划的「出图规格」变成可渲染的图。

延续「规则算准，LLM 增强」：模型只规划「图型 + 用哪些真实列 + 怎么聚合」，
这里用 ``table_query.execute_query`` 在**真实数据**上精确聚合，再拼成前端能直接渲染的
ECharts ``option`` 或 Mermaid 甘特源码。数值绝不经过模型。

build_chart(headers, rows, columns, spec) → 一个 chart 块，或 None（无法可靠出图就丢弃）：
  - engine=echarts → {engine,type,title,rationale,option}
  - engine=mermaid → {engine,type:'gantt',title,rationale,mermaid}
  - engine=image   → {engine,type,title,rationale,image_prompt}  （生图交给 agent 调 LLM 完成）
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from .table_profile import _as_number, flatten_cell
from .table_query import _resolve_col, execute_query

# 与 table_query._AGG_CN 同义；本地一份避免跨模块依赖私有名。
_AGG_CN = {"count": "记录数", "count_distinct": "去重计数",
           "sum": "求和", "avg": "平均", "min": "最小", "max": "最大"}

# 统一调色板（前端 ECharts / Mermaid 共用观感）
PALETTE = ["#4C6FFF", "#22C55E", "#F59E0B", "#EF4444", "#8B5CF6",
           "#06B6D4", "#EC4899", "#84CC16", "#F97316", "#14B8A6"]

_DATE_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")


# ── 公共入口 ─────────────────────────────────────────────────────────

def build_chart(headers: list[str], rows: list[list], columns: list[dict], spec: dict) -> dict | None:
    if not isinstance(spec, dict):
        return None
    engine = str(spec.get("engine") or "").lower().strip()
    ctype = str(spec.get("type") or "").lower().strip()
    title = (str(spec.get("title") or "").strip() or "图表")[:60]
    rationale = str(spec.get("rationale") or "").strip()[:200]

    # engine 缺失时按 type 推断
    if not engine:
        if ctype == "gantt":
            engine = "mermaid"
        elif ctype in ("architecture", "relation", "flow"):
            engine = "image"
        else:
            engine = "echarts"

    base = {"engine": engine, "type": ctype or "bar", "title": title, "rationale": rationale}

    # 架构 / 关系图：交给 agent 调生图模型（这里只透传 prompt）
    if engine == "image" or ctype in ("architecture", "relation", "flow"):
        prompt = str(spec.get("image_prompt") or "").strip()
        if not prompt:
            return None
        base["engine"] = "image"
        base["type"] = ctype or "architecture"
        base["image_prompt"] = prompt[:1500]
        return base

    # 甘特图：Mermaid
    if engine == "mermaid" or ctype == "gantt":
        mer = _build_gantt(headers, rows, spec)
        if not mer:
            return None
        base.update(engine="mermaid", type="gantt", mermaid=mer)
        return base

    # ECharts 数据图
    if ctype == "scatter":
        opt = _build_scatter(headers, rows, spec, title)
    else:
        opt = _build_data_option(headers, rows, spec, ctype or "bar", title)
    if not opt:
        return None
    base["engine"] = "echarts"
    base["option"] = opt
    return base


# ── ECharts：分组聚合类（bar/hbar/line/area/pie/donut/stacked_bar） ──────

def _series_label(col: Any, agg: str) -> str:
    if not col or agg == "count":
        return "记录数"
    return f"{col}（{_AGG_CN.get(agg, agg)}）"


def _run_series(headers, rows, dimension, agg, column, filters, sort, limit) -> list[tuple[str, Any]]:
    spec = {
        "intent": "aggregate",
        "group_by": [dimension],
        "metric": {"agg": agg or "count", "column": column},
        "filters": filters or [],
        "sort": sort or "desc",
        "limit": limit or 20,
    }
    res = execute_query(headers, rows, spec)
    if res.get("result_type") != "table":
        return []
    return [(str(r[0]), r[-1]) for r in res.get("rows", [])]


def _build_data_option(headers, rows, spec, ctype, title) -> dict | None:
    dimension = str(spec.get("dimension") or "").strip()
    if not dimension or _resolve_col(dimension, headers) is None:
        return None

    raw_series = spec.get("series")
    if not isinstance(raw_series, list) or not raw_series:
        raw_series = [{"agg": "count", "column": None}]

    filters = spec.get("filters") or []
    sort = str(spec.get("sort") or "desc").lower()
    limit = spec.get("limit") or 20

    categories: list[str] = []
    built: list[dict] = []
    for ss in raw_series[:6]:
        if not isinstance(ss, dict):
            continue
        agg = str(ss.get("agg") or "count").lower()
        col = ss.get("column")
        if col and _resolve_col(col, headers) is None:
            if agg != "count":
                continue          # 度量列不存在 → 丢这条系列
            col = None
        pairs = _run_series(headers, rows, dimension, agg, col, filters, sort, limit)
        if not pairs:
            continue
        if not categories:
            categories = [c for c, _ in pairs]
        built.append({"name": _series_label(col, agg), "map": {c: v for c, v in pairs}})

    if not built or not categories:
        return None

    # 饼 / 环：取第一条系列
    if ctype in ("pie", "donut"):
        s0 = built[0]
        data = [{"name": c, "value": s0["map"].get(c)} for c in categories if s0["map"].get(c) is not None]
        if not data:
            return None
        radius = ["45%", "70%"] if ctype == "donut" else "65%"
        return {
            "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14}},
            "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
            "legend": {"type": "scroll", "bottom": 0},
            "color": PALETTE,
            "series": [{
                "type": "pie", "radius": radius, "center": ["50%", "48%"],
                "data": data, "label": {"show": True, "formatter": "{b}: {d}%"},
            }],
        }

    # 柱 / 条 / 折线 / 面积 / 堆叠柱
    is_h = ctype == "hbar"
    is_line = ctype in ("line", "area")
    stacked = ctype == "stacked_bar"
    series_arr = []
    for s in built:
        vals = [s["map"].get(c) for c in categories]
        item: dict[str, Any] = {"name": s["name"], "data": vals}
        if is_line:
            item["type"] = "line"
            item["smooth"] = True
            if ctype == "area":
                item["areaStyle"] = {}
        else:
            item["type"] = "bar"
            if stacked:
                item["stack"] = "total"
        series_arr.append(item)

    cat_axis = {"type": "category", "data": categories}
    val_axis = {"type": "value"}
    if not is_h:
        cat_axis["axisLabel"] = {"interval": 0, "rotate": 30 if len(categories) > 6 else 0}
    x_axis, y_axis = (val_axis, cat_axis) if is_h else (cat_axis, val_axis)

    opt: dict[str, Any] = {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14}},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": "3%", "right": "4%", "bottom": "3%" if is_h else "12%", "top": 56, "containLabel": True},
        "color": PALETTE,
        "xAxis": x_axis, "yAxis": y_axis,
        "series": series_arr,
    }
    if len(series_arr) > 1:
        opt["legend"] = {"top": 28, "type": "scroll"}
    return opt


# ── ECharts：散点（XY，原始两数值列，不聚合） ──────────────────────────

def _build_scatter(headers, rows, spec, title) -> dict | None:
    xc = str(spec.get("x_column") or "").strip()
    yc = str(spec.get("y_column") or "").strip()
    xi, yi = _resolve_col(xc, headers), _resolve_col(yc, headers)
    if xi is None or yi is None:
        return None
    pts = []
    for r in rows:
        xv = _as_number(flatten_cell(r[xi])) if xi < len(r) else None
        yv = _as_number(flatten_cell(r[yi])) if yi < len(r) else None
        if xv is None or yv is None:
            continue
        pts.append([xv, yv])
    if len(pts) < 2:
        return None
    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14}},
        "tooltip": {"trigger": "item", "formatter": f"{xc}: {{c0}}"},
        "grid": {"left": "3%", "right": "5%", "bottom": "3%", "top": 56, "containLabel": True},
        "color": PALETTE,
        "xAxis": {"type": "value", "name": xc[:20], "nameLocation": "middle", "nameGap": 26},
        "yAxis": {"type": "value", "name": yc[:20]},
        "series": [{"type": "scatter", "symbolSize": 10, "data": pts}],
    }


# ── Mermaid 甘特 ─────────────────────────────────────────────────────

def _to_iso_date(v: Any) -> str | None:
    if v is None:
        return None
    m = _DATE_RE.search(str(v))
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def _san(s: Any) -> str:
    """Mermaid 里冒号 / 分号会破坏语法，替换成全角。"""
    return str(s).replace(":", "：").replace(";", "；").replace("\n", " ").strip()


def _build_gantt(headers, rows, spec, *, max_tasks: int = 40) -> str | None:
    ti = _resolve_col(spec.get("task_column"), headers)
    si = _resolve_col(spec.get("start_column"), headers)
    ei = _resolve_col(spec.get("end_column"), headers)
    sti = _resolve_col(spec.get("status_column"), headers)
    if ti is None or si is None or ei is None:
        return None

    lines = ["gantt", "    dateFormat YYYY-MM-DD", "    axisFormat %m/%d"]
    title = _san(spec.get("title") or "")
    if title:
        lines.append(f"    title {title}")
    lines.append("    section 任务")

    n = 0
    for r in rows:
        if n >= max_tasks:
            break
        task = flatten_cell(r[ti]) if ti < len(r) else None
        start = _to_iso_date(flatten_cell(r[si]) if si < len(r) else None)
        end = _to_iso_date(flatten_cell(r[ei]) if ei < len(r) else None)
        if not task or not start or not end:
            continue
        if end < start:
            start, end = end, start
        name = (_san(task)[:40]) or f"任务{n + 1}"
        tag = ""
        if sti is not None and sti < len(r):
            stv = str(flatten_cell(r[sti]) or "")
            if any(k in stv for k in ("完成", "已完成", "done", "Done", "结束", "关闭")):
                tag = "done, "
            elif any(k in stv for k in ("进行", "处理中", "active", "doing", "在办")):
                tag = "active, "
        lines.append(f"    {name} :{tag}{start}, {end}")
        n += 1

    if n == 0:
        return None
    return "\n".join(lines)
