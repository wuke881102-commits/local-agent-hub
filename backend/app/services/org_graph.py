"""组织架构关系图谱构建（仿 Obsidian 图谱）。

数据来源：本地索引里的文档负责人（owner_id 为 ou_* 的真实用户）。每个人经
``contact +search-user`` 解析出「部门路径」（如 ``集团总部-数码科技-信息安全``），
拆成层级，构成 *部门树 + 人员* 的图；再用「共享空间」推导人—人协作边，得到
Obsidian 式的网状关系图。

边界：当前授权没有 ``contact:department`` scope，无法自上而下枚举部门，所以这是
"能看到的人"反推出的**局部组织视图**，覆盖度 = 索引里出现过的负责人集合。
"""
from __future__ import annotations

import html
import re
from collections import defaultdict

_SEP_RE = re.compile(r"\s*-\s*")


def split_department(path: str) -> list[str]:
    """部门路径 → 层级段列表。

    飞书 search-user 返回的 ``department`` 是用 '-' 连接的人类可读路径，单个部门名
    内部用 '&' 分隔（如 '大数据&AI'）而非 '-'，故按 '-' 拆层级是安全的启发式。
    顺带反转义 HTML 实体（``&amp;`` → ``&``；JSON 里可能是 ``\\u0026amp;``）。
    """
    if not path:
        return []
    s = html.unescape(html.unescape(path)).strip()  # 双重：&amp; 可能被再转义一层
    return [p.strip() for p in _SEP_RE.split(s) if p.strip()]


_UNKNOWN = "(未知部门)"


def build_graph(
    people: list[dict],
    *,
    collab_edges: list[dict] | None = None,
) -> dict:
    """构造 {nodes, edges, branches, stats}。

    people: [{open_id, name, email, department, docs}]
    collab_edges: [{a, b, weight}]（open_id 对，已去重去自环）
    """
    nodes: dict[str, dict] = {}
    hierarchy: set[tuple[str, str]] = set()
    member_edges: list[dict] = []

    def dept_id(segs: list[str]) -> str:
        return "dept::" + "/".join(segs)

    def ensure_dept(segs: list[str]) -> str:
        did = dept_id(segs)
        if did not in nodes:
            nodes[did] = {
                "id": did,
                "label": segs[-1],
                "type": "dept",
                "depth": len(segs),
                "branch": segs[0],
                "path": "/".join(segs),
                "members": 0,
            }
        return did

    # 1) 部门层级 + 父子边；members 计数沿链累加（= 该子树下的人数，用于节点尺寸）
    for p in people:
        segs = split_department(p.get("department") or "")
        if not segs:
            segs = [_UNKNOWN]
            p["_segs"] = segs
        else:
            p["_segs"] = segs
        for depth in range(1, len(segs) + 1):
            did = ensure_dept(segs[:depth])
            nodes[did]["members"] += 1
            if depth >= 2:
                hierarchy.add((dept_id(segs[: depth - 1]), did))

    # 2) 人员节点 + 归属边（挂到所在叶子部门）
    for p in people:
        oid = p["open_id"]
        segs = p["_segs"]
        pid = "person::" + oid
        branch = segs[0]
        nodes[pid] = {
            "id": pid,
            "label": p.get("name") or oid,
            "type": "person",
            "branch": branch,
            "department": "/".join(segs) if segs != [_UNKNOWN] else "",
            "email": p.get("email") or "",
            "docs": int(p.get("docs") or 0),
            "open_id": oid,
        }
        member_edges.append({"source": dept_id(segs), "target": pid, "kind": "member"})

    edges: list[dict] = [
        {"source": a, "target": b, "kind": "hierarchy"} for (a, b) in sorted(hierarchy)
    ]
    edges.extend(member_edges)

    # 3) 协作边（人—人，共享空间），过滤掉两端不在图里的
    n_collab = 0
    for ce in collab_edges or []:
        pa, pb = "person::" + ce["a"], "person::" + ce["b"]
        if pa in nodes and pb in nodes:
            edges.append({"source": pa, "target": pb, "kind": "collab", "weight": ce.get("weight", 1)})
            n_collab += 1

    # 4) 分支（顶级部门）顺序：按人数倒序，给前端配色用
    branch_count: dict[str, int] = defaultdict(int)
    for n in nodes.values():
        if n["type"] == "person":
            branch_count[n["branch"]] += 1
    branches = [b for b, _ in sorted(branch_count.items(), key=lambda kv: -kv[1])]

    n_people = sum(1 for n in nodes.values() if n["type"] == "person")
    n_dept = sum(1 for n in nodes.values() if n["type"] == "dept")
    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "branches": branches,
        "stats": {
            "people": n_people,
            "departments": n_dept,
            "branches": len(branches),
            "collab_edges": n_collab,
            "hierarchy_edges": len(hierarchy),
            "member_edges": len(member_edges),
        },
    }


def build_department_tree(departments: list[dict]) -> dict:
    """真实组织架构树（来自 contact/v3 部门接口，应用身份）。

    departments: ``[{id, name, member_count, parent}]``（id/parent 为 open_department_id，
    顶级 parent 为 "0"）。产出 ``{nodes, edges, branches, stats}``：
    - 增加一个虚拟根「全员」串起 16 个一级部门；
    - 每个部门标注 depth（距根层级）、branch（所属一级部门名）、path（名称全路径）、
      members（**真实在册人数**，含下级）。
    """
    by_id = {d["id"]: d for d in departments if d.get("id")}
    ROOT = "root"

    def _walk_up(d: dict):
        """返回 (top_level_dept, depth, [name,...] 从顶到此)。"""
        chain = [d]
        seen = {d["id"]}
        cur = d
        while True:
            p = cur.get("parent") or "0"
            if p == "0" or p not in by_id or p in seen:
                break
            seen.add(p)
            cur = by_id[p]
            chain.append(cur)
        names = [c["name"] for c in reversed(chain)]
        return chain[-1], len(chain), names

    total = sum(d.get("member_count", 0) for d in departments if (d.get("parent") or "0") == "0")
    nodes: dict[str, dict] = {
        ROOT: {"id": ROOT, "label": "全员", "type": "dept", "depth": 0,
               "branch": "全员", "path": "全员", "members": total, "dept_id": ""},
    }
    edges: list[dict] = []
    for d in departments:
        top, depth, names = _walk_up(d)
        nodes[d["id"]] = {
            "id": d["id"], "label": d["name"], "type": "dept",
            "depth": depth, "branch": top["name"], "path": "/".join(names),
            "members": d.get("member_count", 0), "dept_id": d["id"],
        }
    for d in departments:
        p = d.get("parent") or "0"
        src = p if (p != "0" and p in by_id) else ROOT
        edges.append({"source": src, "target": d["id"], "kind": "hierarchy"})

    top_levels = [d for d in departments if (d.get("parent") or "0") == "0"]
    branches = [d["name"] for d in sorted(top_levels, key=lambda x: -x.get("member_count", 0))]
    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "branches": branches,
        "stats": {"departments": len(departments), "members": total, "branches": len(branches)},
    }


def _parent_map(graph: dict) -> dict[str, str]:
    """从 hierarchy 边抽 dept_id -> 父 id（父可能是虚拟根 "root"）。"""
    parent: dict[str, str] = {}
    for e in graph.get("edges", []):
        if e.get("kind") != "hierarchy":
            continue
        src, tgt = e.get("source"), e.get("target")
        if tgt:
            parent[tgt] = src or ""
    return parent


def snapshot_from_graph(graph: dict) -> dict:
    """从 build_department_tree 的产出抽出可持久化快照（按 dept_id 记人数/标签/父）。

    存 parent 是为了下次能还原"上一版树形"，从而把累计变化拆到本级（见 compute_changes）。
    虚拟根「全员」（dept_id 为空）不入快照，其总人数另存 total。
    """
    parent = _parent_map(graph)
    depts: dict[str, dict] = {}
    for n in graph.get("nodes", []):
        did = n.get("dept_id")
        if not did:
            continue
        depts[did] = {
            "members": int(n.get("members") or 0),
            "label": n.get("label") or "",
            "path": n.get("path") or "",
            "branch": n.get("branch") or "",
            "parent": parent.get(did, ""),
            "depth": int(n.get("depth") or 0),
        }
    return {"depts": depts, "total": int((graph.get("stats") or {}).get("members") or 0)}


def compute_changes(graph: dict, prev: dict | None) -> dict:
    """对比当前 graph 与上一份快照 prev，逐部门给出人数变化（含下级 + 本级）。

    飞书 member_count 含下级，所以叶子的一次增减会沿祖先链层层累加。为了"显示到
    最后的层级"，这里给每个部门同时算两种 delta：
    - ``delta`` 累计变化（含下级），与详情面板「在册 N 人（含下级）」口径一致；
    - ``own``  本级变化 = 自己的累计变化 − 直接下级们的累计变化之和；变动真正发生
      在哪一层，own 就只在那一层非零（叶子的 own == delta）。前端卡片按 own 过滤，
      天然把每笔变动钉在最深的那个部门，不再把同一笔变动在各级祖先重复列出。

    人员可挂多个部门（导致某些父 < 子合计）是静态结构，差分时基本抵消，故 own 在
    delta 上是稳健的。Σ own == 顶级合计变化 == total_delta。

    返回 ``{prev_at, items, total_delta, total_prev}``；items 每条
    ``{dept_id,label,path,branch,members,prev,delta,own,has_children,kind}``，
    kind ∈ {changed, added, removed}。无 prev（首次拉取）→ items 为空。
    """
    if not prev:
        return {"prev_at": None, "items": [], "total_delta": 0, "total_prev": 0}

    prev_depts: dict = prev.get("depts") or {}
    cur: dict[str, dict] = {n["dept_id"]: n for n in graph.get("nodes", []) if n.get("dept_id")}

    # 当前/历史两套「父 -> 直接下级」结构（用于本级 = 自己 − 直接下级合计）
    now_parent = _parent_map(graph)
    now_children: dict[str, list[str]] = defaultdict(list)
    for did, par in now_parent.items():
        now_children[par].append(did)
    prev_children: dict[str, list[str]] = defaultdict(list)
    for did, rec in prev_depts.items():
        prev_children[rec.get("parent") or ""].append(did)

    def cum_now(d: str) -> int:
        n = cur.get(d)
        return int(n.get("members") or 0) if n else 0

    def cum_prev(d: str) -> int:
        p = prev_depts.get(d)
        return int(p.get("members") or 0) if p else 0

    items: list[dict] = []
    for did in set(cur) | set(prev_depts):
        c_now, c_prev = cum_now(did), cum_prev(did)
        delta = c_now - c_prev
        direct_now = c_now - sum(cum_now(c) for c in now_children.get(did, ()))
        direct_prev = c_prev - sum(cum_prev(c) for c in prev_children.get(did, ()))
        own = direct_now - direct_prev
        if delta == 0 and own == 0:
            continue
        meta = cur.get(did) or prev_depts.get(did) or {}
        kind = "added" if did not in prev_depts else ("removed" if did not in cur else "changed")
        items.append({
            "dept_id": did, "label": meta.get("label"), "path": meta.get("path"),
            "branch": meta.get("branch"), "members": c_now, "prev": c_prev,
            "delta": delta, "own": own,
            "has_children": bool(now_children.get(did) or prev_children.get(did)),
            "kind": kind,
        })

    items.sort(key=lambda x: -max(abs(x["own"]), abs(x["delta"])))
    total_cur = int((graph.get("stats") or {}).get("members") or 0)
    total_prev = int(prev.get("total") or 0)
    return {"prev_at": prev.get("at"), "items": items,
            "total_delta": total_cur - total_prev, "total_prev": total_prev}


def collab_edges_from_assets(
    assets: list[dict],
    *,
    max_space_owners: int = 15,
    max_edges: int = 400,
) -> list[dict]:
    """从资产推导人—人协作边：两人若在同一 source_space 都拥有资产，则相连。

    - 跳过 owner 数 > ``max_space_owners`` 的"大杂烩"空间（全员可见的归档空间不是
      有意义的协作信号，否则一个空间就能炸出上百条边）。
    - 权重 = 两人共享的空间数；最终按权重倒序截断到 ``max_edges`` 条，保持可读。
    """
    space_owners: dict[str, set[str]] = defaultdict(set)
    for a in assets:
        oid = a.get("owner_id") or ""
        sp = (a.get("space") or "").strip()
        if oid.startswith("ou_") and sp:
            space_owners[sp].add(oid)

    weight: dict[tuple[str, str], int] = defaultdict(int)
    for owners in space_owners.values():
        if len(owners) < 2 or len(owners) > max_space_owners:
            continue
        ow = sorted(owners)
        for i in range(len(ow)):
            for j in range(i + 1, len(ow)):
                weight[(ow[i], ow[j])] += 1

    ranked = sorted(weight.items(), key=lambda kv: -kv[1])[:max_edges]
    return [{"a": a, "b": b, "weight": w} for (a, b), w in ranked]
