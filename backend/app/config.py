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
        # exe path: <INSTALL>/backend/feishu-agent.exe
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
    text_model_fast: str = "qwen3.6-flash"         # 重速度/省钱（批量回填、治理复核）
    text_model_best: str = "qwen3.7-max"   # 最强（HTML 生成、合同金额测算等低频高价值/高风险单次任务）
    text_model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    text_model_api_key: str = ""
    text_model_azure_endpoint: str = ""
    text_model_api_version: str = "2024-12-01-preview"

    vision_model_provider: str = "azure"
    vision_model: str = "gpt-4.1-mini"
    vision_model_base_url: str = ""
    vision_model_api_key: str = ""
    vision_model_azure_endpoint: str = ""   # 例如 https://YOUR-RESOURCE.openai.azure.com/（留空则该模型走 mock 回退）
    vision_model_api_version: str = "2024-12-01-preview"

    # 生图模型（GPT-Image-1）：表格分析的「架构 / 关系图」用它出概念图。
    # 任一值为占位/空 → image_generate 进入降级模式（不报错，前端显示占位卡片）。
    # provider=azure 时 image_model 填 Azure 部署名；provider=openai_compatible 时填模型名（gpt-image-1）。
    image_model_provider: str = "azure"
    image_model: str = "gpt-image-1-2025-04-15"   # Azure 部署名（不是模型名）
    image_model_base_url: str = ""
    image_model_api_key: str = ""                 # 留空则自动复用 vision 的 key（同一 Azure 资源）
    image_model_azure_endpoint: str = ""   # 例如 https://YOUR-RESOURCE.openai.azure.com/（留空则该模型走 mock 回退）
    image_model_api_version: str = "2025-04-01-preview"
    image_size: str = "1024x1024"

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
