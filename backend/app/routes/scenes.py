from __future__ import annotations

from fastapi import APIRouter
import yaml
from pathlib import Path

from ..config import settings

router = APIRouter(prefix="/api/scenes", tags=["scenes"])


# 后备静态场景列表（与 config/agents.yaml 等价兜底）
FALLBACK_SCENES = [
    {"id": "knowledge-gov", "title": "知识库治理", "subtitle": "全量盘点分组 · 失修/重复/无主检测 · 归档/合并/转交建议", "agents": ["文档地图", "知识治理"], "accent": "#2563EB", "icon": "shield"},
    {"id": "content",       "title": "内容生成",   "subtitle": "飞书文档套企业模板 · 内部Wiki/项目/公告 · 一键生成可预览 HTML", "agents": ["HTML 页面生成", "文档地图"], "accent": "#16A34A", "icon": "page", "featured": True},
    {"id": "meeting",       "title": "会议沉淀",   "subtitle": "妙记/会议文档 · 摘要 · 决策 · 行动项 · 风险", "agents": ["会议纪要"], "accent": "#EA580C", "icon": "mic"},
    {"id": "table",         "title": "表格分析",   "subtitle": "多维表/电子表 · 列画像 · 数据体检 · AI 自动出图/看板", "agents": ["多维表格分析"], "accent": "#F0A800", "icon": "table"},
    {"id": "pdf",           "title": "PDF 识别",   "subtitle": "云盘 PDF · 全文(含扫描 OCR) · 字段/表格 · 逐页要点", "agents": ["PDF 识别"], "accent": "#6A4DD4", "icon": "scan"},
    {"id": "dispatch",      "title": "协作分发",   "subtitle": "群消息 + 任务草稿 · 通知/摘要/待办自动判别 · 确认后分发", "agents": ["协作分发"], "accent": "#C83A3A", "icon": "send"},
    {"id": "auto-extract",  "title": "自动化提炼", "subtitle": "按 Enter 自动留痕截图 · 定时用大模型提炼工作说明/重点/操作/会议", "agents": ["自动化提炼"], "accent": "#0EA5E9", "icon": "funnel"},
]


@router.get("")
async def list_scenes() -> dict:
    cfg = settings.config_path / "scenes.yaml"
    if cfg.exists():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {"items": data}
        except Exception:  # noqa: BLE001
            pass
    return {"items": FALLBACK_SCENES}
