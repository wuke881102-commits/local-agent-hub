"""多维表格 / 电子表格的「问数据」接口。

POST /api/base/ask：自然语言问题 → LLM 翻译成查询规格 → Python 在已加载的行上
精确执行聚合，返回标量或表格。数字全部由 Python 计算，模型只负责"翻译意图"。
"""
from __future__ import annotations

import datetime as dt
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import settings
from ..feishu import LarkCLI, MockLarkCLI, get_lark
from ..llm import get_llm
from ..llm.prompts import build_table_query_prompt
from ..services import index_service, table_profile, table_query, table_reader
from ..agents.base_analysis import _safe_parse_json

router = APIRouter(prefix="/api/base", tags=["base"])


class AskBody(BaseModel):
    asset_id: str
    asset_type: str | None = None
    target_id: str | None = None   # 多维表 table_id / 电子表 sheet_id（缺省取第一张）
    question: str


async def _resolve_lark():
    lark = await get_lark()
    if isinstance(lark, LarkCLI) and not await lark.ping():
        if settings.enable_mock_fallback:
            return MockLarkCLI()
        raise RuntimeError("lark-cli unavailable")
    return lark


def _kind_of(asset_type: str | None, title: str | None = None) -> str:
    """问数据路线：复用 table_reader.detect_kind（含云盘上传 Excel），无法判别按多维表兜底。"""
    return table_reader.detect_kind(asset_type, title) or "bitable"


def _schema_lines(columns: list[dict]) -> list[str]:
    """把列画像压成「列名 | 类型 | 样例/Top」给 LLM 做列名映射用。"""
    lines: list[str] = []
    for c in columns:
        bits = [str(c["name"]), c.get("inferred_type", "")]
        if c.get("numeric"):
            num = c["numeric"]
            bits.append(f"范围{num['min']}~{num['max']}")
        elif c.get("top_values"):
            bits.append("Top:" + "/".join(str(t["value"]) for t in c["top_values"][:4]))
        if c.get("pii"):
            bits.append(f"[{c['pii']}]")
        lines.append(" | ".join(b for b in bits if b))
    return lines


async def _asset_meta(asset_id: str) -> dict:
    for a in await index_service.list_assets(limit=2000):
        if a.get("asset_id") == asset_id:
            return a
    return {}


@router.post("/ask")
async def ask_table(body: AskBody) -> dict:
    question = (body.question or "").strip()
    if not question:
        return {"ok": False, "error": "问题为空。"}
    asset_id = (body.asset_id or "").strip()
    if not asset_id:
        return {"ok": False, "error": "缺少 asset_id。"}

    meta = await _asset_meta(asset_id)
    asset_type = body.asset_type or meta.get("type")
    asset_url = meta.get("url")
    asset_title = meta.get("title")
    kind = _kind_of(asset_type, asset_title)
    # 查询提示词只区分「多维表 / 表格」两套语义，xlsx 与 sheet 同属表格语义。
    prompt_kind = "bitable" if kind == "bitable" else "sheet"

    try:
        lark = await _resolve_lark()
        if kind == "bitable":
            data = await table_reader.load_table(lark, asset_id, "bitable", table_id=body.target_id, use_cache=True, url=asset_url)
        elif kind == "xlsx":
            data = await table_reader.load_table(lark, asset_id, "xlsx", sheet_id=body.target_id, use_cache=True, filename=asset_title)
        else:
            data = await table_reader.load_table(lark, asset_id, "sheet", sheet_id=body.target_id, use_cache=True, url=asset_url)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"读取表失败：{type(e).__name__}: {str(e)[:160]}"}

    headers, rows = data["headers"], data["rows"]
    if not headers:
        return {"ok": False, "error": "未读到任何列。"}
    if not rows:
        return {"ok": False, "error": "表里没有数据行。"}

    prof = table_profile.profile_table(headers, rows)
    system, user = build_table_query_prompt(
        question, _schema_lines(prof["columns"]), prompt_kind, dt.date.today().isoformat(),
    )

    llm = get_llm()
    try:
        raw = await llm.text_complete(user, system=system, json_mode=True, max_tokens=700, timeout=60, retries=1)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"模型调用失败：{type(e).__name__}"}

    spec = _safe_parse_json(raw)
    if not isinstance(spec, dict) or not spec:
        return {"ok": False, "error": "没能把问题解析成查询，换个问法试试？"}

    result = table_query.execute_query(headers, rows, spec)
    return {
        "ok": True,
        "question": question,
        "explanation": (spec.get("explanation") or "").strip(),
        "spec": spec,
        "result": result,
        "sampled": data["sampled"],
        "analyzed": data["analyzed"],
    }


_SAFE_IMG = re.compile(r"^[A-Za-z0-9_-]+\.png$")
_SAFE_TASK = re.compile(r"^[A-Za-z0-9_-]+$")


@router.get("/chart-image/{task_id}/{name}")
async def chart_image(task_id: str, name: str) -> FileResponse:
    """服务多维表格分析里 GPT-Image-1 生成的图（落在 drafts/_charts_<task_id>/<name>.png）。"""
    if not _SAFE_TASK.match(task_id) or not _SAFE_IMG.match(name):
        raise HTTPException(404, "bad path")
    p = settings.draft_path / f"_charts_{task_id}" / name
    if not p.exists():
        raise HTTPException(404, "image not found")
    return FileResponse(str(p), media_type="image/png")
