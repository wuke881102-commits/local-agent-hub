"""多维表格分析 Agent — 出图导向（图表 / 看板生成）。

指向一张多维表格(bitable)或电子表格(sheet)，读结构 + 数据样本。多维表格 / 电子表格
里的**每一张**子表 / 工作表都会被逐张分析（上限见 table_reader.MAX_TABLES），每张产出：
  1) 确定性列画像（类型 / 填充率 / 去重 / 数值统计）  ← services.table_profile
  2) AI 出图规划：基于画像规划若干图（图型 + 用哪些真实列 + 怎么聚合）
  3) 渲染：数据图用 ECharts（数字由 services.chart_builder 在真实数据上精确聚合），
     甘特用 Mermaid，架构 / 关系图用 GPT-Image-1 生图

只读，不写回。沿用「规则算准，LLM 增强」：Python 把数字算对，模型只规划图型与取数方式。

输入 inputs：
  - asset_id     表 token（必填）
  - asset_type   'bitable' | 'base' | 'sheet'（可选，缺省从本地索引查）
  - template     出图模板（auto/trend/composition/ranking/gantt/architecture/custom，缺省 auto）
  - custom_instruction  自定义模板下的出图要求
  - skip_llm     True 则跳过 AI 规划与出图（精简模式调试用）

输出 payload：tables[] 为逐张表的结果块（analyzed/metrics/columns/summary/charts/preview），
另保留 summary/metrics/analyzed 顶层兼容字段（取首张非空表）供单结果消费方读取。
charts[] 元素：{engine:'echarts',option} | {engine:'mermaid',mermaid} | {engine:'image',image_url|placeholder}。
"""
from __future__ import annotations

import json
import re
import uuid

from .base import AgentContext, AgentResult, register_agent
from ..config import settings
from ..llm.prompts import build_chart_plan_prompt, CHART_ANALYSIS_TEMPLATES
from ..services import chart_builder, index_service, table_profile, table_reader


class BaseAnalysisAgent:
    id = "base-analysis"
    name = "多维表格分析 Agent"
    description = "读取多维表格 / 电子表格的结构与数据，做列画像、数据质量体检，并给出报表建议。"
    writeback_allowed = False

    async def run(self, ctx: AgentContext) -> AgentResult:
        inputs = ctx.inputs or {}
        # 数据源二选一：本地目录里的 Excel/CSV（local_path）或飞书原生表 / 云盘 Excel（asset_id）
        local_path = (inputs.get("local_path") or "").strip()
        asset_id = (inputs.get("asset_id") or inputs.get("token") or "").strip()
        if not asset_id and not local_path:
            return AgentResult(task_id=ctx.task_id, status="failed", error="缺少 asset_id（要分析的表 token）或 local_path（本地表格）")

        template = (inputs.get("template") or "auto").strip().lower()
        if template not in CHART_ANALYSIS_TEMPLATES:
            template = "auto"
        custom_instruction = (inputs.get("custom_instruction") or "").strip()

        if local_path:
            from pathlib import Path  # noqa: PLC0415
            p = Path(local_path)
            if not p.is_file():
                return AgentResult(task_id=ctx.task_id, status="failed", error="本地表格文件不存在")
            meta = {}
            asset_type = "xlsx"
            asset_id = local_path           # 用作 payload / 缓存键
            title = p.name
            kind = "xlsx"
        else:
            # 1) 索引里查元信息（标题 / 空间 / url / 类型）
            meta = await _find_asset(asset_id)
            asset_type = (inputs.get("asset_type") or (meta.get("type") if meta else "") or "").lower()
            title = (meta.get("title") if meta else None) or asset_id
            # 路线判别：原生多维表/电子表，或云盘上传的 Excel 文件（本地解析）。
            kind = table_reader.detect_kind(asset_type, title)
            if not kind:
                # 兜底：token 形态无法判别时，默认按多维表格试
                kind = "bitable"
                await ctx.log("warn", f"未知资产类型 {asset_type!r}，按多维表格处理")

        kind_open = {"bitable": "多维表格", "sheet": "电子表格", "xlsx": "Excel/CSV 文件"}.get(kind, "表")
        await ctx.log("info", f"开始分析「{title}」（{kind_open}）")

        # 2) 读结构 + 数据：一次性读出**所有**子表 / 工作表，逐张分析。
        #    分析任务总读最新，但每张表都会写入缓存供「问数据」复用。
        try:
            bundle = await table_reader.load_all_tables(
                ctx.lark, asset_id, kind,
                url=(meta.get("url") if meta else None),
                filename=title,
                local_path=local_path or None,
            )
        except Exception as e:  # noqa: BLE001
            return AgentResult(task_id=ctx.task_id, status="failed",
                               error=f"读取表数据失败：{type(e).__name__}: {str(e)[:200]}")
        # 知识库托管的表会在 reader 里解析底层对象，可能校正 kind（bitable↔sheet）；以实读为准。
        kind = bundle.get("kind", kind)
        targets = bundle["targets"]
        tables_raw = bundle["tables"]
        truncated = bundle.get("truncated", False)
        total = len(tables_raw)
        kind_label = "多维表格" if kind == "bitable" else "电子表格"
        await ctx.log(
            "info",
            f"{kind_label}共 {len(targets)} 张表/工作表，将逐张分析"
            + (f"（数量较多，仅分析前 {total} 张）" if truncated else f"（全部 {total} 张）"),
        )

        # 3) 逐张：确定性画像 + 异常 + LLM 解读
        skip_llm = bool(inputs.get("skip_llm"))
        tables: list[dict] = []
        for i, entry in enumerate(tables_raw, 1):
            tables.append(await self._analyze_one(
                ctx, entry, template=template, custom_instruction=custom_instruction,
                fallback_title=title, idx=i, total=total, skip_llm=skip_llm,
            ))

        if all(t.get("empty") for t in tables):
            return AgentResult(task_id=ctx.task_id, status="failed",
                               error="未读到任何字段 / 列。可能都是空表，或当前账号无读取权限。")

        # 4) 组装 payload。新结构以 tables[] 为主；同时保留若干顶层兼容字段
        #    （summary / metrics / analyzed），供 collab-dispatch 等以"单结果"消费的旧逻辑读取。
        first = next((t for t in tables if not t.get("empty")), tables[0])
        combined_summary = "\n".join(
            f"「{t['analyzed']['name']}」：{t['summary']}".strip()
            for t in tables
            if (t.get("summary") or "").strip()
        )
        payload = {
            "asset_id": asset_id,
            "title": title,
            "url": (meta.get("url") if meta else "") or "",
            "space": (meta.get("space") if meta else "") or "",
            "kind": kind,
            "asset_type": asset_type or kind,
            "template": template,
            "targets": targets,
            "truncated": truncated,
            "tables": tables,
            # ── 兼容字段（单结果消费方）──
            "summary": combined_summary,
            "analyzed": first.get("analyzed"),
            "metrics": first.get("metrics", {}),
        }
        return AgentResult(task_id=ctx.task_id, status="done", payload=payload)

    async def _analyze_one(
        self, ctx: AgentContext, entry: dict, *,
        template: str, custom_instruction: str,
        fallback_title: str, idx: int, total: int, skip_llm: bool,
    ) -> dict:
        """分析单张表/工作表 → 一个 table 结果块（画像 + AI 出图规划 + 渲染 + 预览）。"""
        analyzed = entry["analyzed"]
        name = analyzed.get("name") or fallback_title
        headers, rows, sampled = entry["headers"], entry["rows"], entry["sampled"]
        tag = f"[{idx}/{total}] " if total > 1 else ""

        if not headers:
            await ctx.log("warn", f"{tag}「{name}」未读到任何字段（空表或无读取权限），跳过。")
            return {
                "analyzed": analyzed, "empty": True,
                "row_count": 0, "column_count": 0, "sampled": False,
                "metrics": {}, "columns": [], "summary": "", "charts": [],
                "note": "空表或当前账号无读取权限，已跳过。",
                "preview": {"headers": [], "rows": []},
            }

        await ctx.log("info", f"{tag}「{name}」已读取 {len(headers)} 列 × {len(rows)} 行{'（已达采样上限，仅分析样本）' if sampled else ''}，开始列画像 …")
        prof = table_profile.profile_table(headers, rows)
        columns, metrics = prof["columns"], prof["metrics"]
        await ctx.log("info", f"{tag}「{name}」画像完成：{metrics['column_count']} 列、整体填充率 {metrics['overall_fill']}%")

        summary = ""
        charts: list[dict] = []
        note = ""
        if skip_llm:
            note = "已跳过 AI 出图（skip_llm）。"
        elif not rows:
            note = "表中没有数据行，无法出图。"
        else:
            compact = table_profile.compact_for_llm(
                {"name": name, "kind": entry["kind"]},
                columns, metrics, [], rows,
            )
            system, user = build_chart_plan_prompt(compact, template=template, custom_instruction=custom_instruction)
            await ctx.log("info", f"{tag}「{name}」按「{CHART_ANALYSIS_TEMPLATES[template]['label']}」模板调用 {ctx.llm.text_model} 规划出图 …")
            try:
                raw = await ctx.llm.text_complete(
                    user, system=system, json_mode=True, max_tokens=2000,
                    timeout=120, retries=1,
                )
                parsed = _safe_parse_json(raw) or {}
                summary = (parsed.get("summary") or "").strip()
                specs = _clean_list(parsed.get("charts"))
                await ctx.log("info", f"{tag}「{name}」AI 规划了 {len(specs)} 张图，开始渲染 …")
                for spec in specs[:8]:
                    try:
                        built = chart_builder.build_chart(headers, rows, columns, spec)
                    except Exception as e:  # noqa: BLE001
                        await ctx.log("warn", f"{tag}「{name}」一张图渲染失败（跳过）：{type(e).__name__}")
                        built = None
                    if not built:
                        continue
                    if built.get("engine") == "image":
                        built = await self._render_image(ctx, built, tag=tag, name=name)
                    charts.append(built)
                kinds = ", ".join(f"{c['engine']}/{c.get('type', '')}" for c in charts) or "无"
                await ctx.log("info", f"{tag}「{name}」出图完成：{len(charts)} 张（{kinds}）")
                if not charts and not summary:
                    note = "AI 未能从这张表规划出可靠的图（可能列结构不适合或数据太少）。"
            except Exception as e:  # noqa: BLE001
                await ctx.log("warn", f"{tag}「{name}」AI 出图规划失败（不阻塞）：{type(e).__name__}")
                note = f"AI 出图规划失败：{type(e).__name__}"

        return {
            "analyzed": analyzed,
            "row_count": metrics["row_count"],
            "column_count": metrics["column_count"],
            "sampled": sampled,
            "metrics": metrics,
            "columns": columns,
            "summary": summary,
            "charts": charts,
            "note": note,
            "preview": _build_preview(headers, rows, limit=8),
        }

    async def _render_image(self, ctx: AgentContext, built: dict, *, tag: str, name: str) -> dict:
        """把 engine=image 的规划用 GPT-Image-1 渲染成 PNG，落盘并回填 image_url。

        未配置 / 失败 → 标记 placeholder + note，前端显示占位卡片（不阻塞其它图）。
        """
        prompt = (built.pop("image_prompt", "") or "").strip()
        if not prompt:
            built["placeholder"] = True
            built["note"] = "未给出生图描述，已跳过。"
            return built
        if not ctx.llm.image_available:
            built["placeholder"] = True
            built["note"] = "未配置 GPT-Image-1（在 backend/.env 填好 IMAGE_MODEL_* 后重跑即可）。"
            return built
        try:
            await ctx.log("info", f"{tag}「{name}」调用 {ctx.llm.image_model} 生成架构 / 关系图 …")
            imgs = await ctx.llm.image_generate(prompt, timeout=180)
        except Exception as e:  # noqa: BLE001
            built["placeholder"] = True
            built["note"] = f"生图失败（不阻塞）：{type(e).__name__}"
            return built
        if not imgs:
            built["placeholder"] = True
            built["note"] = "生图返回空结果。"
            return built
        img_dir = settings.draft_path / f"_charts_{ctx.task_id}"
        img_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{uuid.uuid4().hex[:12]}.png"
        (img_dir / fname).write_bytes(imgs[0])
        built["image_url"] = f"/api/base/chart-image/{ctx.task_id}/{fname}"
        return built


# ── 工具 ─────────────────────────────────────────────────────────

def _build_preview(headers: list[str], rows: list[list], *, limit: int = 8) -> dict:
    flat_rows = []
    for r in rows[:limit]:
        flat_rows.append([
            (str(table_profile.flatten_cell(r[c])) if c < len(r) and table_profile.flatten_cell(r[c]) is not None else "")[:60]
            for c in range(len(headers))
        ])
    return {"headers": headers, "rows": flat_rows}


async def _find_asset(asset_id: str) -> dict:
    for a in await index_service.list_assets(limit=2000):
        if a.get("asset_id") == asset_id:
            return a
    return {}


def _clean_list(v) -> list:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _safe_parse_json(text: str) -> dict | None:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


register_agent(BaseAnalysisAgent())
