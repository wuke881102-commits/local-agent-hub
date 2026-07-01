"""表格画像服务 — 把 (表头, 行) 二维数据做确定性列画像与数据质量体检。

与 facets / governance 同样的「规则算准，LLM 增强」分工：这里只做 Python 能精确
计算的东西（类型推断、填充率、去重、数值统计、离群、异常分级）；语义解读与报表
建议交给 base_analysis Agent 里的 LLM 层。纯函数、无 IO，便于单测与复用。

输入约定：
- headers: list[str]               列名（按列序）
- rows:    list[list[cell]]        数据行；cell 可能是 str / int / float / bool /
                                   None / list[str]（多维表格的多选/人员/关联字段）。
  注意：调用方传入的 rows **不含表头行**（sheet 读取时需先把第 0 行切掉）。
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from typing import Any

# 识别"看起来像日期"的字符串：2024-03-15 / 2024/3/5 / 2024-03-15 12:30 / 2024年3月5日
_DATE_RE = re.compile(
    r"^\s*\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\s*$"
)
# 纯数值（含千分位、货币符号、百分号、负号）
_NUM_RE = re.compile(r"^[\s¥$€£]*-?[\d,]+(?:\.\d+)?\s*%?$")
_TRUE = {"true", "是", "yes", "y", "✓", "已完成", "done"}
_FALSE = {"false", "否", "no", "n", "✗", "未完成"}

# 敏感信息（PII）识别：顺序敏感——身份证(18)在银行卡(16-19)之前，邮箱无需去横杠。
_PII_PATTERNS: list[tuple[str, "re.Pattern[str]", bool]] = [
    ("邮箱", re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$"), False),
    ("身份证", re.compile(r"^\d{17}[\dXx]$"), True),
    ("手机号", re.compile(r"^1[3-9]\d{9}$"), True),
    ("银行卡", re.compile(r"^\d{16,19}$"), True),
]


def _detect_pii(str_vals: list[str]) -> str | None:
    """列里 ≥60% 的非空值匹配同一类敏感信息模式则判定为该 PII 类型。"""
    sample = [s for s in str_vals if s and s.strip()][:200]
    if len(sample) < 3:
        return None
    best, best_ratio = None, 0.0
    for label, pat, strip_sep in _PII_PATTERNS:
        hits = 0
        for v in sample:
            s = v.strip()
            cand = s.replace(" ", "").replace("-", "") if strip_sep else s
            if pat.match(s) or pat.match(cand):
                hits += 1
        ratio = hits / len(sample)
        if ratio > best_ratio:
            best, best_ratio = label, ratio
    return best if best_ratio >= 0.6 else None


def flatten_cell(v: Any) -> Any:
    """把富类型单元格降为标量：数值保留为数值（供统计），其余转字符串。

    - None → None（空）
    - bool → bool
    - int/float → 原样
    - list → 元素 flatten 后用「, 」连接（多维表格多选/人员/关联字段）
    - dict → 取 text/name/link 等可读字段，否则空
    - str → strip
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    if isinstance(v, list):
        parts = [flatten_cell(x) for x in v]
        parts = [str(p) for p in parts if p is not None and str(p).strip()]
        return ", ".join(parts) if parts else None
    if isinstance(v, dict):
        for k in ("text", "name", "value", "link", "en_name", "title"):
            val = v.get(k)
            if val:
                return str(val).strip()
        return None
    return str(v)


def _as_number(v: Any) -> float | None:
    """尝试把单元格解析成数值；失败返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s or not _NUM_RE.match(s):
            return None
        s = s.replace(",", "").replace("¥", "").replace("$", "").replace("€", "").replace("£", "").strip()
        pct = s.endswith("%")
        if pct:
            s = s[:-1]
        try:
            n = float(s)
            return n / 100 if pct else n
        except ValueError:
            return None
    return None


def _classify(v: Any) -> str:
    """单个非空单元格归类：number / date / bool / text。"""
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    s = str(v).strip()
    low = s.lower()
    if low in _TRUE or low in _FALSE:
        return "bool"
    if _NUM_RE.match(s) and _as_number(s) is not None:
        return "number"
    if _DATE_RE.match(s):
        return "date"
    return "text"


def _round(x: float) -> float:
    return round(x, 2)


def profile_column(name: str, index: int, raw_values: list[Any]) -> dict:
    """对单列做画像。raw_values 为该列所有行的原始单元格（未 flatten）。"""
    flat = [flatten_cell(v) for v in raw_values]
    non_empty = [v for v in flat if v is not None and str(v).strip() != ""]
    total = len(flat)
    n = len(non_empty)
    fill = _round(n / total * 100) if total else 0.0

    # 类型分布
    kinds = Counter(_classify(v) for v in non_empty)
    inferred = "empty"
    if n:
        top_kind, top_n = kinds.most_common(1)[0]
        if top_n / n >= 0.8:
            inferred = top_kind
        else:
            inferred = "mixed"

    # 去重 / Top 值（按字符串值）
    str_vals = [str(v) for v in non_empty]
    distinct = len(set(str_vals))
    counter = Counter(str_vals)
    top_values = [{"value": (val[:40]), "count": c} for val, c in counter.most_common(5)]

    col: dict[str, Any] = {
        "name": name,
        "index": index,
        "inferred_type": inferred,
        "fill_rate": fill,
        "non_empty": n,
        "distinct": distinct,
        "top_values": top_values if (inferred in ("text", "bool", "date", "mixed") and distinct <= max(20, n)) else [],
    }

    # 数值统计 + 离群（IQR）
    if inferred in ("number", "mixed"):
        nums = [x for x in (_as_number(v) for v in non_empty) if x is not None]
        if len(nums) >= 1:
            stats: dict[str, Any] = {
                "count": len(nums),
                "min": _round(min(nums)),
                "max": _round(max(nums)),
                "mean": _round(statistics.fmean(nums)),
                "median": _round(statistics.median(nums)),
            }
            outliers = _iqr_outliers(nums)
            stats["outliers"] = outliers
            col["numeric"] = stats

    # 文本长度
    if inferred in ("text", "mixed"):
        lens = [len(str(v)) for v in non_empty]
        if lens:
            col["text_len"] = {"min": min(lens), "max": max(lens), "avg": _round(statistics.fmean(lens))}

    # 敏感信息（规则识别，不依赖 LLM）
    pii = _detect_pii(str_vals)
    if pii:
        col["pii"] = pii

    return col


def _iqr_outliers(nums: list[float]) -> int:
    if len(nums) < 8:
        return 0  # 样本太小，IQR 不稳，不报离群
    qs = statistics.quantiles(nums, n=4)  # [Q1, Q2, Q3]
    q1, q3 = qs[0], qs[2]
    iqr = q3 - q1
    if iqr == 0:
        return 0
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return sum(1 for x in nums if x < lo or x > hi)


def profile_table(headers: list[str], rows: list[list[Any]]) -> dict:
    """整表画像：列画像 + 表级指标。rows 不含表头。"""
    ncols = len(headers)
    # 规整：每行补齐/截断到 ncols
    norm_rows = [(r + [None] * ncols)[:ncols] for r in rows]
    columns = [
        profile_column(headers[c] or f"列{c + 1}", c, [row[c] for row in norm_rows])
        for c in range(ncols)
    ]

    # 表级
    nrows = len(norm_rows)
    empty_columns = sum(1 for col in columns if col["non_empty"] == 0)
    total_cells = nrows * ncols
    filled_cells = sum(col["non_empty"] for col in columns)
    overall_fill = _round(filled_cells / total_cells * 100) if total_cells else 0.0

    # 重复行（基于 flatten 后的字符串签名）
    seen: set[tuple] = set()
    dup = 0
    for row in norm_rows:
        sig = tuple(str(flatten_cell(c)) for c in row)
        if sig in seen:
            dup += 1
        else:
            seen.add(sig)

    metrics = {
        "row_count": nrows,
        "column_count": ncols,
        "empty_columns": empty_columns,
        "duplicate_rows": dup,
        "overall_fill": overall_fill,
        "pii_columns": sum(1 for col in columns if col.get("pii")),
    }
    return {"columns": columns, "metrics": metrics}


# ── 异常体检（规则分级） ─────────────────────────────────────────────

def detect_anomalies(columns: list[dict], metrics: dict) -> list[dict]:
    """从列画像 + 表级指标派生数据质量异常，按 high/medium/low 分级。"""
    out: list[dict] = []
    nrows = metrics.get("row_count", 0) or 0

    for col in columns:
        name = col["name"]
        fill = col["fill_rate"]
        n = col["non_empty"]
        if n == 0:
            out.append({"level": "medium", "type": "empty_column",
                        "title": f"整列为空：{name}", "columns": [name],
                        "detail": "该列没有任何数据，可考虑删除或补录。"})
            continue
        if fill < 5:
            out.append({"level": "medium", "type": "near_empty",
                        "title": f"填充率极低：{name}（{fill}%）", "columns": [name],
                        "detail": "几乎整列空白，数据可用性差。"})
        elif fill < 50:
            out.append({"level": "low", "type": "low_fill",
                        "title": f"填充率偏低：{name}（{fill}%）", "columns": [name],
                        "detail": "过半行缺失该字段，统计时需注意口径。"})
        if col.get("distinct") == 1 and n > 1:
            out.append({"level": "low", "type": "constant",
                        "title": f"整列只有一个值：{name}", "columns": [name],
                        "detail": "字段无区分度，可能是默认值或冗余列。"})
        if col["inferred_type"] == "mixed":
            out.append({"level": "medium", "type": "mixed_type",
                        "title": f"类型混排：{name}", "columns": [name],
                        "detail": "数值与文本混在一列，建议统一格式以便排序与计算。"})
        outliers = (col.get("numeric") or {}).get("outliers", 0)
        if outliers:
            out.append({"level": "low", "type": "outlier",
                        "title": f"疑似离群值：{name}（{outliers} 个）", "columns": [name],
                        "detail": "存在远离主体分布的数值，建议核对是否录入错误。"})

    if metrics.get("duplicate_rows"):
        dup = metrics["duplicate_rows"]
        lvl = "high" if nrows and dup / nrows > 0.1 else "medium"
        out.append({"level": lvl, "type": "duplicate_rows",
                    "title": f"重复行：{dup} 行完全相同", "columns": [],
                    "detail": "存在内容完全一致的行，建议去重。"})

    # 排序：high → medium → low
    rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda a: rank.get(a["level"], 3))
    return out


# ── 给 LLM 的紧凑画像（控制 token） ──────────────────────────────────

def compact_for_llm(meta: dict, columns: list[dict], metrics: dict,
                    anomalies: list[dict], sample_rows: list[list[Any]]) -> dict:
    """压缩成小体积上下文喂给模型：列摘要 + 表级 + 异常标题 + 少量样例行。"""
    col_lines = []
    for col in columns[:60]:
        item: dict[str, Any] = {
            "name": col["name"][:30],
            "type": col["inferred_type"],
            "fill": col["fill_rate"],
            "distinct": col["distinct"],
        }
        if col.get("numeric"):
            num = col["numeric"]
            item["range"] = [num["min"], num["max"]]
        if col.get("top_values"):
            item["top"] = [t["value"][:20] for t in col["top_values"][:3]]
        if col.get("pii"):
            item["pii"] = col["pii"]
        col_lines.append(item)

    sample = []
    headers = [c["name"] for c in columns]
    for row in sample_rows[:3]:
        cells = [flatten_cell(c) for c in row][:len(headers)]
        sample.append({headers[i][:20]: (str(c)[:40] if c is not None else "")
                       for i, c in enumerate(cells)})

    return {
        "title": meta.get("name") or meta.get("title"),
        "kind": meta.get("kind"),
        "row_count": metrics.get("row_count"),
        "column_count": metrics.get("column_count"),
        "overall_fill": metrics.get("overall_fill"),
        "columns": col_lines,
        "anomalies": [{"level": a["level"], "title": a["title"]} for a in anomalies[:12]],
        "sample_rows": sample,
    }
