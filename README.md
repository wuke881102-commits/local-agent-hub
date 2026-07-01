# 本地 Agent 工作台（Local Agent Hub）

> 一个**运行在本机**的桌面工具。它通过飞书官方 CLI 读取你有权限访问的飞书文档、知识库、多维表格、会议纪要、云盘文件（Excel / Word / PDF / HTML），在本地建立索引，再用内置的 AI Agent 把这些内容整理成可复用的成果——生成企业内部 HTML 页面、提炼会议纪要、分析多维表格、治理知识库等。**所有数据留在本机，所有写回飞书的动作都需你显式确认。**

面向企业内部用户设计：不部署服务器、不上云、只监听 `127.0.0.1`；凭据存于系统钥匙串与本地 `.env`，界面永不显示任何 API Key。

---

## 目录

- [它能做什么](#它能做什么)
- [工作原理（一图看懂）](#工作原理一图看懂)
- [快速开始](#快速开始)
- [配置](#配置)
- [完整模式 vs Mock 模式](#完整模式-vs-mock-模式)
- [目录结构](#目录结构)
- [开发](#开发)
- [打包发版](#打包发版)
- [安全与隐私](#安全与隐私)
- [许可](#许可)

---

## 它能做什么

内置多个 AI Agent，每个对应一类企业内部场景：

| Agent | 作用 |
|---|---|
| **HTML 页面生成** | 把 1–3 篇飞书资产（文档 / 多维表 / 电子表 / 幻灯片）套入内置 **Lumen-light** 设计系统，生成可预览、可下载、可写回飞书的单文件企业内部页面。支持「套模板」与「AI 自由版式」两种。 |
| **会议纪要提炼** | 从飞书会议纪要抽取重点、决议与待办，可一键把待办写回飞书任务。 |
| **多维表格分析** | 对多维表格 / 电子表格做数据质量体检、报表看板、业务洞察，并可生成概念图。 |
| **文档地图** | 扫描你可访问的资产，建立本地索引与目录地图。 |
| **知识治理** | 识别过期、重复、无属主的文档，给出治理建议。 |
| **协作分发** | 把成果分发到指定群 / 邮件（默认关闭批量分发，防误发）。 |
| **自动化提炼** | 工作期间按 Enter 自动留痕当前窗口截图（存本机私有目录），每隔一段时间用视觉模型提炼「这段时间在做什么」。单次会话最长 10 小时自动停止。 |
| **本地目录 / Office 解析** | 读取本地 Excel / Word / PDF / HTML，纳入索引与内容生成。 |
| **组织关系图** | 基于通讯录生成组织关系可视化。 |

> 未配置飞书 CLI 或模型 Key 时，系统自动进入 **Mock 模式**，UI 仍可完整跑通全流程，便于离线演示与二次开发。

---

## 工作原理（一图看懂）

```
        浏览器 (React 前端)
              │  HTTP / SSE，仅 127.0.0.1
              ▼
   ┌──────────────────────────────────────────────────────┐
   │          本地后端  FastAPI  (127.0.0.1:8787)            │
   │                                                        │
   │     路由 routes/  ──►  Agent 框架 agents/               │
   │                            │                           │
   │        ┌───────────────────┼────────────────────┐      │
   │        ▼                   ▼                    ▼      │
   │   飞书适配 feishu/     LLM 客户端 llm/      本地服务 services/ │
   │   (lark-cli 子进程)   (文本/视觉/生图)    (索引/解析/截图/审计) │
   │        │                   │                    │      │
   │        ▼                   ▼                    ▼      │
   │   飞书 OpenAPI          模型服务            SQLite + 本地文件 │
   │   (你的授权范围)       (你自己的 Key)       (backend/data/)   │
   └──────────────────────────────────────────────────────┘
```

一次典型任务的生命周期：

1. **刷新索引**：后端调用 `lark-cli` 拉取你有权限访问的文档 / 知识库 / 多维表 / 纪要元数据，写入本地 SQLite。
2. **选资产 → 起任务**：前端把 `{agent_id, inputs, scene}` 发给 `/api/tasks/run`；后端在 `task_runner` 里异步执行对应 Agent。
3. **实时日志**：前端通过 SSE (`/api/tasks/{id}/stream`) 实时看到 Agent 的每一步。
4. **产出预览**：Agent 调用 LLM + 飞书数据生成结果（如 HTML 草稿），落到 `backend/data/drafts/`。
5. **写回确认**：任何写回飞书的动作都先弹出确认框展示完整内容，**你确认后**才通过 `lark-cli` 执行，并记入审计日志。

**关键设计**：飞书访问全部走官方 `@larksuite/cli` 子进程（授权一次，凭据存系统钥匙串）；模型调用走你自己的 Key；后端只监听本地回环地址，不开放公网。生产打包时后端用 `StaticFiles` 同源托管前端构建产物，无需单独跑前端服务。

更深入的组件、数据流与扩展点见 [`docs/架构与工作原理.md`](docs/架构与工作原理.md)。

---

## 快速开始

### 前置依赖

- **Node.js ≥ 18**（用于飞书 CLI `@larksuite/cli` 与前端构建）
- **Python 3.11**
- 一个飞书账号（用于授权）；可选：文本 / 视觉模型的 API Key

### Windows · 双击运行

双击根目录的 **`启动本地Agent.bat`**，无需打开终端。脚本会自动：

1. 检测 Node.js / Python（缺失时给出下载链接）
2. 首次运行自动安装飞书 CLI（`npx @larksuite/cli@latest install`，约 5–10 分钟，仅首次）
3. 复制 `backend/.env.example` → `backend/.env`
4. 创建 Python venv 并安装后端依赖
5. 安装前端依赖
6. 启动后端 `127.0.0.1:8787` 与前端 `127.0.0.1:5173`
7. 打开浏览器进入工作台，首页顶部的「立即授权飞书」按钮引导你完成授权

### macOS / Linux

```bash
chmod +x scripts/start.sh
scripts/start.sh
```

### 停止服务

关闭名为 *feishu-agent · 后端* / *feishu-agent · 前端* 的两个窗口即可；macOS / Linux 在终端按 `Ctrl+C`。

---

## 配置

### 1. 飞书 CLI 安装与授权

```bat
scripts\install-lark-cli.cmd
:: 或手动：
npx @larksuite/cli@latest install
lark-cli auth login --recommend
```

授权一次即可，凭据存于系统钥匙串。系统不在 UI 中显示任何凭据。

> 若要使用**你自己的专用飞书应用**（而非默认应用），在 `backend/.env` 里填 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，首启会以非交互方式初始化；留空则走交互式创建/选择。

### 2. 模型配置（`backend/.env`）

文本与视觉模型可分别独立配置，支持两类提供方：

**文本模型 · OpenAI 兼容端点（如阿里百炼 / DashScope）**
```env
TEXT_MODEL_PROVIDER=openai_compatible
TEXT_MODEL=qwen-plus
TEXT_MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
TEXT_MODEL_API_KEY=sk-your-key
```

**视觉模型 · Azure OpenAI**
```env
VISION_MODEL_PROVIDER=azure
VISION_MODEL=your-deployment-name          # Azure 上的「部署名」，不是模型名
VISION_MODEL_AZURE_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/
VISION_MODEL_API_VERSION=2024-12-01-preview
VISION_MODEL_API_KEY=your-azure-key
```

**其它**
```env
LARK_CLI_BIN=lark-cli
ENABLE_MOCK_FALLBACK=true
APP_PORT=8787
```

完整字段与注释见 [`backend/.env.example`](backend/.env.example)。**任何 Key 留空或为占位值，对应模型自动进入 Mock，UI 仍可演示；文本与视觉的 Mock 状态彼此独立。**

---

## 完整模式 vs Mock 模式

| 依赖 | 完整模式 | Mock 回退 |
|---|---|---|
| `lark-cli`（@larksuite/cli） | 必需，需 `auth login` | 返回示例资产数据 |
| LLM API Key（`backend/.env`） | 必需 | LLM 返回模板 JSON |
| SQLite | 自动创建于 `backend/data/index.sqlite` | 同 |

Mock 模式下 UI 可完整跑通流程（含 HTML 生成 + 写回确认 → 模拟返回值），适合离线演示与开发。

---

## 目录结构

```
.
├── backend/              # Python 3.11 + FastAPI 本地服务
│   └── app/
│       ├── routes/       # HTTP / SSE 路由
│       ├── agents/       # 各 AI Agent 的实现（import 即注册）
│       ├── services/     # 索引、解析、截图、审计等本地服务
│       ├── feishu/       # 飞书适配：lark-cli 子进程 + mock
│       ├── llm/          # 文本 / 视觉 / 生图模型客户端与 prompts
│       ├── html/         # Lumen-light 设计系统 + HTML 渲染器 + 模板
│       ├── config.py     # 配置（读 .env）
│       └── main.py       # FastAPI 入口
├── frontend/             # Vite + React + TypeScript
│   └── src/pages/        # 工作台各页面
├── config/               # 团队共享配置（agents / 模板 / 分类规则）
├── scripts/              # 启动与安装脚本（Windows / macOS / Linux）
├── build/                # 打包（PyInstaller + Inno Setup）
├── product-overview.html # 产品概览页（可视化介绍）
├── docs/                 # 架构与工作原理文档
└── README.md
```

---

## 开发

```bash
# 后端
cd backend
python -m venv .venv
.\.venv\Scripts\activate        # Windows；macOS/Linux: source .venv/bin/activate
pip install -e .                # 依赖声明在 pyproject.toml
python -m uvicorn app.main:app --reload   # http://127.0.0.1:8787

# 前端
cd frontend
npm install
npm run dev                     # http://127.0.0.1:5173（已代理 /api → 8787）
```

后端 OpenAPI 文档：`http://127.0.0.1:8787/docs`

---

## 打包发版

Windows 一键打包为单安装包（PyInstaller 编译后端 + `npm run build` 前端 + Inno Setup）：

```bat
scripts\bump_version.ps1 X.Y      :: 同步 4 处版本号
build\build_installer.cmd         :: 产出 dist-installer\LocalAgentHub-Setup-X.Y.exe
```

> 打包依赖 [Inno Setup 6](https://jrsoftware.org/isdl.php) 与内置 Node 运行时。生产环境的密钥请放在 `build/production.env`（已被 `.gitignore` 忽略，不会进仓库）。

---

## 安全与隐私

- **仅监听 `127.0.0.1`**，不暴露公网，不部署服务器。
- 飞书凭据由 `lark-cli` 存于**系统钥匙串**；模型 Key 存于本地 `.env`（已 gitignore）。
- **界面永不显示任何 API Key / 密钥。**
- **所有写回飞书的动作必须显式用户确认**，并记入本地审计日志。
- 本地只存元数据与草稿，不默认持久化完整文档正文。
- ⚠️ 模型调用会把必要上下文发送到你配置的模型服务，请确保符合团队隐私合规要求。
- 仓库不含任何真实凭据；请填入你自己的飞书应用与模型 Key。

---

## 许可

本项目基于 [MIT License](LICENSE) 开源。内置 **Lumen-light** 轻量绿色设计系统。
