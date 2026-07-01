"""Mock LarkCLI 实现 — 当 lark-cli 未安装或未授权时使用。

让 UI 可以独立演示 / 开发，并提供与真实 CLI 一致的接口签名。
数据风格沿用设计原型 data.jsx 中的样例。
"""
from __future__ import annotations

import asyncio
from typing import Any


SAMPLE_DOCS = [
    {"asset_id": "doc-201", "type": "doc",  "title": "Q3 营销战役复盘",       "space": "市场中心 / 战役复盘", "owner": "张子萱", "updated": "2026-05-26", "url": "https://example.feishu.cn/docx/doc-201"},
    {"asset_id": "doc-198", "type": "doc",  "title": "新员工入职手册 v3",      "space": "人事 / 制度",         "owner": "李珂",   "updated": "2026-05-24", "url": "https://example.feishu.cn/docx/doc-198"},
    {"asset_id": "doc-195", "type": "doc",  "title": "Agent 工作台 · PRD v0.1", "space": "产品中心 / PRD",      "owner": "陈昭",   "updated": "2026-05-22", "url": "https://example.feishu.cn/docx/doc-195"},
    {"asset_id": "doc-192", "type": "doc",  "title": "Lumen-light 设计系统说明",  "space": "设计 / 规范",         "owner": "苏黎",   "updated": "2026-05-19", "url": "https://example.feishu.cn/docx/doc-192"},
    {"asset_id": "doc-188", "type": "doc",  "title": "2026 年中战略说明（草）", "space": "战略 / 季度",         "owner": "王立",   "updated": "2026-05-15", "url": "https://example.feishu.cn/docx/doc-188"},
    {"asset_id": "wiki-31", "type": "wiki", "title": "产品中心 Wiki 首页",       "space": "产品中心",            "owner": "陈昭",   "updated": "2026-05-12", "url": "https://example.feishu.cn/wiki/wiki-31"},
    {"asset_id": "meet-44","type": "meeting","title":"2026 W22 产品周会",        "space": "产品中心 / 会议",     "owner": "陈昭",   "updated": "2026-05-25", "url": "https://example.feishu.cn/minutes/meet-44"},
    {"asset_id": "meet-43","type": "meeting","title":"Agent Hub 评审会议",       "space": "产品中心 / 会议",     "owner": "陈昭",   "updated": "2026-05-23", "url": "https://example.feishu.cn/minutes/meet-43"},
    {"asset_id": "base-12","type": "base", "title": "销售线索池 2026Q2",         "space": "销售 / 数据",         "owner": "吴楠",   "updated": "2026-05-27", "url": "https://example.feishu.cn/base/base-12"},
    {"asset_id": "sheet-7","type": "sheet","title": "部门预算 H1 跟踪",          "space": "财务 / 预算",         "owner": "何静",   "updated": "2026-05-20", "url": "https://example.feishu.cn/sheets/sheet-7"},
]


SAMPLE_MARKDOWN = """# Q3 营销战役复盘

## 背景与目标
Q3 围绕新品发布与渠道渗透两个主题，开展为期 8 周的整合营销战役。整体目标为：
- 新品认知触达不低于 300 万用户
- 留资转化率较 Q2 提升 ≥ 2 个百分点
- 渠道获客单价（CPA）下降 10% 以上

## 战役回顾
### 投放节奏
- W21–W22：预热，KOL 软投 + 朋友圈互动广告
- W23–W24：上线高峰，效果广告 + 直播带货
- W25–W28：长尾承接，内容种草 + 私域转化

### 创意素材
共产出 24 组核心素材，其中 8 组为视频形态。视频素材在 CTR 上整体领先静态素材 1.8 个百分点。

## 关键指标
| 指标 | 数值 | 同环比 |
|---|---|---|
| 触达用户 | 3.42M | +18% vs Q2 |
| CTR | 4.6% | +0.7pt |
| CPA | ¥18.4 | −12% |
| 留资转化 | 11.8% | +2.1pt |

## 问题与归因
1. 直播带货 GMV 未达预期，主要受 SKU 准备不足影响。
2. 私域承接链路存在断点，留资用户回访率仅 27%。
3. 区域差异较大，华东表现明显优于西南。

## 下一步建议
- 联合供应链提前 4 周锁定核心 SKU
- 私域 SOP 标准化，引入企微自动应答
- 区域差异化投放预算分配，优先 ROI 高地区
"""


SAMPLE_MEETING_TRANSCRIPT = """陈昭：今天 W22 产品周会，主要对齐三件事——Agent Hub 的发布节奏、会议纪要 Agent 的范围，还有下季度 OKR 草稿。
吴楠：先说发布。HTML 页面生成已经端到端跑通了，PDF 识别上周也上线了。我建议这周五先灰度给产品中心内部用，下周一全量。
陈昭：可以，那就定下周一全量发布。灰度期间的反馈吴楠你汇总一下。
李珂：会议纪要 Agent 这块，范围我们要明确。妙记转写不一定每个人都有权限，所以也要支持普通会议记录文档。
陈昭：同意。范围就定成：妙记和会议记录文档都支持，产出包括摘要、决策、行动项、风险。李珂你这周内出一版设计稿。
苏黎：风险方面，妙记 transcript 接口的权限 scope 是个不确定项，可能部分用户取不到转写。
陈昭：这个先做优雅降级，取不到就提示改用文档。另外下季度 OKR 草稿，大家周三前各自把条目提到共享文档里。
吴楠：还有个遗留问题，组织架构图谱的部门数据偶尔会缺，我跟数据那边再确认下。
陈昭：好，今天就到这里。"""


class MockLarkCLI:
    """与 LarkCLI 接口对齐的 mock。"""

    def __init__(self):
        self._version = "mock-0.1.0"
        self._authed = True  # mock 模式下默认已"授权"

    async def ping(self) -> bool:
        return True

    @property
    def version(self) -> str | None:
        return self._version

    async def auth_login(self, recommend: bool = True, no_wait: bool = True, scope: str | None = None, force: bool = False) -> dict:
        await asyncio.sleep(0.1)
        return {
            "verification_uri": "https://example.feishu.cn/oauth/device",
            "user_code": "MOCK-1234",
            "expires_in": 600,
            "interval": 5,
            "mock": True,
        }

    async def auth_status(self) -> dict:
        return {
            "authenticated": self._authed,
            "user_id": "ou_mock_user_001",
            "user_name": "陈昭（mock）",
            "scopes": ["docs:document:read", "wiki:wiki:read", "im:message:create"],
            "mock": True,
        }

    async def auth_list(self) -> list[dict]:
        return [await self.auth_status()]

    async def docs_list(self, page_all: bool = True, page_limit: int = 5) -> list[dict]:
        await asyncio.sleep(0.2)
        return [d for d in SAMPLE_DOCS if d["type"] == "doc"]

    async def docs_get(self, token: str) -> dict:
        match = next((d for d in SAMPLE_DOCS if d["asset_id"] == token), SAMPLE_DOCS[0])
        return {**match, "token": token}

    async def docs_export_markdown(self, token: str) -> str:
        await asyncio.sleep(0.3)
        return SAMPLE_MARKDOWN

    async def drive_download_file(self, file_token: str, dest_dir, filename: str) -> dict:
        # mock 模式拿不到真实 PDF 字节；明确报错，让 PDF 识别 Agent 友好降级。
        raise RuntimeError("mock 模式不支持下载真实 PDF（请连接飞书后再试）")

    async def docs_create_markdown(self, title: str, content: str, folder_token: str | None = None) -> dict:
        await asyncio.sleep(0.2)
        return {
            "document_id": "doc-mock-new",
            "url": "https://example.feishu.cn/docx/doc-mock-new",
            "title": title,
            "mock": True,
        }

    async def wiki_spaces_list(self) -> list[dict]:
        return [{"space_id": "wiki-31", "name": "产品中心 Wiki", "description": "产品中心知识库根空间"}]

    async def wiki_nodes_list(self, space_id: str) -> list[dict]:
        return [d for d in SAMPLE_DOCS if d["type"] == "wiki"]

    async def base_list_apps(self) -> list[dict]:
        return [d for d in SAMPLE_DOCS if d["type"] == "base"]

    async def base_tables_list(self, app_token: str) -> list[dict]:
        return [{"table_id": "tbl-mock-001", "name": "线索主表"}, {"table_id": "tbl-mock-002", "name": "回访记录"}]

    async def sheets_get(self, spreadsheet_token: str) -> dict:
        return {"spreadsheet_token": spreadsheet_token, "title": "部门预算 H1 跟踪", "sheets": []}

    async def calendar_events(self) -> list[dict]:
        return [{"event_id": "ev-mock-1", "summary": "Agent Hub 评审", "start_time": "2026-05-28T14:00:00+08:00"}]

    async def minutes_list(self) -> list[dict]:
        return [d for d in SAMPLE_DOCS if d["type"] == "meeting"]

    async def minutes_get_content(self, minute_token: str) -> dict:
        await asyncio.sleep(0.2)
        match = next((d for d in SAMPLE_DOCS if d["asset_id"] == minute_token), None)
        return {
            "title": (match or {}).get("title", "2026 W22 产品周会"),
            "url": (match or {}).get("url", "https://example.feishu.cn/minutes/meet-44"),
            "duration_ms": "2730000",
            "transcript": SAMPLE_MEETING_TRANSCRIPT,
        }

    async def im_send(self, chat_id: str, text: str, dry_run: bool = False, markdown: bool = False) -> dict:
        return {"message_id": "om-mock-001", "chat_id": chat_id, "dry_run": dry_run, "mock": True}

    async def im_chat_list(self, *, page_size: int = 50) -> list[dict]:
        return [
            {"chat_id": "oc_mock_product", "name": "产品中心 · 大群", "members": 42},
            {"chat_id": "oc_mock_dt", "name": "DT 技术组", "members": 18},
            {"chat_id": "oc_mock_pmo", "name": "PMO 项目协同", "members": 7},
        ]

    async def task_create(self, title: str, due: str | None = None, description: str | None = None, dry_run: bool = False) -> dict:
        return {"task_id": "task-mock-001", "title": title, "due": due, "dry_run": dry_run, "mock": True}

    async def api(self, method: str, path: str, data: dict | None = None, params: dict | None = None) -> dict:
        return {"mock": True, "method": method, "path": path}
