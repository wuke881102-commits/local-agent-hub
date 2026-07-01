"""协作分发 Agent — Phase B 实现。

把上游 Agent 的成果（尤其会议行动项 / 知识治理建议 / 文档摘要）或一段自由文本，
改写成可直接落地的协作动作：
  1) 一条适合发到飞书群的消息（Markdown）
  2) 一份可建成飞书任务的清单（标题 / 截止 / 备注）

沿用「规则算准 + LLM 增强」：素材里**已结构化的行动项**直接作为任务草稿（忠实保留负责人/
截止，不重写不丢项）；LLM 只负责把素材写成一条像样的群消息、并为非结构化素材抽取任务。

分析类 Agent（会议纪要 / PDF / 表格…）只读，其产出经「分发到飞书」按钮带入本 Agent，
在一个确认弹窗里勾选要落地的动作（默认都不勾，逐项 opt-in）。

所有分发都不直接执行——产出一条 ``batch_dispatch`` 写回提议（携带 N 个 建任务 / 发消息
子动作），经用户在确认弹窗里勾选确认后，才通过 lark-cli 真正建任务 / 发消息。

输入 inputs：
  - source_task_id   上游任务 id（可选）：拉它的 payload 作为分发素材
  - content          自由文本素材（可选）
  - mode             'message' | 'tasks' | 'both'（默认 both）
  - chat_id          目标群 chat_id（可选；给了才会把消息纳入分发）
  - chat_name        目标群名（可选，仅用于展示）
  - default_due      任务默认截止 YYYY-MM-DD（可选，给无截止的任务兜底）
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re

from .base import AgentContext, AgentResult, register_agent
from ..llm.prompts import build_collab_dispatch_prompt, build_action_extract_prompt
from ..services import task_runner

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CollabDispatchAgent:
    id = "collab-dispatch"
    name = "协作分发 Agent"
    description = "把会议行动项 / 治理建议 / 文档摘要改写成飞书群消息与任务草稿，确认后一键分发。"
    writeback_allowed = True

    async def run(self, ctx: AgentContext) -> AgentResult:
        inputs = ctx.inputs or {}
        default_due = (inputs.get("default_due") or "").strip()
        content = (inputs.get("content") or "").strip()
        source_task_id = (inputs.get("source_task_id") or "").strip()

        # ── 收集素材 ──
        source_info: dict | None = None
        brief_parts: list[str] = []
        if source_task_id:
            src = await task_runner.get_task(source_task_id)
            if not src:
                return AgentResult(task_id=ctx.task_id, status="failed", error=f"找不到上游任务 {source_task_id}")
            source_info = {"task_id": source_task_id, "agent_id": src.get("agent_id"), "title": src.get("target")}
            brief_parts.extend(_brief_from_task(src))
            await ctx.log("info", f"载入上游任务 {source_task_id}（{src.get('agent_id')}）")
        if content:
            brief_parts.append("# 补充素材\n" + content)
        brief = "\n\n".join(p for p in brief_parts if p).strip()
        if not brief:
            return AgentResult(task_id=ctx.task_id, status="failed",
                               error="没有可分发的素材：请选择一个上游任务，或填写要分发的内容。")

        # ── 两路并发：①生成群消息 + 判性质；②专职抽取待办（单一目标更稳，自由文本也抽得出）──
        await ctx.log("info", f"调用 {ctx.llm.text_model} 生成消息并抽取待办（两路并发）…")
        today = dt.date.today().isoformat()
        sys_m, user_m = build_collab_dispatch_prompt(brief=brief, today=today)
        sys_t, user_t = build_action_extract_prompt(brief=brief, today=today)
        msg_res, task_res = await asyncio.gather(
            ctx.llm.text_complete(user_m, system=sys_m, json_mode=True, max_tokens=1200, timeout=120, retries=1, temperature=0.3),
            ctx.llm.text_complete(user_t, system=sys_t, json_mode=True, max_tokens=1200, timeout=120, retries=1, temperature=0.1),
            return_exceptions=True,
        )

        kind = ""
        message = ""
        llm_tasks: list[dict] = []
        llm_err: str | None = None
        if isinstance(msg_res, Exception):
            llm_err = type(msg_res).__name__
            await ctx.log("warn", f"消息生成失败：{msg_res}")
        else:
            parsed = _safe_parse_json(msg_res) or {}
            kind = (parsed.get("kind") or "").strip().lower()
            message = (parsed.get("message") or "").strip()
        if isinstance(task_res, Exception):
            await ctx.log("warn", f"待办抽取失败：{task_res}")
        else:
            llm_tasks = _clean_tasks((_safe_parse_json(task_res) or {}).get("tasks"))

        task_candidates = llm_tasks
        if default_due:
            for t in task_candidates:
                if not t.get("due"):
                    t["due"] = default_due
        # 有待办但性质没判出来时，按含待办处理。
        if not kind:
            kind = "action" if task_candidates else "notice"
        kind_cn = {"notice": "通知/同步", "digest": "摘要/总结", "action": "含待办"}.get(kind, kind)
        await ctx.log("info", f"分发判断：{kind_cn} · 待办 {len(task_candidates)} 条 · 群消息 {'有' if message else '无'}")

        # ── 组装 batch_dispatch 写回提议 ──
        # 任务 + 群消息都放进 items；群消息**不预绑定群**，目标群在确认弹窗里选。
        items: list[dict] = []
        for t in task_candidates:
            pl: dict = {"title": t["title"]}
            due_norm = _norm_due(t.get("due"))
            if due_norm:
                pl["due"] = due_norm
            desc = _task_desc(t)
            if desc:
                pl["description"] = desc
            items.append({"action_type": "create_task", "label": t["title"], "payload": pl})
        if message:
            items.append({
                "action_type": "send_im",
                "label": "群消息",
                "payload": {"text": message, "markdown": True},  # chat_id 由确认弹窗注入
            })

        payload = {
            "source": source_info,
            "kind": kind,
            "message": message,
            "has_message": bool(message),
            "tasks": task_candidates,
            "task_dispatch_count": len(task_candidates),
            "dispatch_count": len(items),
            "llm_error": llm_err,
            "brief_preview": brief[:3000],
        }

        result = AgentResult(task_id=ctx.task_id, status="done", payload=payload)
        if items:
            result.writeback_proposal = {
                "action_type": "batch_dispatch",
                "target": "飞书任务 / 群消息",
                "preview_text": _dispatch_summary(items),
                "title": "协作分发",
                "kind": kind,  # 供确认弹窗决定任务默认勾选与否
                "items": items,
                "content_markdown": _items_to_markdown(items, message),
            }
            result.status = "preview"
        else:
            await ctx.log("warn", "没有可分发的项。")
        return result


# ── 从上游任务 payload 提取分发素材 ─────────────────────────────────

def _brief_from_task(src: dict) -> list[str]:
    """从上游任务 payload 拼出喂给 LLM 的素材段落；是否出任务、出几条由 LLM 判断性质后决定。"""
    p = src.get("payload") or {}
    aid = src.get("agent_id") or ""
    title = src.get("target") or aid
    parts: list[str] = [f"# 来源：{aid} · {title}"]

    if aid == "meeting-minutes":
        if p.get("summary"):
            parts.append("会议摘要：" + p["summary"])
        if p.get("decisions"):
            parts.append("决策：\n" + "\n".join(f"- {d}" for d in p["decisions"]))
        ais = p.get("action_items") or []
        if ais:
            lines = ["行动项（供你判断，是否建成任务由你决定）："]
            for a in ais:
                owner = a.get("owner") or "未指派"
                due = a.get("due") or "未定"
                lines.append(f"- {a.get('task','')} ｜负责人：{owner} ｜截止：{due}")
            parts.append("\n".join(lines))
        if p.get("risks"):
            parts.append("风险与阻塞：\n" + "\n".join(f"- {r}" for r in p["risks"]))
        return parts

    if aid == "html-page":
        # 分享一个生成好的页面：把页面要点改写成群消息（整页本身无法在群里渲染，
        # 如需分享整页请先写回飞书文档再分享链接）。
        if p.get("summary"):
            parts.append("页面摘要：" + str(p["summary"]))
        if p.get("highlights"):
            parts.append("页面要点：\n" + "\n".join(f"- {h}" for h in p["highlights"]))
        if p.get("next_steps"):
            parts.append("下一步：\n" + "\n".join(f"- {n}" for n in p["next_steps"]))
        return parts

    # 通用：尽量从常见字段拼素材
    if p.get("summary"):
        parts.append("摘要：" + str(p["summary"]))
    if p.get("highlights"):
        parts.append("要点：\n" + "\n".join(f"- {h}" for h in p["highlights"]))
    if isinstance(p.get("metrics"), dict) and p["metrics"]:
        parts.append("指标：" + json.dumps(p["metrics"], ensure_ascii=False)[:800])
    # 知识治理：把"建议归档"清单作为素材
    buckets = p.get("buckets") if isinstance(p.get("buckets"), dict) else None
    if buckets and buckets.get("archive"):
        names = [a.get("title") for a in buckets["archive"][:15] if a.get("title")]
        if names:
            parts.append("建议归档文档：\n" + "\n".join(f"- {n}" for n in names))
    if len(parts) == 1:  # 只有标题行，没抓到内容
        parts.append("（上游任务无可直接分发的结构化内容，将基于标题与补充素材生成。）")
    return parts


# ── 输出清洗 ───────────────────────────────────────────────────────

def _clean_tasks(v) -> list[dict]:
    if not isinstance(v, list):
        return []
    out: list[dict] = []
    for it in v:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or it.get("task") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "due": (it.get("due") or "").strip(),
            "owner": (it.get("owner") or "").strip(),
            "note": (it.get("note") or "").strip(),
        })
    return out[:40]


def _norm_due(due: str) -> str:
    """规范任务截止为 lark-cli ``task +create --due`` 接受的格式。

    lark-cli 只认 ISO 8601（如 ``2026-03-20`` 或 ``2026-03-20T15:04:05+08:00``）或 Unix
    时间戳。``YYYY-MM-DD`` 本身就是合法 ISO 日期，直接传（早先错误地加了 ``date:`` 前缀，
    会被 CLI 拒：``cannot parse time "date:2026-03-20"``）。ISO datetime 一并放行；
    其它无法识别的值（如 ``+2d`` / 自然语言）一律丢弃——宁可建一个无截止的任务，
    也不要因 due 解析失败导致整条建任务失败。
    """
    due = (due or "").strip()
    if not due:
        return ""
    if _DATE_RE.match(due):
        return due
    if re.match(r"^\d{4}-\d{2}-\d{2}T[\d:]{2,}", due):
        return due
    return ""


def _task_desc(t: dict) -> str:
    bits = []
    if t.get("owner"):
        bits.append(f"负责人：{t['owner']}")
    if t.get("note"):
        bits.append(t["note"])
    bits.append("（由本地 Agent 工作台 · 协作分发创建）")
    return "　".join(bits)


def _dispatch_summary(items: list[dict]) -> str:
    n_task = sum(1 for it in items if it["action_type"] == "create_task")
    n_msg = sum(1 for it in items if it["action_type"] == "send_im")
    bits = []
    if n_task:
        bits.append(f"{n_task} 个任务")
    if n_msg:
        bits.append(f"{n_msg} 条群消息")
    return "待分发：" + "、".join(bits) if bits else "无可分发项"


def _items_to_markdown(items: list[dict], message: str) -> str:
    lines: list[str] = []
    msgs = [it for it in items if it["action_type"] == "send_im"]
    tasks = [it for it in items if it["action_type"] == "create_task"]
    if msgs:
        lines.append("## 群消息")
        for m in msgs:
            lines.append(f"**发送至**：{m.get('label','')}")
            lines.append("")
            lines.append(m["payload"].get("text", ""))
            lines.append("")
    elif message:
        lines.append("## 群消息草稿（未指定目标群，不分发）")
        lines.append("")
        lines.append(message)
        lines.append("")
    if tasks:
        lines.append("## 任务")
        for t in tasks:
            pl = t["payload"]
            due = pl.get("due", "")
            extra = f"（截止 {due}）" if due else ""
            lines.append(f"- [ ] {pl.get('title','')} {extra}")
    return "\n".join(lines).strip() or "（无分发内容）"


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


register_agent(CollabDispatchAgent())
