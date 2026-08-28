<p align="center">
  <img src="docs/assets/banner.svg" alt="本地 Agent 工作台 · Local Agent Hub" width="100%">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-00AA4F.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/React-61DAFB?logo=react&logoColor=black" alt="React">
  <img src="https://img.shields.io/badge/Vite-646CFF?logo=vite&logoColor=white" alt="Vite">
  <img src="https://img.shields.io/badge/飞书%2FLark-00D6B9" alt="Feishu / Lark">
  <img src="https://img.shields.io/badge/Local--first-16A34A" alt="Local-first">
</p>

<p align="center">
  <b>一个面向企业用户的、本地优先的 AI 工作台。</b>把你手头的资料——本机的 Excel / Word / PDF / PPT / 网页，以及工作时随手按 Enter 留痕的截图——在本地建立索引，<br>用内置的 AI Agent 生成 HTML 页面、识别 PDF、分析表格，并自动提炼你的工作记录。<br>同时<b>接入飞书</b>，读取并治理你有权限的文档 / 知识库 / 多维表格 / 会议纪要。<br><b>数据留在本机；写回飞书 / 发消息一律需你确认，调用模型发出去多少界面每次都写明、也可以关掉。</b>
</p>

<p align="center">
  <a href="#-快速开始">快速开始</a> ·
  <a href="#-内置-agent">能力</a> ·
  <a href="#-任务场景">场景</a> ·
  <a href="#-工作原理">工作原理</a> ·
  <a href="docs/架构与工作原理.md">架构文档</a>
</p>

---

## ✨ 为什么用它

- 🖥️ **本地文档，直接处理** — 选本机目录里的 Excel / Word / PDF / PPT / 网页，用 openpyxl / python-docx / PyMuPDF / BeautifulSoup **在本地解析**，直接生成页面、识别 PDF、分析表格——不依赖任何云端。
- 📸 **自动留痕 + 定时提炼** — 工作时按 Enter 自动截当前窗口（存本机私有目录），每隔一段时间用视觉模型提炼「这段时间在做什么」，单次最长 10 小时自动停止。
- 🔒 **本地优先，隐私可控** — 只监听 `127.0.0.1`，不部署服务器、不上云。索引 / 草稿 / 日志全存本机。
- 🧩 **开箱即用的 AI Agent** — 生成 HTML 页面、提炼会议纪要、分析表格、治理知识、协作分发。
- 🔗 **接入飞书，资产可治理** — 以你本人身份授权，读取你有权限的飞书文档 / 知识库 / 多维表格 / 会议纪要，建索引、分类、查重、批量回填摘要标签；写回飞书必须手动确认，绝不自动外发。
- 🎛️ **零配置也能跑** — 没配模型 Key / 飞书 CLI 时自动进入 Mock 模式，UI 全流程可演示。

<div align="center">

| 🧩 **11** | 🎯 **8** | 🤖 **5** | 🔒 **100%** |
|:---:|:---:|:---:|:---:|
| 内置 Agent | 任务场景 | 模型档位 | 数据存本地 |

</div>

---

## 🏢 面向企业：三大能力

企业用户用它把**本地资料、日常工作、飞书资产**串成一条可治理的知识流水线——全程在本机完成：

<p align="center"><img src="docs/assets/pillars.svg" alt="三大能力：本地文件内容生成 · 自动化提炼 · 飞书文档管理" width="100%"></p>

| 能力 | 做什么 | 企业价值 |
|---|---|---|
| 🖥️ **本地文件内容生成** | 本机 Excel / Word / PDF / PPT / 网页 → 企业 HTML 页面、知识卡片、FAQ；PDF 合同识别与金额测算；表格分析出图 | 散落的原始资料就地变成规范成果，全程不上传 |
| 📸 **自动化提炼** | 工作时按 Enter 自动留痕，定时提炼「重点 / 操作 / 会议 / 待办」，单次最长 10 小时自停 | 日报 / 周报素材、工作交接记录自动成文，无感留痕 |
| 🔗 **飞书文档管理** | 盘点可访问的飞书文档 / 知识库 / 多维表格，建索引、分类、查重去旧、批量回填摘要标签 | 团队飞书资产可治理、可复用，写回飞书需人工确认 |

> 三条能力共享同一套本地索引与 AI Agent；只监听 `127.0.0.1`、数据留本机、写回 / 发送需人工确认，适合**企业内网离线分发**。

---

## 🖥️ 界面一览

<p align="center"><img src="docs/assets/screen-dashboard.svg" alt="工作台概览（示例数据）" width="100%"></p>

<p align="center"><sub>工作台概览 —— 授权状态、索引进度、任务场景一览。<b>图中均为示例数据</b></sub></p>

<p align="center"><img src="docs/assets/screen-localdir.svg" alt="本地目录（示例数据）" width="100%"></p>

<p align="center"><sub>本地目录 —— 浏览本机文件（截图 / PDF / Word / PPT），本地解析后直接「识别 / 生成 HTML / 分析」，全程不依赖飞书。<b>示例数据</b></sub></p>

<p align="center"><img src="docs/assets/screen-docs.svg" alt="飞书文档资产库（示例数据）" width="100%"></p>

<p align="center"><sub>飞书文档资产库 —— 授权后按类型 / 空间 / 负责人浏览你可访问的飞书资产，一键「生成 HTML / 分析」。<b>示例数据</b></sub></p>

<p align="center"><img src="docs/assets/screen-summaries.svg" alt="历史总结（示例数据）" width="100%"></p>

<p align="center"><sub>历史总结 —— 按周 / 月 / 年回顾动过的文档，AI 一键生成工作回顾，并按主题统计。<b>示例数据</b></sub></p>

<p align="center"><img src="docs/assets/screen-org.svg" alt="组织架构图谱（虚构示例）" width="100%"></p>

<p align="center"><sub>组织架构图谱 —— 基于通讯录的部门 / 人员关系可视化。<b>图为虚构部门与人数</b></sub></p>

---

## 🧩 内置 Agent

<p align="center"><img src="docs/assets/capabilities.svg" alt="主线 Agent 能力总览" width="100%"></p>

| # | Agent | 做什么 | 输入 → 产出 |
|:--:|---|---|---|
| **A1** | **HTML 页面生成** | 把飞书文档 / 云盘 Word / HTML 套入内置 **Lumen-light** 设计系统，生成企业内部页面。支持「套模板」与「AI 自由版式」。 | 1–3 篇资产 → 可预览 / 下载 / 写回的单文件 HTML |
| **A2** | **文档地图** | 列出你可访问的文档与表格，按类型、归属、重复情况归类。 | 授权范围 → 本地资产索引与目录地图 |
| **A3** | **摘要标签回填** | 为文档批量生成摘要与标签，写入本地索引，也可写回飞书。 | 一批文档 → 摘要 + 标签 |
| **A4** | **知识治理** | 找出重复、过期、缺负责人的文档，并给出处理建议。 | 资产索引 → 治理清单与建议 |
| **A5** | **多维表格分析** | 表格数据归纳并出图（柱状 / 饼 / 折线 / 甘特 / 架构图，可下载）；一次分析全部子表，支持「问数据」自然语言查询。 | 多维表 / 电子表 / Excel → 图表 + 洞察 |
| **A6** | **PDF 识别** | 识别云盘 PDF 全文、字段与表格，扫描页走 OCR，支持逐页识别与合同金额测算。 | 云盘 PDF → 结构化文本 / 字段 / 金额 |
| **A7** | **会议纪要** | 把妙记或会议记录整理成摘要、决策、待办和风险点，待办可写回飞书任务。 | 会议 / 妙记 → 结构化纪要 |
| **A8** | **协作分发** | 生成消息、邮件、任务和日程提醒的草稿，确认后再发送。 | 成果 → 消息 / 邮件 / 任务草稿 |

> 上表是主线 Agent。另有 **📸 自动化提炼** 特性（见下），以及 **AIHot 模型榜** / **AIHot 新闻简报** 两个内容 Agent 和 local-image 支撑能力 —— 合计 11 个。

---

## 🎯 任务场景

任务场景是把几个 Agent 组合起来的预设流程：

| 场景 | 说明 | 用到的 Agent |
|---|---|---|
| **知识库治理** | 盘点文档、分类，过滤重复和缺负责人的内容 | 文档地图 · 知识治理 |
| **内容生成** 🌟 | 从飞书文档或云盘 Word / HTML 生成 HTML 页面、摘要与 FAQ | HTML 页面生成 · 文档地图 |
| **会议沉淀** | 把会议记录整理成摘要、决策、待办和风险 | 会议纪要 |
| **表格分析** | 归纳表格数据并出图；支持全部子表与云盘 Excel | 多维表格分析 |
| **PDF 识别** | 云盘 PDF 的全文、字段、表格识别 | PDF 识别 |
| **协作分发** | 起草消息、邮件、任务和日程提醒 | 协作分发 |
| **自动化提炼** | 按间隔把留痕自动提炼成「重点 / 操作 / 会议 / 待办」 | 本地内容 |
| **AIHot 内容和模型** | 抓取公开站点的 AI 新闻与模型榜，生成简报页 | AIHot 新闻简报 · AIHot 模型榜 |

**「内容生成」这条流水线长这样：**

```mermaid
flowchart LR
    A["选 1-3 篇<br/>飞书资产/上传文件"] --> B["抽取正文/表格<br/>(含内嵌图识别)"]
    B --> C["LLM 套用<br/>Lumen-light 设计系统"]
    C --> D["HTML 草稿<br/>iframe 实时预览"]
    D --> E{写回确认}
    E -->|确认| F["写回飞书<br/>新建文档"]
    E -->|下载| G["本地 HTML 文件"]
```

<p align="center"><img src="docs/assets/screen-html.svg" alt="HTML 页面生成（示例数据）" width="100%"></p>

<p align="center"><sub>「内容生成」：左侧选资产与模板，右侧实时预览 Agent 生成的页面。<b>示例数据</b></sub></p>

---

## 📁 支持的文件类型

工作台区分两类对象 —— **🟢 飞书在线对象**（有内容接口，直接读）与 **🔵 云盘上传文件**（二进制，先下载到本机，再用对应解析器抽取）：

| 类型 | 场景 | 工作方式 |
|---|---|---|
| 🟢 **飞书文档** `docx·doc·wiki` | 内容生成 / 知识治理 | 抽取正文（含内嵌图识别）→ 重组 HTML，或生成摘要 / 标签 |
| 🟢 **多维表格** `bitable` | 表格分析 | 读全部子表 → 列画像 → AI 出图，数字由本地精确聚合 |
| 🟢 **电子表格** `sheet` | 表格分析 | 逐工作表出图，支持「问数据」自然语言查询 |
| 🟢 **演示文稿** `slides` | 内容生成 | 抽取页面文字 → 重组 HTML |
| 🟢 **会议 / 妙记** `meeting` | 会议沉淀 | 整理成摘要、决策、待办、风险 |
| 🔵 **PDF** `.pdf` | PDF 识别 | 逐页抽取正文与表格，扫描件走 OCR → 字段 / 金额测算 |
| 🔵 **Excel** `.xlsx .xls` | 表格分析 | 本地解析每个工作表 → 出图与「问数据」 |
| 🔵 **Word** `.docx` | 内容生成 | 本地解析段落与表格 → 重组 HTML |
| 🔵 **网页** `.html .htm` | 内容生成 | 提取正文 → 重组 HTML |

> 🟢 在线对象 · 🔵 云盘上传。解析全部在本地完成（Excel → openpyxl，Word → python-docx，网页 → BeautifulSoup，PDF → PyMuPDF），不外传。

---

## 📸 自动化提炼

工作期间在任意窗口按 **Enter** 自动留痕当前窗口截图（存本机私有目录，与「内容生成」隔离），每隔一段时间用视觉模型提炼「这段时间在做什么」——重点、操作、会议、待办。**单次会话最长 10 小时自动停止**（停止前做最后一次提炼，避免尾段丢失）。

```mermaid
flowchart LR
    K["按 Enter"] --> C["截当前窗口"]
    C --> P[("私有目录<br/>captures/")]
    T["每 N 分钟"] --> D["视觉模型提炼"]
    P --> D
    D --> J[("digests.jsonl")]
    T -. "满 10 小时" .-> X["自动停止"]
```

<p align="center"><img src="docs/assets/screen-autoextract.svg" alt="自动化提炼（示例数据）" width="100%"></p>

<p align="center"><sub>自动化提炼 —— 提炼频率、开始时刻、10 小时自动停止，以及按窗口提炼出的「重点 / 操作」。<b>示例数据</b></sub></p>

---

## 📬 本地邮箱（只读）

直接跟**桌面上已登录的 Outlook** 说话（COM），不走 Graph、不注册 Entra 应用、不经任何第三方邮件服务——**邮件不出这台机器**。一页里从上到下三块：**智能看板**（按「这封邮件要你做什么动作」分类，点格子筛下面的矩阵）、**信息矩阵**（一行一个会话，只列跟我有关的，群组邮件不进表）、**时间图谱**。最顶上先用一句话说清「几件即将到期 / 几个人在等你回话」。

**「谁在等我回」是本地算的，零模型调用**：拿收件箱的 `ConversationID` 去已发送里找有没有更晚的同会话邮件。不靠「未读」——实测真实收件箱几万封、未读 0，拿未读当信号只会得到空列表。

| | |
|---|---|
| 🔒 **严格只读** | 没有发送 / 回复 / 删除 / 移动 / 改已读的代码路径，一行都没有。这条由 [`backend/tools/check_readonly.py`](backend/tools/check_readonly.py) 走 AST 钉死（不是正则，注释和字符串不参与判定）。唯一豁免：你点一封邮件时在 Outlook 里把它打开。 |
| 📮 **多邮箱可选** | 一个 profile 里挂着委派 / 共享邮箱是常态。可以选读哪一个，**收件箱和已发送一起切**——分开会让「我回过没有」的判断整个反掉。 |
| 🧠 **AI 语义层（默认开，可关）** | 只发**主题 + 正文前 400 字**；**不发附件、不发附件名、不发收件人栏**；按内容哈希缓存，同一封只发一次。页脚每次写明这一趟外发了几批、多少字、给了哪个模型。`OUTLOOK_AI_ENRICH=false` 关掉后页面照常工作——只少三个语义类型（决策 / 业务 / 行政）和矩阵里的三列语义字段，本地维度和时间轴不受影响。 |
| 🛡️ **正文当数据，不当指令** | 邮件正文是不可信输入。它被包在分隔符里、在 system 里申明是数据；模型输出只接受固定枚举，不在白名单里的直接丢弃。 |

<p align="center"><img src="docs/assets/screen-outlook.svg" alt="本地邮箱（示例数据）" width="100%"></p>

<p align="center"><sub>本地邮箱 —— 顶部先说清「几件到期 / 几个人在等你」，中间是按动作分类的智能看板，下面是信息矩阵。页脚如实写明这一趟给模型发了多少、以及关掉它的环境变量。<b>图中人名、主题、数字全为虚构示例</b></sub></p>

## 📝 个人摘记

**默认不开。** 你打开它并选一个间隔（默认 4 小时），它就把这段时间的动静合并成一段话，在飞书里推给**你自己**——不是罗列 16 条流水，是合并成人话。**以你自己的身份发送**——内置应用没有机器人发消息权限，所以飞书里显示为你自己发的。因此用户授权的 **7 天滚动窗口是生效的**：正常用着一直有效，连续七天不碰本应用需要重新登录。（配了有 `im:message.send_as_bot` 权限的自有应用时会改用应用身份，那种情况才不受此限。）内置授权保活会在窗口内主动续期，`needs_refresh` 不会被误判成登录失效。

> ⚠️ 勾选「本地邮箱」作为来源，意味着每次汇总都会**真去读一次 Outlook**，并按你的设置把邮件主题和正文前 400 字发到云端模型——**那一刻没有人在旁边确认**。产品里把这句话原样写在勾选框下面。

<p align="center"><img src="docs/assets/screen-memo.svg" alt="个人摘记（示例数据）" width="100%"></p>

<p align="center"><sub>个人摘记 —— 频率、两个采集来源，以及勾选「本地邮箱」时那条外发警示（产品里原样写着这句，不是 README 补的）。<b>图中数字与时间为虚构示例</b></sub></p>

---

## 🏗️ 工作原理

前端是浏览器里的单页应用，后端是只监听 `127.0.0.1` 的 FastAPI 进程。除了「调飞书 OpenAPI」和「调你配置的模型服务」，其余数据都留在本机。

```mermaid
flowchart TB
    UI["浏览器 · React 前端"] -->|"/api · SSE (仅 127.0.0.1)"| R
    subgraph Backend["本地后端 · FastAPI (127.0.0.1:8787)"]
        R["routes/ 路由"] --> AG["agents/ Agent 框架"]
        AG --> TR["task_runner 异步调度 + SSE"]
        AG --> F["feishu/ (lark-cli 子进程)"]
        AG --> L["llm/ (文本·视觉·生图)"]
        AG --> S["services/ (索引·解析·截图·审计)"]
    end
    F --> FS["飞书 OpenAPI<br/>(你的授权范围)"]
    L --> M["模型服务<br/>(你自己的 Key)"]
    S --> DB[("SQLite + 本地文件<br/>backend/data/")]
```

一次任务：**刷新索引** → **选资产起任务** → **SSE 实时看日志** → **产出草稿预览** → **写回确认（记审计）**。生产打包后，后端用 `StaticFiles` 同源托管前端，单进程即可运行。

📖 更深入的组件、时序图与扩展点见 **[docs/架构与工作原理.md](docs/架构与工作原理.md)**。

---

## 🚀 快速开始

**前置依赖**：Node.js ≥ 18 · Python 3.11 · 一个飞书账号（可选：文本 / 视觉模型 API Key）

### Windows · 双击运行

双击根目录的 **`启动本地Agent.bat`**，无需终端。脚本会自动检测环境、首次安装飞书 CLI、创建 venv、装依赖、起前后端并打开浏览器，首页顶部的「立即授权飞书」引导你完成授权。

### macOS / Linux

```bash
chmod +x scripts/start.sh
scripts/start.sh
```

上手四步：**① 安装启动 → ② 授权飞书 → ③ 刷新索引 → ④ 运行 Agent / 查看产出**

```mermaid
flowchart LR
    s1["① 安装启动"] --> s2["② 授权飞书"] --> s3["③ 刷新索引"] --> s4["④ 运行 Agent"] --> s5["⑤ 查看 / 写回"]
```

---

## ⚙️ 配置

### 飞书 CLI 授权

```bat
scripts\install-lark-cli.cmd
:: 或手动：
npx @larksuite/cli@latest install
lark-cli auth login --recommend
```

授权一次即可，凭据存系统钥匙串，UI 永不显示凭据。若要用你自己的专用飞书应用，在 `backend/.env` 填 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`。

> 🔐 **最小权限原则** —— 登录只请求一份**最小 scope 集**（绝大多数只读；写回 / 发消息 / 建任务需在产品内确认后才触发），不申请多余的写 / 删 / 分享 / 邮箱权限。完整清单与自建应用配置见 **[docs/飞书权限说明.md](docs/飞书权限说明.md)**。

### 模型（`backend/.env`）

```env
# 文本模型 · OpenAI 兼容端点（如阿里百炼 / DashScope）
TEXT_MODEL_PROVIDER=openai_compatible
TEXT_MODEL=qwen-plus
TEXT_MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
TEXT_MODEL_API_KEY=sk-your-key

# 视觉模型 · Azure OpenAI
VISION_MODEL_PROVIDER=azure
VISION_MODEL=your-deployment-name          # Azure 的「部署名」，不是模型名
VISION_MODEL_AZURE_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/
VISION_MODEL_API_KEY=your-azure-key
```

完整字段见 [`backend/.env.example`](backend/.env.example)。**任何 Key 留空 → 对应模型自动进入 Mock，UI 仍可演示；文本与视觉互相独立。**

### 🤖 模型分工（按任务分档，平衡速度 / 成本 / 质量）

| 档位 | 模型（示例） | 用途 |
|---|---|---|
| **默认** | `qwen-plus` | 会议纪要、表格分析、通用解读 |
| **快速** | `qwen-flash` | 批量索引回填、治理复核 |
| **高质量** | `qwen-max` | HTML 重组、合同金额测算 |
| **视觉** | `gpt-4.1-mini` | PDF 逐页识别、扫描件 OCR、内嵌图说明 |
| **生图** | `gpt-image-1` | 表格分析里的架构图 / 关系图 / 概念图 |

---

## 🔀 完整模式 vs Mock 模式

| 依赖 | 完整模式 | Mock 回退 |
|---|---|---|
| `lark-cli`（@larksuite/cli） | 必需，需 `auth login` | 返回示例资产数据 |
| LLM API Key | 必需 | 返回模板 JSON / 占位图 |
| SQLite | 自动创建 | 同 |

Mock 模式下 UI 可完整跑通全流程（含 HTML 生成 + 写回确认 → 模拟返回值），适合离线演示与二次开发。

---

## 🔒 安全与隐私

- **仅监听 `127.0.0.1`**，不暴露公网、不部署服务器。
- 飞书凭据由 `lark-cli` 存于**系统钥匙串**；模型 Key 存本地 `.env`（已 gitignore）。
- 🔐 **飞书最小权限** —— 登录只请求一份最小 scope 集（绝大多数只读；写回 / 发消息 / 建任务需产品内确认），不申请多余的写 / 删 / 分享 / 邮箱权限。完整清单见 **[飞书权限说明](docs/飞书权限说明.md)**。
- **界面永不显示任何 API Key / 密钥。**
- **所有写回飞书的动作必须显式确认**，并记入本地审计日志。
- 本地只存元数据与草稿，不默认持久化完整文档正文。
- 📬 **本地邮箱严格只读**——无发送 / 删除 / 移动 / 改已读代码路径，由仓内 AST 检查脚本钉死。邮件正文**不落盘**，缓存只在内存。
- ⚠️ 模型调用会把必要上下文发到你配置的模型服务——**这是唯一离开本机的业务数据**，请按团队合规评估。其中「本地邮箱」的语义层**默认开启**，每次取数发送主题 + 正文前 400 字（不含附件 / 附件名 / 收件人栏），界面当场写明发送量，`OUTLOOK_AI_ENRICH=false` 可整条关闭。
- 仓库不含任何真实凭据，请填入你自己的飞书应用与模型 Key。

---

## 📦 打包发版

Windows 一键打包为单安装包（PyInstaller 后端 + `npm run build` 前端 + Inno Setup）：

```bat
scripts\bump_version.ps1 X.Y      :: 同步 4 处版本号
build\build_installer.cmd         :: 产出 dist-installer\LocalAgentHub-Setup-X.Y.exe
```

> 需 [Inno Setup 6](https://jrsoftware.org/isdl.php) 与内置 Node 运行时（放在 `build/tools/`，不入库）。生产密钥放 `build/production.env`（已 gitignore）。

---

## 🗂️ 目录结构

```
backend/    Python 3.12+ + FastAPI 本地服务
  app/
    routes/    HTTP / SSE 路由
    agents/    各 AI Agent（import 即注册）
    services/  索引 · 解析 · 截图 · 审计
    feishu/    飞书适配：lark-cli 子进程 + mock
    llm/       文本 / 视觉 / 生图客户端与 prompts
    html/      Lumen-light 设计系统 + 渲染器 + 模板
frontend/   Vite + React + TypeScript（src/pages/ 各页面）
config/     团队共享配置（agents / 模板 / 分类规则）
scripts/    启动与安装脚本（Windows / macOS / Linux）
build/      打包（PyInstaller + Inno Setup）
docs/       架构与工作原理文档
```

**开发**：后端 `pip install -e . && python -m uvicorn app.main:app --reload`（`127.0.0.1:8787`），前端 `npm install && npm run dev`（`127.0.0.1:5173`，已代理 `/api`）。OpenAPI 文档：`127.0.0.1:8787/docs`。

---

## 📄 许可

[MIT License](LICENSE) · 内置 **Lumen-light** 轻量绿色设计系统。欢迎 issue / PR。
