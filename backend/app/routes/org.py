from __future__ import annotations

import datetime as dt
import json
import time

from fastapi import APIRouter

from ..feishu import LarkCLI, MockLarkCLI, get_lark
from ..config import settings, DATA_ROOT
from ..services import org_graph

router = APIRouter(prefix="/api/org", tags=["org"])

# 部门树变化很慢，自动拉取一天一次即可（手动「重新拉取」随时强制重拉）。
_TREE_CACHE: dict = {"data": None, "at": 0.0}
_TREE_TTL = 86400

# 人数变化基准：每次真实拉取后落一份快照，下次拉取与之相比即得各部门 Δ。
_SNAPSHOT_PATH = DATA_ROOT / "org_snapshot.json"


def _load_snapshot() -> dict | None:
    try:
        return json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_snapshot(snap: dict, at: str) -> None:
    try:
        _SNAPSHOT_PATH.write_text(
            json.dumps({**snap, "at": at}, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


async def _resolve_lark():
    lark = await get_lark()
    if isinstance(lark, LarkCLI) and not await lark.ping():
        if settings.enable_mock_fallback:
            return MockLarkCLI()
        raise RuntimeError("lark-cli unavailable")
    return lark


@router.get("/graph")
async def org_graph_view(refresh: bool = False) -> dict:
    """真实组织架构图谱（应用身份枚举 contact/v3 部门树）。

    节点 = 部门（含虚拟根「全员」），尺寸按真实在册人数；边 = 上下级。
    结果缓存 30 分钟，refresh=True 强制重拉。
    """
    now = time.time()
    cached = _TREE_CACHE["data"]
    if not refresh and cached and now - _TREE_CACHE["at"] <= _TREE_TTL:
        return cached

    lark = await _resolve_lark()
    depts = await lark.contact_departments() if hasattr(lark, "contact_departments") else []
    graph = org_graph.build_department_tree(depts)
    now_iso = dt.datetime.now().isoformat(timespec="seconds")
    graph["last_refreshed"] = now_iso

    # 人数变化：与上一份快照对比，列出所有有增减的部门；再把本次落为新基准。
    # 仅真实身份维护基准——mock 兜底的数据不写快照，避免污染对比。
    if isinstance(lark, MockLarkCLI):
        graph["changes"] = {"prev_at": None, "items": [], "total_delta": 0, "total_prev": 0}
    else:
        graph["changes"] = org_graph.compute_changes(graph, _load_snapshot())
        _save_snapshot(org_graph.snapshot_from_graph(graph), now_iso)

    _TREE_CACHE["data"] = graph
    _TREE_CACHE["at"] = now
    return graph


@router.get("/members")
async def org_members(dept_id: str) -> dict:
    """某部门的直属成员（点击部门时按需加载）。"""
    lark = await _resolve_lark()
    members = (
        await lark.contact_department_members(dept_id)
        if hasattr(lark, "contact_department_members") else []
    )
    return {"dept_id": dept_id, "members": members, "count": len(members)}
