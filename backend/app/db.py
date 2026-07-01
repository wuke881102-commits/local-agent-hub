from __future__ import annotations

import aiosqlite
from contextlib import asynccontextmanager
from typing import AsyncIterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS asset (
  asset_id TEXT PRIMARY KEY,
  asset_type TEXT NOT NULL,
  title TEXT,
  url TEXT,
  owner TEXT,
  owner_id TEXT,
  created_time TEXT,
  updated_time TEXT,
  source_space TEXT,
  path TEXT,
  tags TEXT,
  category TEXT,
  summary TEXT,
  last_processed_at TEXT,
  last_task_status TEXT,
  raw_json TEXT,
  index_state TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_asset_type ON asset(asset_type);
CREATE INDEX IF NOT EXISTS idx_asset_updated ON asset(updated_time);

CREATE TABLE IF NOT EXISTS task_run (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  scene TEXT,
  target TEXT,
  inputs TEXT,
  status TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  result_path TEXT,
  error TEXT,
  payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_started ON task_run(started_at);

CREATE TABLE IF NOT EXISTS task_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  message TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_log_task ON task_log(task_id, id);

CREATE TABLE IF NOT EXISTS writeback_queue (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  action_type TEXT NOT NULL,
  target TEXT,
  payload TEXT,
  status TEXT NOT NULL,
  created_at TEXT,
  confirmed_at TEXT,
  executed_at TEXT,
  result TEXT
);
CREATE INDEX IF NOT EXISTS idx_writeback_task ON writeback_queue(task_id);
CREATE INDEX IF NOT EXISTS idx_writeback_status ON writeback_queue(status);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT,
  agent_id TEXT,
  action TEXT NOT NULL,
  target TEXT,
  outcome TEXT,
  details TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

-- LLM 聚类结果缓存：key = (model, sha256(asset_ids+titles+spaces+updated))
CREATE TABLE IF NOT EXISTS llm_cluster_cache (
  cache_key TEXT PRIMARY KEY,
  payload   TEXT NOT NULL,
  created_at TEXT
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(settings.db_path) as db:
        await db.executescript(SCHEMA)
        # Idempotent migrations for columns added after v0.1.0 ship.
        async with db.execute("PRAGMA table_info(task_run)") as cur:
            task_cols = {row[1] async for row in cur}
        if "payload" not in task_cols:
            await db.execute("ALTER TABLE task_run ADD COLUMN payload TEXT")
        async with db.execute("PRAGMA table_info(asset)") as cur:
            asset_cols = {row[1] async for row in cur}
        if "owner_id" not in asset_cols:
            await db.execute("ALTER TABLE asset ADD COLUMN owner_id TEXT")
        if "index_state" not in asset_cols:
            # 软标记：刷新时把飞书侧已删/已失权的资产标为 'removed'，从计数与列表中排除。
            await db.execute(
                "ALTER TABLE asset ADD COLUMN index_state TEXT NOT NULL DEFAULT 'active'"
            )
        # 列已就绪后再建索引（放在 SCHEMA executescript 里会先于上面的 ALTER 执行而报错）。
        await db.execute("CREATE INDEX IF NOT EXISTS idx_asset_state ON asset(index_state)")
        await db.commit()


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    db = await aiosqlite.connect(settings.db_path)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()
