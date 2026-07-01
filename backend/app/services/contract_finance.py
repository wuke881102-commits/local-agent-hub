"""合同金额测算 — 确定性聚合引擎。

分工严格遵循全站「规则算准 + LLM 增强」：
  - LLM 只负责**把每一笔款项读成结构化字段**（金额数值、币种、一次性/周期性、
    起止日期、年递增率等）——这是"翻译意图"，模型擅长且不涉及加总。
  - 本模块用 Python 把这些条目**精确展开到每个自然年并求和**：周期性款项按月/季/年
    迭代、可选按年递增（复利），一次性款项计入对应年份；分币种汇总，绝不混算汇率。

因此"每年所需金额总数 / 合计"全部由 Python 算出，模型不做任何算术 → 数字不会被编造。
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict

MAX_ITEMS = 200
_GUARD = 1200  # 周期展开的最大迭代步数（防脏数据死循环：100 年 × 月付）

# 频率 → 步进月数
_FREQ_STEP = {
    "monthly": 1, "month": 1, "月": 1, "月付": 1, "每月": 1,
    "quarterly": 3, "quarter": 3, "季": 3, "季度": 3, "每季": 3,
    "yearly": 12, "annual": 12, "annually": 12, "年": 12, "年付": 12, "每年": 12,
    "semiannual": 6, "半年": 6,
}
_ONCE = {"once", "one_time", "onetime", "一次性", "单次", ""}

_CUR_MAP = [
    (("cny", "rmb", "人民币", "元", "￥", "¥"), "CNY"),
    (("usd", "美元", "us$", "$"), "USD"),
    (("eur", "欧元", "€"), "EUR"),
    (("hkd", "港币", "港元", "hk$"), "HKD"),
    (("jpy", "日元", "円"), "JPY"),
    (("gbp", "英镑", "£"), "GBP"),
    (("sgd", "新加坡元", "新元"), "SGD"),
]


def compute(money_items, conditional_items=None) -> dict:
    """把 LLM 抽取的款项列表测算成分币种、分年度的金额合计。

    money_items[i] = {label, amount, currency, role(line_item|total|subtotal),
                      type(one_time|recurring), frequency, start, end, year,
                      escalation_pct, note, quote}

    去重要点：订单/报价单常同时给出**明细行**和一条**合计行**。若对所有款项无差别
    求和，会把合计行再加一遍（≈2 倍）。因此分两遍：先把每笔展开到「分年金额」并判定
    角色（明细 / 小计 / 合计），再**按币种**决定计入哪些——某币种若存在「合计」行，就用
    合计作为该币种金额、明细仅作参考不计入；否则用明细求和（小计是部分汇总，一律不计入）。
    """
    warnings: list[str] = []
    assumptions: list[str] = []
    saw_default_cur = False

    # ── 第一遍：展开 + 分类 ──
    expanded: list[dict] = []
    for raw in (money_items or [])[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        label = (str(raw.get("label") or "未命名款项")).strip()
        amt = _to_float(raw.get("amount"))
        if amt is None:
            warnings.append(f"「{label}」没有可解析的金额，已跳过测算")
            continue

        cur_raw = raw.get("currency")
        if not str(cur_raw or "").strip():
            saw_default_cur = True
        cur = _normalize_currency(cur_raw)

        typ = str(raw.get("type") or "").strip().lower()
        freq = str(raw.get("frequency") or "").strip().lower()
        esc = _to_float(raw.get("escalation_pct")) or 0.0
        note = str(raw.get("note") or "").strip()
        quote = str(raw.get("quote") or "").strip()

        is_recurring = (typ == "recurring") or (freq in _FREQ_STEP and freq not in _ONCE)
        by_year: dict = defaultdict(float)

        if not is_recurring:
            y = _year_from(raw)
            key = y if y is not None else "未标注年份"
            by_year[key] += amt
            if y is None:
                warnings.append(f"「{label}」为一次性款项但缺日期，未能归入具体年份")
        else:
            start = _parse_date(raw.get("start") or raw.get("date"))
            end = _parse_date(raw.get("end"))
            step = _FREQ_STEP.get(freq, 1)
            if not start or not end:
                y = _year_from(raw)
                key = y if y is not None else "未标注年份"
                by_year[key] += amt
                warnings.append(f"「{label}」为周期性款项但缺完整起止日期，按单期金额计入 {key}")
            else:
                if end < start:
                    start, end = end, start
                cur_d = dt.date(start.year, start.month, 1)
                end_m = dt.date(end.year, end.month, 1)
                guard = 0
                while cur_d <= end_m and guard < _GUARD:
                    y = cur_d.year
                    factor = (1.0 + esc / 100.0) ** (y - start.year) if esc else 1.0
                    by_year[y] += amt * factor
                    cur_d = _add_months(cur_d, step)
                    guard += 1
                if esc:
                    assumptions.append(f"「{label}」按每年递增 {esc}% 复利测算（自 {start.year} 年起）")

        expanded.append({
            "label": label, "cur": cur, "amount": amt, "is_recurring": is_recurring,
            "freq": freq, "esc": esc, "note": note, "quote": quote, "raw": raw,
            "by_year": dict(by_year), "role": _classify_item(raw, label),
        })

    # ── 第二遍：按币种决定计入项，避免重复累加 ──
    # 规则（按币种）：
    #   · 有「合计」行 → 只计**其中一条**权威合计；明细 / 小计 / 其余合计仅作参考。
    #     多条合计（如「报价总价」「订单总额」）是同一总额的不同写法，**绝不相加**——
    #     按 label 权威度打分挑一条（订单/应付/final 优先于 报价/list/折前），同分取较小者（折后）。
    #   · 无合计行 → 计明细求和（小计是部分汇总，一律不计）。
    cur_totals: dict[str, list[int]] = defaultdict(list)  # cur -> 该币种 total 项在 expanded 中的下标
    for i, e in enumerate(expanded):
        if e["role"] == "total":
            cur_totals[e["cur"]].append(i)
    chosen_total: dict[str, int] = {}
    for cur, idxs in cur_totals.items():
        chosen_total[cur] = min(
            idxs,
            key=lambda i: (-_total_score(expanded[i]["label"]), sum(expanded[i]["by_year"].values())),
        )
    if any(len(v) > 1 for v in cur_totals.values()):
        assumptions.append(
            "同一币种检测到多条「合计」行（如报价总价 / 订单总额），已取其一作为合计、未相加"
        )

    by_cur_year: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    items_out: list[dict] = []
    for i, e in enumerate(expanded):
        if e["cur"] in chosen_total:
            counted = (i == chosen_total[e["cur"]])
        else:
            counted = e["role"] == "line_item"  # 无合计时只数明细，小计是部分汇总不计
        if counted:
            for y, v in e["by_year"].items():
                by_cur_year[e["cur"]][y] += v
        items_out.append({
            "label": e["label"],
            "currency": e["cur"],
            "amount": round(e["amount"], 2),
            "role": e["role"],
            "counted": counted,
            "type": "recurring" if e["is_recurring"] else "one_time",
            "frequency": e["freq"] or ("once" if not e["is_recurring"] else ""),
            "start": e["raw"].get("start") or e["raw"].get("date") or "",
            "end": e["raw"].get("end") or "",
            "escalation_pct": e["esc"] or None,
            "note": e["note"],
            "quote": e["quote"],
            "total": round(sum(e["by_year"].values()), 2),
            "by_year": [{"year": k, "amount": round(v, 2)} for k, v in _sorted_years(e["by_year"])],
        })

    if chosen_total:
        assumptions.append(
            "检测到「合计 / 总计」行：已用它作为该币种合计，未与明细行重复累加（明细仅作参考列出）"
        )

    by_currency = []
    for cur, ymap in by_cur_year.items():
        years = [{"year": k, "total": round(v, 2)} for k, v in _sorted_years(ymap)]
        by_currency.append({
            "currency": cur,
            "years": years,
            "total": round(sum(ymap.values()), 2),
        })
    by_currency.sort(key=lambda c: -c["total"])

    if saw_default_cur:
        assumptions.append("部分款项未标注币种，已按人民币(CNY)计入")
    mixed = len(by_currency) > 1
    if mixed:
        warnings.append("检测到多种币种，已分币种分别汇总，未做汇率换算")

    return {
        "by_currency": by_currency,
        "mixed_currency": mixed,
        "items": items_out,
        "conditional_items": _clean_conditional(conditional_items),
        "warnings": warnings,
        "assumptions": assumptions,
        "item_count": len(items_out),
    }


# 汇总行识别：模型给的 role 优先；role 缺失/不可信时用 label 关键词兜底纠正。
_TOTAL_LABEL_RE = re.compile(
    r"合计|總計|总计|总额|總額|总费用|总金额|总价款|order\s*total|grand\s*total|total\s*fees|total\s*amount",
    re.I,
)
_SUBTOTAL_LABEL_RE = re.compile(r"小计|小計|sub-?total", re.I)

# 多条「合计」择一时的权威度：订单/应付/最终 > 报价/标价/折前。分高者胜，同分取较小金额（折后）。
_TOTAL_AUTH_POS = re.compile(r"order|订单|訂單|final|最终|最終|应付|應付|实付|實付|net|grand|应收|實收", re.I)
_TOTAL_AUTH_NEG = re.compile(r"quote|报价|報價|list|标价|標價|before\s*discount|折前|原价|原價|list\s*price", re.I)


def _total_score(label: str) -> int:
    s = 0
    if _TOTAL_AUTH_POS.search(label):
        s += 2
    if _TOTAL_AUTH_NEG.search(label):
        s -= 2
    return s


def _classify_item(raw: dict, label: str) -> str:
    """判定款项角色：'total'（合计行）/ 'subtotal'（小计）/ 'line_item'（明细）。

    以模型显式 role 为主，label 关键词为兜底（含 role 误标成明细但 label 明显是合计的纠正）。
    """
    role = str(raw.get("role") or "").strip().lower()
    if role in ("total", "grand_total", "grandtotal", "order_total", "sum"):
        return "total"
    if role in ("subtotal", "sub_total"):
        return "subtotal"
    if _TOTAL_LABEL_RE.search(label):
        return "total"
    if role in ("line_item", "line", "item"):
        return "line_item"
    if _SUBTOTAL_LABEL_RE.search(label):
        return "subtotal"
    return "line_item"


# ── 解析工具 ──────────────────────────────────────────────────────

def _to_float(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if v is None:
        return None
    s = re.sub(r"[,，\s]", "", str(v))
    s = s.replace("￥", "").replace("¥", "").replace("$", "").replace("元", "")
    mult = 1.0
    if s.endswith("万"):
        mult, s = 1e4, s[:-1]
    elif s.endswith("亿"):
        mult, s = 1e8, s[:-1]
    elif s.endswith("万元"):
        mult, s = 1e4, s[:-2]
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) * mult if m else None


def _normalize_currency(c) -> str:
    s = str(c or "").strip().lower()
    if not s:
        return "CNY"
    for keys, code in _CUR_MAP:
        if any(k in s for k in keys):
            return code
    return s.upper()


def _parse_date(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    s2 = re.sub(r"[年月]", "-", s).replace("日", "").replace("/", "-").replace(".", "-").strip("- ")
    m = re.match(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", s2)
    if not m:
        ym = re.search(r"(\d{4})", s2)
        return dt.date(int(ym.group(1)), 1, 1) if ym else None
    y = int(m.group(1))
    mo = min(max(int(m.group(2) or 1), 1), 12)
    d = min(max(int(m.group(3) or 1), 1), 28)
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return dt.date(y, 1, 1)


def _parse_year(v):
    if v is None:
        return None
    if isinstance(v, int):
        return v if 1900 <= v <= 2200 else None
    m = re.search(r"(19|20|21)\d{2}", str(v))
    return int(m.group(0)) if m else None


def _year_from(raw: dict):
    y = _parse_year(raw.get("year"))
    if y is not None:
        return y
    d = _parse_date(raw.get("start") or raw.get("date") or raw.get("end"))
    return d.year if d else None


def _add_months(d: dt.date, n: int) -> dt.date:
    total = d.month - 1 + n
    y = d.year + total // 12
    mo = total % 12 + 1
    return dt.date(y, mo, 1)


def _sorted_years(ymap: dict):
    # 数值年份在前按大小排，"未标注年份"等字符串键排最后
    return sorted(
        ymap.items(),
        key=lambda kv: (0, kv[0]) if isinstance(kv[0], int) else (1, str(kv[0])),
    )


def _clean_conditional(items) -> list:
    out = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        label = str(it.get("label") or "").strip()
        if not label:
            continue
        out.append({
            "label": label,
            "basis": str(it.get("basis") or "").strip(),
            "note": str(it.get("note") or "").strip(),
            "quote": str(it.get("quote") or "").strip(),
        })
    return out
