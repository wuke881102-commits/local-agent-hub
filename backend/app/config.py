from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _app_root() -> Path:
    """Install/source root. Frontend dist + bundled runtime live here."""
    if _is_frozen():
        # exe path: <INSTALL>/backend/LocalAgentHub.exe
        return Path(sys.executable).resolve().parent.parent
    # dev: this file = backend/app/config.py -> root is two parents up
    return Path(__file__).resolve().parent.parent.parent


def _data_root() -> Path:
    """Writable runtime data (SQLite, drafts, logs)."""
    if _is_frozen():
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "Feishu Agent Hub"
    return _app_root() / "backend" / "data"


def _env_file() -> Path:
    """Where to read .env from. In frozen mode the install ships one,
    but a per-user override at %LOCALAPPDATA%\\Feishu Agent Hub\\.env wins."""
    if _is_frozen():
        override = _data_root() / ".env"
        if override.exists():
            return override
        return _app_root() / "backend" / ".env"
    return _app_root() / "backend" / ".env"


APP_ROOT = _app_root()
DATA_ROOT = _data_root()
DATA_ROOT.mkdir(parents=True, exist_ok=True)
ENV_FILE = _env_file()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    text_model_provider: str = "openai_compatible"
    text_model: str = "qwen3.7-plus"               # 均衡默认（会议纪要、表格分析、协作分发、问数据）
    # 快省档换成 deepseek-v4-flash：同一份批量分类 prompt 实测 p50 2.69s，
    # 而 qwen3.7-flash 是 8.14s —— 快 3 倍，准确率同为 5/5。这一档是延迟最痛的
    # 地方（outlook_ai 每次打开本地邮箱都跑，40s 硬超时）。
    # 顺带一个反直觉的实测：qwen3.7-flash 比 plus(7.80s) 和 max(7.32s) 都慢，
    # “快省档”名不副实。
    text_model_fast: str = "deepseek-v4-flash"     # 重速度/省钱（批量回填、治理复核、邮件分类）
    # 最强档换成 deepseek-v4-pro。这一档**原本是空的**（aihot.py 里记着：无调用点，
    # 因为上一个 max/preview 档实测会超时）。deepseek-v4-pro 实测 p50 5.98s，比现在的
    # 均衡档还快 —— 超时那个反对理由不成立了，这一档可以重新启用。
    # 但长输出稳定性（HTML 整页生成）尚未测，所以先只换档位、不改调用点。
    text_model_best: str = "deepseek-v4-pro"   # 最强（低频高价值/高风险单次任务）
    text_model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    text_model_api_key: str = ""
    text_model_azure_endpoint: str = ""
    text_model_api_version: str = "2024-12-01-preview"

    # 视觉主档失败时的兜底模型，走**文本端点**（同一个 key）。留空 = 关闭兜底。
    #
    # 为什么需要它：视觉能力现在整条吊在一个内网 Azure 资源上，它一挂，PDF 识别 /
    # 文档配图 / 自动化提炼 / 读图直出 HTML 四个场景**同时**全挂，而且四个都只会
    # 报一句「识别失败」，用户无从判断是自己文件的问题还是服务的问题。
    #
    # 为什么可行：实测 qwen3.7-flash / plus / max 都能读图（PIL 合成的中文审批单
    # 5/5 字段、6 张图一次送 6/6 全认出），而它们和文本共用同一个端点和 key ——
    # 第二条腿本来就在，只是没接上。
    #
    # 为什么默认不拿它当主档：同样实测，单图中文文档 OCR 上 gpt-4.1-mini 是 2.0s，
    # qwen3.7-flash 要 10.4s，而 PDF 逐页识别、文档配图正是单图文档场景。
    # 兜底要的是「还能用」，不是「更快」。
    vision_fallback_model: str = "qwen3.7-flash"
    vision_model_provider: str = "azure"
    vision_model: str = "gpt-4.1-mini"
    vision_model_base_url: str = ""
    vision_model_api_key: str = ""
    vision_model_azure_endpoint: str = "https://YOUR-RESOURCE.openai.azure.com/"
    vision_model_api_version: str = "2024-12-01-preview"

    # 生图模型（GPT-Image-1）：表格分析的「架构 / 关系图」用它出概念图。
    # 任一值为占位/空 → image_generate 进入降级模式（不报错，前端显示占位卡片）。
    # provider=azure 时 image_model 填 Azure 部署名；provider=openai_compatible 时填模型名（gpt-image-1）。
    image_model_provider: str = "azure"
    image_model: str = "gpt-image-1-2025-04-15"   # Azure 部署名（不是模型名）
    image_model_base_url: str = ""
    image_model_api_key: str = ""                 # 留空则自动复用 vision 的 key（同一 Azure 资源）
    image_model_azure_endpoint: str = "https://YOUR-RESOURCE.openai.azure.com/"
    image_model_api_version: str = "2025-04-01-preview"
    image_size: str = "1024x1024"

    # 自动截图的「会议白名单」补充关键词（逗号分隔，小写匹配窗口标题）。
    # 聊天应用（飞书/Teams/微信…）默认整体不截图，但命中会议关键词的窗口会照常截。
    # 若你的会议窗口没被识别到，在「最近跳过的窗口」里看到它的标题后加到这里即可。
    capture_meeting_markers: str = ""

    # ─── 飞书用户授权保活（小时）───
    # 每隔多少小时做一次极轻的用户身份调用，把 7 天滚动窗口往后推。0 = 关。
    # 不能小于 3：access token 有 2 小时寿命，只有它过期时那次调用才会触发
    # 刷新。详细理由见 services/authkeep.py 头注。
    auth_keepalive_hours: int = 12

    # ─── 个人摘记：发送身份 ───
    # auto = 能用机器人就用机器人，否则退回用户身份。取值：auto | bot | user
    #
    # 为什么默认优先机器人：机器人用的是**应用级令牌**（tenant_access_token，由
    # app id/secret 自己换），**永不过期**；而用户身份的 refresh token 是 7 天滚动的 ——
    # 七天内没有产生过 user API 调用就失效。定时任务无人值守，恰恰是最容易撞上这个
    # 的场景（实测就撞上了：连续几天没开应用，摘记直接推送失败）。
    #
    # 代价：飞书里显示为机器人发来的，不是你自己发的；且需要应用开通
    # im:message:send_as_bot（应用身份权限，不在 auth status 的 user scope 列表里）。
    # 强制 bot 但应用没那个权限时会失败并如实报错，不会静默退回。
    selfpush_identity: str = "auto"

    # ─── AIHot 内容和模型（aihot.virxact.com）───
    # 新闻走站点公开 API v1（匿名只读）；模型榜没有 API，靠解析榜单页的 SSR 数据。
    # base_url / leaderboard_path 留了口子：站点改版或自建镜像时不用改代码。
    aihot_base_url: str = "https://aihot.virxact.com"
    aihot_leaderboard_path: str = "/leaderboard"
    aihot_user_agent: str = ""      # 留空 → LocalAgentHub/<版本>（诚实标身份，不伪装浏览器）
    aihot_timeout: int = 30

    # ─── 本地邮箱（底层 Outlook COM，只读）───
    # 只读桌面版 Outlook 的默认邮箱。不发送、不删除、不改标记。
    # window_days: 收件箱回看多少天。scan_limit: 单次最多碰多少封（防御性上限，
    #   正常情况下靠"排序 + 遇到早于窗口就停"提前退出，根本用不到它）。
    # sent_scan_*: 「这个会话我回过没有」要去已发送里翻多少封 / 多少天。
    # body_max: 正文截断长度。实测单封正文能到 28,000 字符，不截断既贵又是不必要的外发。
    # 7 天 = 一个完整工作周。页面**不给选择器**（见 OutlookPage 里的理由），
    # 所以这个值就是产品行为，不是"默认值"。改它要想清楚。
    outlook_window_days: int = 7
    outlook_scan_limit: int = 400
    outlook_sent_scan_days: int = 180
    outlook_sent_scan_limit: int = 800
    outlook_body_max: int = 4000
    # 墙钟预算（秒）。比条数上限更重要：在**非缓存（在线模式）**的邮箱上，
    # 每读一封的属性都是一次网络往返，实测约 300ms/封 —— 400 封就是 2 分钟，
    # 请求直接挂死。条数上限拦不住这种情况（问题在单封耗时，不在条数）。
    # 到点就带着已取到的部分返回，并在响应里标 partial=True。
    outlook_time_budget_s: int = 10
    # 取数结果的内存缓存时长。**只在内存，不落盘** —— 缓存的是真实邮件的主题和
    # 正文，落盘等于在磁盘上多存一份邮件副本。进程退出即消失，这是刻意的取舍。
    outlook_cache_ttl_s: int = 300
    # 单次请求的硬超时（秒）。**必需，不是保险**：实测 Outlook 的某些属性访问会
    # 无限期阻塞，而且哪一个会阻塞不稳定（同一句昨天正常、今天挂死）。同线程里
    # 没法给 COM 调用加超时，只能在请求这一侧到点放弃并给用户一个说得清的错误。
    outlook_request_timeout_s: int = 45
    # 跳过「发件人」相关的全部属性读取。**默认关闭。**
    #
    # 为什么需要这个开关：某些邮箱上，任何跟发件人有关的读取都会**无限期阻塞** ——
    # SenderName、SenderEmailAddress，连原始属性 PR_SENDER_NAME 也一样（在全新重启的
    # Outlook 上干净复测确认过）。而卡住的是 **Outlook 本身**、不只是我们的线程：
    # 一旦发出这个调用，Outlook 就不再响应任何后续请求，整个功能瘫掉。
    # 同线程内没法给 COM 调用加超时，所以唯一的办法是**根本不发它**。
    #
    # 实测这类邮箱的共同点：在线模式（无本地缓存）+ profile 指向的邮箱只支持枚举
    # （主题/时间/会话 ID/收件人栏/正文/附件都正常，0.2~0.8 秒）而不支持发件人解析。
    # 打开这个开关后，列表里没有发件人名字，其余（三分类、回复判定、截止线索、附件）
    # 全部照常工作 —— 残缺但有用，好过整页超时。
    outlook_skip_sender: bool = False
    # 「你答应过但还没做」——从**我自己的已发送邮件**里抽承诺。这是这个面板相对
    # Outlook 唯一无法被替代的能力（Outlook 完全没有对应功能），所以默认开。
    # 代价：要读已发送邮件的正文。在线模式下每封是一次网络往返，会吃掉全部预算，
    # 所以 _inbox_sync 里检测到非缓存邮箱会自动关掉它（少一栏好过整页超时）。
    # promise_cap 同时限制抽到的条数和读正文的封数（上限 = cap * 4）。
    outlook_promise_scan: bool = True
    outlook_promise_cap: int = 40
    # ─── AI 语义层（**会把邮件内容发到云端模型**）───
    # 用户明确选择了「全量自动分析」：每次取数把 7 天的邮件主题 + 正文前 400 字
    # 批量发到 text_model_fast 做分类和字段抽取。关掉它页面照常工作，只少三个语义
    # 类型（决策 / 业务 / 行政）和矩阵里的三列语义字段。
    # 暴露面控制见 services/outlook_ai.py 头注：不发附件、不发附件名、不发收件人栏、
    # 按内容哈希缓存避免重复外发、用 fast 档不用最强档。
    outlook_ai_enrich: bool = True
    outlook_ai_timeout_s: int = 40
    # 不进「信息矩阵 / 时间图谱」的类型（逗号分隔）。那两个视图是工作台，给
    # 「跟我有关、我可能要动手」的事用；广告、订阅资讯、工单状态流水混进去只会
    # 稀释信号。它们照常留在智能看板里 ——「这周有 20 封广告」本身是有用的信息。
    # **系统告警（alert）刻意不在默认名单里**：磁盘满、证书过期是真要处置的。
    # 哪些算噪音因人而异，改这个环境变量就能调，不用改代码。
    outlook_skip_kinds: str = "bulk,newsletter,ticket"

    # ─── 语音速记（麦克风 / 系统回环 → 云端实时转写）───
    # **会把语音发到云端。** 麦克风那一路发出去的是你自己的声音；系统声音（回环）
    # 那一路发出去的是会议里**其他人**的声音 —— 后者是用户明确选择要的能力，
    # 不是默认开启的副作用：录哪一路每次开录时在页面上选，没有"记住上次"。
    #
    # 音频本身**没有任何一条代码路径会落盘**（连可选项都不提供），只有转写出来的
    # 文字存到 DATA_ROOT/voice/notes.jsonl。和 outlook_cache_ttl_s 那条注记同一个
    # 取舍：内容只在内存里过一遍，磁盘上不留副本。
    audio_model: str = "qwen-audio-3.0-realtime-flash"
    # realtime 是 websocket，**不走** text_model_base_url 那个 /compatible-mode/v1
    # REST 端点，所以单列一个地址、不复用。
    audio_model_ws_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    audio_model_api_key: str = ""      # 留空则复用 text_model_api_key（同一个 dashscope key）
    # 静音判定阈值（PCM16 的 RMS，满量程 32768）。低于它算静音：**静音期一个字节
    # 都不往云端发**，也用它决定什么时候提交一段。
    # 200 是个保守起点：太高会把小声说话判成静音、句子被切断；太低则环境噪声让它
    # 永远提交不了。真录起来不合适就调这个，不用改代码。
    voice_silence_rms: int = 200
    # 连续静音多久算"这句说完了"，然后提交。900 ms 接近服务端 VAD 自己的
    # 800 ms（我们把服务端 VAD 关了，见 services/voice_asr.py 头注）。
    voice_commit_silence_ms: int = 900
    # 单次录音的硬上限（分钟）。**必需，不是保险**：录音是后台线程在跑，浏览器
    # 标签页关了它还在录。没有这个上限，一次忘记停止就是无声无息录一整天。
    voice_max_minutes: int = 90
    # ─── 语音速记：传文件进来转写 ───
    # 上传的字节**全程只在内存里**（接口收原始请求体，不用 FastAPI 的 UploadFile ——
    # 后者超过 1 MB 就落到系统临时目录）。所以这两个上限同时也是内存上限。
    #
    # 200 MB 差不多是一小时的 m4a 会议录音再乘几倍的余量。
    voice_file_max_mb: int = 200
    # 时长上限。**不是性能考虑，是防误操作**：串行转写约 2.6 倍实时（实测），
    # 一个三小时的文件就是一小时的模型调用，而误传一个大文件太容易了。
    voice_file_max_minutes: int = 180
    # 提炼（转写完把逐字稿送给文字模型）**每一档**的超时。
    #
    # 90 而不是更长，是实测定的。用户那条失败记录（235 字）复现出来的现象是：
    # qwen3.7-plus **整整挂了 300 秒**才超时；而同一段内容隔一会儿再打，
    # 33 秒就出结果，连打两次都正常。也就是说那是一次**瞬时卡死**，
    # 不是"内容长所以慢"。
    #
    # 对这种故障，把超时调大是错的方向 —— 只会让人干等五分钟然后什么都没有。
    # 正确做法是短超时 + 退到快档：实测同一段内容 deepseek-v4-flash 只要 7.2 秒
    # （主档 33 秒）。所以最坏情况是 90 秒等不到 + 快档几秒出结果，
    # 而不是 300 秒之后一场空。
    voice_distill_timeout_s: int = 90

    lark_cli_bin: str = "lark-cli"
    enable_mock_fallback: bool = True

    # 打包内置的专用飞书应用凭据。两者都非空时，首启走非交互 config init（跳过"创建应用"）。
    # 留空 → 退回交互式 `config init --new`（开发态默认）。
    feishu_app_id: str = ""
    feishu_app_secret: str = ""

    app_host: str = "127.0.0.1"
    app_port: int = 8787
    local_index_db: str = ""
    draft_dir: str = ""
    log_dir: str = ""
    config_dir: str = ""
    frontend_dev_origin: str = "http://127.0.0.1:5173"

    @property
    def db_path(self) -> Path:
        if self.local_index_db:
            return Path(self.local_index_db).resolve()
        return DATA_ROOT / "index.sqlite"

    @property
    def draft_path(self) -> Path:
        if self.draft_dir:
            return Path(self.draft_dir).resolve()
        return DATA_ROOT / "drafts"

    @property
    def log_path(self) -> Path:
        if self.log_dir:
            return Path(self.log_dir).resolve()
        return DATA_ROOT / "logs"

    @property
    def captures_path(self) -> Path:
        """「自动化提炼」专用的截图私有目录。

        独立于用户在「本地目录 / 内容生成」里浏览的目录——按 Enter 自动留痕的截图
        只落在这里，不会出现在任何内容生成的文件选择器中。
        """
        return DATA_ROOT / "captures"

    @property
    def config_path(self) -> Path:
        if self.config_dir:
            return Path(self.config_dir).resolve()
        return APP_ROOT / "config"

    @property
    def frontend_dist(self) -> Path:
        return APP_ROOT / "frontend" / "dist"


# In the packaged app the bundled .env is authoritative for the dedicated Feishu
# app credentials. pydantic-settings ranks real environment variables ABOVE the
# .env file, so a stray inherited var (e.g. a leftover User-scope
# FEISHU_APP_ID=cli_xxx from earlier dev/testing) would silently override the
# bundled value and bind the app to the WRONG Feishu app (wrong bot on the consent
# page). Drop those vars before Settings() reads them — frozen mode only, so dev
# overrides still work. Case-insensitive to match Windows env semantics.
if _is_frozen():
    for _k in [k for k in os.environ if k.upper() in ("FEISHU_APP_ID", "FEISHU_APP_SECRET")]:
        os.environ.pop(_k, None)

settings = Settings()
settings.draft_path.mkdir(parents=True, exist_ok=True)
settings.log_path.mkdir(parents=True, exist_ok=True)
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
