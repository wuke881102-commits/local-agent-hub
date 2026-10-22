"""模型产出的「事实」要能在原文里对上 —— 对不上的丢弃。

两条产出路都要用：HTML 页面（指标卡）和 PPT 讲稿（数据页、表格页）。
原先 filter_real_metrics 长在 agents/html_page.py 里，讲稿模式也要同一条规则；
抽出来而不是复制：防编造的规则一旦两边各改各的，就会在某一边悄悄变松。

这些都是对 prompt 指令的**确定性兜底** —— prompt 里写了「严禁编造数字」，
模型有时仍会补凑一个好看的数，只靠指令拦不住。
"""
from __future__ import annotations

import re


def filter_real_metrics(metrics: list, source_md: str, cap: int = 24) -> list:
    """只保留「数字确实出现在原文里」的指标，丢弃模型凭空编造的。

    判定：从 metric.value 抽取数字 token（如 70 / 18.4 / 3000），只有当其中
    某个**两位及以上**的数字串确实出现在原文中才保留。无数字的「伪指标」直接丢弃。
    只用两位以上：单个 1/2/3 在长文里到处都能撞上，拿来当证据等于没查。
    """
    if not isinstance(metrics, list) or not metrics:
        return []
    kept = []
    for m in metrics:
        if not isinstance(m, dict):
            continue
        val = str(m.get("value", "")).strip()
        nums = re.findall(r"\d+(?:\.\d+)?", val)
        sig = [n for n in nums if len(n.replace(".", "")) >= 2]
        if not sig:
            continue
        if any(n in source_md for n in sig):
            kept.append(m)
        if len(kept) >= cap:
            break
    return kept


def _norm(text: str) -> str:
    """比对用：去掉空白与常见标点。表格被模型转述时空格和全角标点常常不一样。"""
    return re.sub(r"[\s　,.，。：:；;、·\-—_/|()（）]+", "", text or "")


def table_is_real(columns: list[str], rows: list[list[str]], source_md: str,
                  threshold: float = 0.5) -> bool:
    """这张表是不是原文里的 —— 过半的非空单元格能在原文里找到才算。

    讲稿模式下模型会重组内容，可能自己「整理」出一张原文没有的表（季度、营收、
    占比……看起来很专业），这比编一个数字更难被发现。空单元格不计入分母。
    """
    cells = [c for c in list(columns) + [x for r in rows for x in r] if _norm(str(c))]
    if not cells:
        return False
    src = _norm(source_md)
    hit = sum(1 for c in cells if _norm(str(c)) in src)
    return hit / len(cells) > threshold
