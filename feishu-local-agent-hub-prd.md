---
name: 本地 Agent 工作台
version: 0.1
language: zh-CN
deployment: local-web
distribution: team-shared-config-package
design_system: Lumen-light
default_text_model: qwen3.7-plus
default_vision_model: gpt-4.1-mini
status: draft
---

# 本地 Agent 工作台 · PRD

## 1. 产品概述

### 1.1 产品定位

本地 Agent 工作台是一个面向企业内部用户的本地 Web 应用。用户在自己的电脑上启动系统，通过个人飞书授权调用本机飞书 CLI 能力，让不同 Agent 处理用户已有权限访问的飞书资产，包括文档、知识库、多维表格、会议纪要、任务、消息等。

产品不提供企业统一 SaaS 后台，不要求企业统一运维。团队通过共享配置包分发统一的 Agent、模型配置、HTML 模板、分类规则和样式规范；每个用户在本地运行，数据和授权均保留在本机。

### 1.2 核心价值

- 把用户可访问的飞书内容整理成可检索、可分类、可治理的本地知识资产。
- 把飞书 CLI 的文档、多维表格、会议、任务、消息等能力包装成企业用户能直接理解的任务场景。
- 通过内置 Agent 完成文档盘点、知识治理、HTML 页面生成、会议纪要沉淀、多维表格分析、协作分发。
- 所有写回飞书的动作都必须经过用户确认，避免误写、误发和权限风险。
- 生成的 HTML 页面严格遵循 `Lumen-light.md` 的浅色绿色企业设计系统。

## 2. 目标用户与使用场景

### 2.1 目标用户

| 用户类型 | 典型诉求 | 使用频率 |
|---|---|---|
| 产品经理 | 从 PRD、项目文档、会议纪要中整理项目页面和行动项 | 高频 |
| 项目经理 | 盘点项目资料、跟进会议决策、分发任务提醒 | 高频 |
| 知识库运营 | 整理团队 Wiki、发现过期/重复/缺 owner 文档 | 高频 |
| 业务运营 | 把活动方案、公告、素材整理成页面并分发 | 中高频 |
| 数据/业务分析人员 | 分析多维表格、电子表格，提炼异常和报表建议 | 中频 |
| 普通员工 | 搜索、整理、摘要自己有权限访问的飞书内容 | 中频 |

### 2.2 核心使用场景

1. 用户启动本地 Web 应用。
2. 系统读取团队共享配置包中的 `.env`、Agent 配置、HTML 模板和分类规则。
3. 用户完成个人飞书授权。
4. 系统通过飞书 CLI 扫描用户已有权限访问的飞书资产，建立本地元数据索引。
5. 用户从首页选择任务场景，例如“整理知识库”“生成 HTML 页面”“分析多维表格”“沉淀会议纪要”。
6. 系统匹配对应 Agent，按需读取飞书内容并调用模型处理。
7. 用户在本地预览结果。
8. 如需创建文档、更新文档、发送消息、创建任务，系统展示写回内容和影响范围。
9. 用户确认后，系统通过飞书 CLI 写回飞书。

## 3. 产品边界

### 3.1 已确认范围

- 产品形态：本地 Web 应用。
- 交付方式：团队共享配置包，每个用户本地部署和运行。
- 飞书授权：用户个人授权，只处理该用户已有权限访问的内容。
- 首页入口：以任务场景为入口，而不是以 Agent 名称为入口。
- Agent 形态：系统内置 6 个 Agent，用户可配置参数。
- 数据策略：本地元数据索引，不默认持久化完整正文。
- 写回策略：所有写回飞书动作必须用户确认。
- 模型策略：通过 `.env` 预置，普通用户无需在界面填写 API Key。
- 文本分析模型：`qwen3.7-plus`。
- 图像理解模型：`gpt-4.1-mini`。
- 设计系统：遵循 `Lumen-light.md`。
- 系统语言：中文。

### 3.2 非目标范围

- 不做企业统一 SaaS 平台。
- 不做企业级统一管理员后台。
- 不支持普通用户在 UI 中配置模型 API Key。
- 不允许 Agent 在未确认的情况下自动写回飞书。
- 不在首版提供完全自由的 Agent Builder。
- 不默认持久化完整文档正文和附件全文解析结果。
- 不在首版提供多用户共享数据库或中心化索引服务。
- 不在首版提供英文系统界面。

## 4. 信息架构

### 4.1 首页任务入口

首页按用户要完成的工作组织，而不是按底层 Agent 或飞书能力组织。

| 一级入口 | 主要任务 | 背后 Agent |
|---|---|---|
| 知识库治理 | 文档盘点、分类、过期/重复/缺 owner 检查 | 文档地图 Agent、知识治理 Agent |
| 内容生产 | 文档生成 HTML、摘要、FAQ、知识卡片 | HTML 页面生成 Agent、文档地图 Agent |
| 会议沉淀 | 会议纪要整理、决策提取、行动项生成 | 会议纪要 Agent、协作分发 Agent |
| 表格分析 | 多维表格/电子表格摘要、异常识别、报表建议 | 多维表格分析 Agent |
| 协作分发 | 生成消息、邮件、任务提醒草稿 | 协作分发 Agent |
| 本地配置 | 授权状态、索引刷新、Agent 参数、系统诊断 | 配置模块 |

### 4.2 页面结构

- **首页仪表盘**
  - 授权状态
  - 本地索引状态
  - 最近任务
  - 推荐任务场景
  - Agent 可用状态

- **任务运行页**
  - 场景说明
  - 输入选择
  - 参数配置
  - 运行进度
  - 结果预览
  - 写回确认

- **资产浏览页**
  - 文档
  - 知识库
  - 多维表格
  - 会议纪要
  - 任务
  - 消息/分发记录

- **Agent 配置页**
  - 启用/停用
  - 默认扫描范围
  - 分类规则
  - HTML 模板
  - 写回位置
  - 通知目标
  - 审批/确认规则

- **系统诊断页**
  - 飞书 CLI 可用性
  - 飞书授权状态
  - 模型连通性
  - 本地索引健康度
  - 最近失败任务
  - `.env` 配置摘要，不显示密钥

## 5. 内置 Agent 定义

### 5.1 Agent 通用规范

每个 Agent 必须定义以下字段：

```yaml
agent:
  id: string
  name: string
  description: string
  task_entry: string[]
  input_sources: string[]
  feishu_cli_domains: string[]
  default_model: string
  output_types: string[]
  writeback_allowed: boolean
  writeback_requires_confirmation: true
  configurable_params: string[]
  local_logs: true
```

### 5.2 文档地图 Agent

```yaml
agent:
  id: document-map-agent
  name: 文档地图 Agent
  task_entry:
    - 知识库治理
    - 内容生产
  input_sources:
    - 飞书文档
    - 飞书知识库
    - 文件夹/空间
  feishu_cli_domains:
    - Docs
    - Drive
    - Wiki
  default_model: qwen3.7-plus
  output_types:
    - 文档清单
    - 分类树
    - 标签
    - owner 信息
    - 更新时间
    - 内容摘要
  writeback_allowed: false
```

#### 主要能力

- 扫描用户可访问的飞书文档和知识库。
- 建立本地元数据索引。
- 根据标题、路径、摘要、owner、更新时间自动分类。
- 识别文档类型，例如 PRD、会议纪要、项目计划、方案、周报、公告、数据说明。
- 为后续 Agent 提供候选文档集合。

#### 可配置项

- 默认扫描范围。
- 分类体系。
- 标签规则。
- 忽略路径。
- 文档更新时间阈值。
- 是否启动时自动刷新索引。

### 5.3 知识治理 Agent

```yaml
agent:
  id: knowledge-governance-agent
  name: 知识治理 Agent
  task_entry:
    - 知识库治理
  input_sources:
    - 本地元数据索引
    - 飞书文档
    - 飞书知识库
  feishu_cli_domains:
    - Docs
    - Wiki
    - Drive
  default_model: qwen3.7-plus
  output_types:
    - 治理问题列表
    - 优先级
    - 修复建议
    - 合并建议
    - 权限风险提示
  writeback_allowed: true
  writeback_requires_confirmation: true
```

#### 主要能力

- 发现长期未更新文档。
- 发现疑似重复文档。
- 发现缺少 owner、标题不规范、结构混乱的文档。
- 提示可能权限过宽的文档，但不自动变更权限。
- 生成治理建议，可确认后写回飞书文档或创建治理任务。

#### 可配置项

- 过期判定天数。
- 重复判定相似度。
- owner 规则。
- 分类命名规范。
- 治理结果写回位置。

### 5.4 HTML 页面生成 Agent

```yaml
agent:
  id: html-page-agent
  name: HTML 页面生成 Agent
  task_entry:
    - 内容生产
  input_sources:
    - 飞书文档
    - 飞书知识库
    - 多维表格摘要
    - 会议纪要摘要
  feishu_cli_domains:
    - Docs
    - Wiki
    - Drive
    - Markdown
  default_model: qwen3.7-plus
  vision_model: gpt-4.1-mini
  output_types:
    - HTML 页面
    - 本地预览
    - 来源引用清单
    - 生成说明
  writeback_allowed: true
  writeback_requires_confirmation: true
```

#### 主要能力

- 用户选择一个或多个飞书文档。
- Agent 抽取文档结构、关键信息、引用来源和图片说明。
- Agent 判断页面类型，并选择对应 HTML 模板。
- 生成完整可预览 HTML 页面。
- 用户确认后，可导出 HTML 或写回飞书文档。

#### 首版模板

| 模板 | 用途 | 典型内容 |
|---|---|---|
| 内部知识页 | 专题 Wiki、制度说明、FAQ、知识导航 | 摘要、目录、标签、FAQ、相关文档 |
| 项目/产品展示页 | 项目介绍、产品说明、阶段汇报 | 背景、目标、里程碑、指标、风险、链接 |
| 运营活动/公告页 | 活动方案、公告、通知、报名页 | 时间、对象、流程、行动按钮、联系人 |

#### 可配置项

- 默认模板。
- 允许使用的模板集合。
- 页面标题规则。
- 来源引用展示方式。
- 写回位置。
- 是否允许导出本地 HTML。

## 6. HTML 生成规范

### 6.1 强制设计系统

HTML 页面生成 Agent 的输出必须严格遵循 `Lumen-light.md`。生成结果不是自由风格网页，而是使用统一设计系统的企业内部页面。

生成页面必须满足：

- 使用浅色企业绿色风格。
- 主品牌色为 `#00AA4F`。
- 所有颜色、间距、圆角、字体、阴影必须使用 CSS 变量。
- 组件必须复用设计系统中的按钮、卡片、徽章、表格、标签页、表单、提示组件、仪表盘布局。
- 不允许使用胶囊按钮。
- 不允许使用原生 `select` 下拉。
- 所有交互元素必须有 hover 和 focus 状态。
- 页面语言首版为中文。
- 页面必须包含来源文档引用区域。
- 页面必须包含生成时间和生成说明。

### 6.2 HTML 输出形态

首版输出为单文件 HTML，便于预览、导出和写回。

```yaml
html_output:
  format: single-file-html
  required_sections:
    - meta
    - css_tokens
    - layout
    - content
    - source_references
    - generation_note
  assets:
    mode: embedded-or-linked
  language: zh-CN
```

### 6.3 CSS Token 基线

生成 HTML 必须内置 `:root` token。以下为最低要求，完整值以 `Lumen-light.md` 为准。

```css
:root {
  --brand-50: #E3F5EA;
  --brand-100: #C0E6D0;
  --brand-500: #00AA4F;
  --brand-600: #008E43;
  --brand-700: #006845;

  --success: #10B050;
  --warning: #F0A800;
  --error: #C83A3A;
  --info: #0095D4;

  --surface-page: #F9FAFC;
  --surface-elevated: #FFFFFF;
  --surface-subtle: #F0F1F3;
  --surface-hover: #EDEEF0;

  --border-default: #DDE3EA;
  --border-subtle: #E8ECF0;
  --border-focus: #00AA4F;

  --text-primary: #42464A;
  --text-secondary: #555B61;
  --text-tertiary: #737A82;
  --text-brand: #00AA4F;
  --text-inverse: #FFFFFF;

  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  --font-mono: 'SF Mono', 'Menlo', 'Monaco', 'Consolas', 'JetBrains Mono', monospace;

  --text-2xl: 26px;
  --text-xl: 20px;
  --text-lg: 16px;
  --text-base: 14px;
  --text-sm: 13px;
  --text-xs: 11px;

  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;
  --space-10: 40px;

  --radius-md: 6px;
  --radius-lg: 8px;
  --radius-xl: 12px;
  --radius-2xl: 16px;

  --shadow-sm: 0 1px 3px rgba(0, 0, 0, 0.08), 0 0 0 1px rgba(0, 0, 0, 0.03);
  --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.1), 0 0 0 1px rgba(0, 0, 0, 0.04);
}
```

### 6.4 组件约束

#### 按钮

```css
.btn {
  border-radius: var(--radius-lg);
  padding: var(--space-2) var(--space-5);
  transition: 200ms cubic-bezier(0, 0, 0.2, 1);
}

.btn-primary {
  background: var(--brand-500);
  color: var(--text-inverse);
}

.btn-primary:hover {
  background: var(--brand-600);
  box-shadow: var(--shadow-md);
  transform: translateY(-1px);
}
```

#### 卡片

```css
.card {
  background: var(--surface-elevated);
  border-radius: var(--radius-2xl);
  padding: var(--space-6);
  box-shadow: var(--shadow-sm);
}

.card:hover {
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}
```

#### 表格

```css
.table th {
  background: var(--surface-subtle);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.table td {
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}
```

### 6.5 HTML Agent 生成流程

1. 读取用户选择的飞书文档。
2. 提取标题、摘要、章节、重点、表格、图片、引用来源。
3. 如存在图片、截图、流程图，调用 `gpt-4.1-mini` 生成图片说明。
4. 调用 `qwen3.7-plus` 进行内容重组。
5. 判断页面类型：内部知识页、项目/产品展示页、运营活动/公告页。
6. 套用对应 Lumen-light 模板。
7. 生成单文件 HTML。
8. 本地预览。
9. 用户确认后导出或写回飞书。

## 7. 会议纪要 Agent

```yaml
agent:
  id: meeting-minutes-agent
  name: 会议纪要 Agent
  task_entry:
    - 会议沉淀
  input_sources:
    - 会议纪要文档
    - 会议相关文档
    - 日程信息
  feishu_cli_domains:
    - Meetings
    - Docs
    - Calendar
    - Tasks
  default_model: qwen3.7-plus
  output_types:
    - 会议摘要
    - 决策列表
    - 行动项
    - 风险与阻塞
    - 跟进任务草稿
  writeback_allowed: true
  writeback_requires_confirmation: true
```

### 主要能力

- 从会议纪要中提炼背景、结论、决策、行动项。
- 识别负责人、截止时间、依赖事项。
- 生成任务草稿，用户确认后创建飞书任务。
- 可将整理结果写回原会议纪要或新建总结文档。

### 可配置项

- 行动项格式。
- 默认任务列表。
- 默认提醒时间。
- 写回文档位置。
- 是否发送会议总结消息。

## 8. 多维表格分析 Agent

```yaml
agent:
  id: base-analysis-agent
  name: 多维表格分析 Agent
  task_entry:
    - 表格分析
  input_sources:
    - 飞书多维表格
    - 飞书电子表格
  feishu_cli_domains:
    - Base
    - Sheets
  default_model: qwen3.7-plus
  output_types:
    - 表结构说明
    - 数据摘要
    - 异常识别
    - 报表建议
    - 字段解释
  writeback_allowed: true
  writeback_requires_confirmation: true
```

### 主要能力

- 读取表结构、字段、视图和样例记录。
- 生成字段说明和业务含义。
- 识别异常值、空值、重复记录、状态分布异常。
- 生成适合业务用户阅读的数据摘要。
- 生成报表和看板建议。

### 可配置项

- 默认分析表。
- 样本记录数量。
- 异常判断规则。
- 输出摘要模板。
- 是否允许写回分析文档。

## 9. 协作分发 Agent

```yaml
agent:
  id: collaboration-dispatch-agent
  name: 协作分发 Agent
  task_entry:
    - 协作分发
  input_sources:
    - Agent 结果
    - HTML 页面
    - 会议行动项
    - 知识治理建议
  feishu_cli_domains:
    - Messenger
    - Mail
    - Tasks
    - Calendar
  default_model: qwen3.7-plus
  output_types:
    - 消息草稿
    - 邮件草稿
    - 任务草稿
    - 日程提醒草稿
  writeback_allowed: true
  writeback_requires_confirmation: true
```

### 主要能力

- 将 Agent 结果改写成适合群聊、邮件、任务、日程提醒的内容。
- 根据结果类型推荐分发方式。
- 生成消息草稿，用户确认后发送。
- 生成任务草稿，用户确认后创建任务。
- 生成后续跟进提醒，用户确认后创建日程。

### 可配置项

- 默认通知群。
- 默认邮件收件人。
- 默认任务列表。
- 默认消息模板。
- 是否允许批量分发。

## 10. 本地索引与数据策略

### 10.1 本地存储范围

首版保存本地元数据索引，不默认持久化完整正文。

```yaml
local_index:
  stores:
    - asset_id
    - asset_type
    - title
    - url
    - owner
    - created_time
    - updated_time
    - source_space
    - path
    - tags
    - category
    - summary
    - last_processed_at
    - last_task_status
  does_not_store_by_default:
    - full_document_body
    - full_attachment_content
    - full_image_ocr_result
```

### 10.2 刷新策略

- 用户可手动刷新索引。
- 系统可在启动时提示刷新。
- 可配置定时刷新。
- 当飞书侧权限变化时，刷新后同步更新本地可见资产。
- 任务运行时如发现文档不可访问，提示用户重新授权或刷新索引。

### 10.3 任务结果存储

允许本地保存：

- Agent 运行记录。
- 生成摘要。
- HTML 草稿。
- 表格分析结果。
- 会议行动项草稿。
- 写回前后的操作日志。

## 11. 模型调用策略

### 11.1 配置方式

模型配置通过 `.env` 预置在团队共享配置包中。普通用户不需要在 UI 中填写 API Key。

```env
TEXT_MODEL=qwen3.7-plus
VISION_MODEL=gpt-4.1-mini
LLM_BASE_URL=...
LLM_API_KEY=...
OPENAI_BASE_URL=...
OPENAI_API_KEY=...
```

### 11.2 模型路由

| 任务类型 | 默认模型 |
|---|---|
| 文档摘要 | qwen3.7-plus |
| 文档分类 | qwen3.7-plus |
| 知识治理建议 | qwen3.7-plus |
| HTML 页面内容生成 | qwen3.7-plus |
| 会议纪要整理 | qwen3.7-plus |
| 多维表格分析 | qwen3.7-plus |
| 消息/邮件/任务草稿 | qwen3.7-plus |
| 图片、截图、流程图理解 | gpt-4.1-mini |

### 11.3 诊断要求

系统诊断页只展示：

- 当前文本模型名称。
- 当前图像模型名称。
- 模型服务连通性。
- 最近失败原因。

系统不得在 UI 中展示 API Key。

## 12. 写回与确认机制

### 12.1 写回原则

所有写回飞书的动作都必须由用户确认。

包括但不限于：

- 创建飞书文档。
- 更新飞书文档。
- 写入知识治理建议。
- 创建任务。
- 发送群消息。
- 发送邮件。
- 创建日程提醒。
- 写回多维表格分析结果。

### 12.2 确认页要求

确认页必须展示：

- 操作类型。
- 目标位置。
- 目标对象。
- 将写入/发送的完整内容。
- 涉及人员或群组。
- 是否可撤销。
- 相关来源文档。

用户点击确认前，系统不得执行写回。

### 12.3 本地审计日志

本地记录：

- 操作时间。
- Agent 名称。
- 输入来源。
- 飞书 CLI 动作类型。
- 写回目标。
- 用户确认状态。
- 执行结果。
- 错误信息。

## 13. 部署与交付

### 13.1 交付包内容

```yaml
delivery_package:
  includes:
    - 本地 Web 应用
    - 本地后端服务
    - .env
    - Agent 配置
    - HTML 模板
    - 分类规则
    - Lumen-light 设计系统文件
    - 启动脚本
    - 使用说明
```

### 13.2 本地架构

```text
浏览器 UI
  ↓
本地 Web 后端
  ↓
Agent 编排器
  ├─ 飞书 CLI Adapter
  ├─ 模型调用 Client
  ├─ 本地元数据索引
  ├─ HTML Renderer
  └─ 本地审计日志
```

### 13.3 启动流程

1. 用户解压或安装团队共享配置包。
2. 用户启动本地服务。
3. 浏览器打开本地地址。
4. 系统检测 `.env`、飞书 CLI、模型连通性。
5. 用户完成飞书授权。
6. 用户刷新本地索引。
7. 用户开始运行任务。

## 14. UI 与视觉规范

### 14.1 总体风格

系统 UI 遵循 `Lumen-light.md`：

- 企业级浅色界面。
- 主品牌色 `#00AA4F`。
- 背景色 `#F9FAFC`。
- 白色容器。
- 绿色强调色。
- 几何圆角。
- 清晰的仪表盘布局。
- 中文界面。

### 14.2 组件规范

| 组件 | 要求 |
|---|---|
| 按钮 | 6 种变体，3 种尺寸，不使用胶囊样式 |
| 卡片 | 白底、16px 圆角、轻阴影、hover 上浮 |
| 徽章 | mono 字体、小字号、语义颜色 |
| 表单 | 自定义输入和下拉，不使用原生 select |
| 表格 | mono 表头、hover 行高亮 |
| 标签页 | active 状态使用品牌色和上圆角 |
| 仪表盘 | 侧边栏 + 主内容区 grid 布局 |
| 提示 | success、warning、error、info 四类语义 |

## 15. MVP 范围

### 15.1 必须交付

- 本地 Web 应用启动与访问。
- `.env` 预置模型配置。
- 飞书个人授权。
- 飞书 CLI 可用性检测。
- 本地元数据索引。
- 首页任务场景入口。
- 6 个内置 Agent。
- HTML 页面生成 Agent 的 3 类模板。
- Lumen-light HTML 生成规范。
- 本地预览。
- 写回前确认。
- 本地操作日志。
- 系统诊断页。

### 15.2 可延后

- 自定义 Agent Builder。
- 企业统一后台。
- 多用户共享索引。
- 完整全文/向量索引。
- 英文界面。
- 自动批量写回。
- 复杂审批流。
- 插件市场。

## 16. 成功指标

| 指标 | MVP 目标 |
|---|---|
| 首次启动成功率 | 90% 以上 |
| 飞书授权成功率 | 90% 以上 |
| 索引刷新成功率 | 85% 以上 |
| HTML 页面生成可用率 | 80% 以上 |
| 写回前确认覆盖率 | 100% |
| 用户首次完成任务时间 | 10 分钟以内 |
| 生成页面符合 Lumen-light 规范 | 90% 以上 |

## 17. 风险与待确认项

### 17.1 风险

- 飞书 CLI 实际能力和权限范围可能与设计假设不同。
- 用户可访问文档过多时，元数据索引刷新可能较慢。
- `.env` 内置密钥虽然方便，但本地用户可读取，需要明确交付安全边界。
- 模型调用会发送必要上下文到模型服务，需要在使用说明中明确提示。
- HTML 生成质量依赖模板约束和模型稳定性。
- 飞书写回接口失败时，需要保留本地草稿和重试能力。

### 17.2 待确认项

- 飞书 CLI 的实际命令清单和认证方式。
- 多维表格、会议、任务、消息等能力在目标环境中的可用范围。
- 团队共享配置包由谁维护和更新。
- HTML 生成后是优先写回飞书文档，还是优先导出本地 HTML。
- 是否需要对敏感文档路径或关键词做默认排除。
- 本地索引使用 SQLite、文件型 JSON，还是轻量搜索库。

## 18. 版本规划

### V0.1 MVP

- 本地 Web 应用。
- 个人飞书授权。
- 本地元数据索引。
- 6 个内置 Agent。
- Lumen-light HTML 页面生成。
- 用户确认后写回。

### V0.2

- 增强索引能力。
- 支持更多 HTML 模板。
- 增加任务批处理。
- 增加敏感内容识别。
- 增加配置包版本管理。

### V0.3

- 支持自定义 Agent Builder。
- 支持更细粒度的工具权限控制。
- 支持可选全文/向量索引。
- 支持团队共享模板仓库。

