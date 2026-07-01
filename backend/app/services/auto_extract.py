"""自动化提炼 —— 把「按 Enter 自动留痕」的工作截图，按固定频率喂给视觉模型，
提炼这段时间的工作内容（说明 / 重点 / 操作 / 会议）。

与「内容生成」严格隔离：
- 截图只落在应用私有目录 ``settings.captures_path``（DATA_ROOT/captures），用户在
  「本地目录 / 内容生成」里浏览的是自己挑的目录，故这些截图不会出现在任何内容生成
  的文件选择器中。
- 提炼结果（digest）独立存到 captures/digests.jsonl，不进「运行记录」，避免每隔几分钟
  就刷一条任务。

生命周期：
- ``start(interval_min)``：开 Enter 全局钩子（复用 capture_session，目录指向私有目录），
  并起一个后台 asyncio 循环，按频率自动提炼。
- ``stop()``：停钩子 + 取消循环。
- ``distill_now()``：手动立即提炼当前窗口（也会重置下一次自动提炼的计时）。
- 后端重启后钩子与循环都不在了，状态回到「未开启」（与 capture_session 一致）。
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io
import json
import logging
import mimetypes
import time
import uuid
from pathlib import Path

from ..config import settings
from ..llm import get_llm
from . import capture_session

log = logging.getLogger("auto_extract")

_MAX_IMAGES = 12          # 单次提炼最多读 12 张（窗口内均匀采样），控制 token / 时延
_MAX_EDGE = 1280          # 图片长边超过则等比缩小，省 token
_MIN_INTERVAL = 1
_MAX_INTERVAL = 240
_DEFAULT_INTERVAL = 15
_MAX_SESSION_SECONDS = 10 * 3600   # 单次提炼会话最长 10 小时，到点自动停止

_DIGEST_FILE = "digests.jsonl"

_state = {
    "active": False,
    "interval_min": _DEFAULT_INTERVAL,
    "started_at": "",
    "last_run_at": "",
    "next_run_at": "",
    "digest_count": 0,
    "error": "",
    "busy": False,            # 正在提炼中
    "auto_stopped": False,    # 因达到最长时长而自动停止（供前端提示）
}
_loop_task: "asyncio.Task | None" = None
_distill_lock = asyncio.Lock()
_cursor_ts = 0.0             # 已处理到的截图 mtime 上界（epoch 秒）
_session_start_ts = 0.0     # 本次会话「开始」时刻（停止后保留，供「本次会话」范围筛选）


# ── 路径 / 文件 ───────────────────────────────────────────────────────────

def captures_dir() -> Path:
    d = settings.captures_path
    d.mkdir(parents=True, exist_ok=True)
    return d


def _digest_path() -> Path:
    return captures_dir() / _DIGEST_FILE


def _list_shots(after_ts: float = 0.0, before_ts: float | None = None) -> list[Path]:
    """按 mtime 升序列出私有目录里的截图（png/jpg）。"""
    d = captures_dir()
    out: list[tuple[float, Path]] = []
    for p in d.iterdir():
        if not p.is_file() or p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m <= after_ts:
            continue
        if before_ts is not None and m > before_ts:
            continue
        out.append((m, p))
    out.sort(key=lambda x: x[0])
    return [p for _, p in out]


def _sample(files: list[Path], k: int) -> list[Path]:
    """窗口内截图过多时均匀采样 k 张（保留首尾，覆盖整段时间）。"""
    n = len(files)
    if n <= k:
        return files
    step = (n - 1) / (k - 1)
    idx = sorted({round(i * step) for i in range(k)})
    return [files[i] for i in idx]


def _to_data_uri(path: Path) -> str | None:
    """读单张图片 → data URI；过大用 Pillow 等比缩小。失败返回 None。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    try:
        from PIL import Image  # noqa: PLC0415
        with Image.open(io.BytesIO(raw)) as im:
            w, h = im.size
            if max(w, h) > _MAX_EDGE:
                scale = _MAX_EDGE / float(max(w, h))
                im = im.convert("RGB").resize((int(w * scale), int(h * scale)))
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=82)
                raw = buf.getvalue()
                mime = "image/jpeg"
    except Exception:  # noqa: BLE001 —— 缩放失败就用原图
        pass
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


# ── digest 存取 ──────────────────────────────────────────────────────────

def _append_digest(rec: dict) -> None:
    path = _digest_path()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


_MD_SECTIONS = (("重点", "highlights"), ("操作", "operations"), ("会议", "meetings"), ("待办", "todos"))


def digest_to_markdown(rec: dict) -> str:
    """把一条提炼记录排版成可直接发群的 Markdown（说明 / 重点 / 操作 / 会议 / 待办）。"""
    lines: list[str] = [f"## 工作提炼 · {rec.get('window_label') or rec.get('created_at', '')}"]
    if rec.get("summary"):
        lines += ["", str(rec["summary"])]
    for label, key in _MD_SECTIONS:
        items = rec.get(key) or []
        if items:
            lines += ["", f"**{label}**"] + [f"- {it}" for it in items]
    apps = rec.get("apps") or []
    if apps:
        lines += ["", "涉及：" + " / ".join(apps)]
    return "\n".join(lines).strip()


def list_digests(limit: int = 50) -> list[dict]:
    """最新在前。每条附带 ``markdown`` 字段（供协作分发直接发送）。"""
    path = _digest_path()
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    out.reverse()
    out = out[:limit]
    for rec in out:
        rec["markdown"] = digest_to_markdown(rec)
    return out


def _count_digests() -> int:
    path = _digest_path()
    if not path.exists():
        return 0
    try:
        return sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())
    except OSError:
        return 0


def clear_digests() -> int:
    """清空提炼记录（不动截图文件）。返回清掉的条数。"""
    path = _digest_path()
    n = _count_digests()
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
    _state["digest_count"] = 0
    return n


def clear_shots(scope: str = "all") -> int:
    """删除私有目录里的截图文件（不动提炼记录）。返回删掉的张数。

    scope="session"：只删本次会话（自「开始」以来）的截图；从未开始过则不删。
    scope="all"：删私有目录里的全部截图。
    与 list_shots 的范围语义保持一致——前端选「本次会话」时清空只清本次会话。
    """
    if scope == "session":
        if not _session_start_ts:
            return 0
        targets = _list_shots(after_ts=_session_start_ts)
    else:
        targets = _list_shots()
    n = 0
    for p in targets:
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def list_shots(limit: int = 60, scope: str = "session") -> list[dict]:
    """最近截图（供本场景内透明展示，最新在前）。

    scope="session"：只列本次会话（自「开始」以来）的截图；从未开始过则返回空。
    scope="all"：列私有目录里的全部截图。
    """
    if scope == "session":
        if not _session_start_ts:
            return []
        files = _list_shots(after_ts=_session_start_ts)
    else:
        files = _list_shots()
    files.reverse()
    out: list[dict] = []
    for p in files[:limit]:
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({
            "name": p.name, "path": str(p), "size": st.st_size,
            "mtime": dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })
    return out


# ── 提炼（LLM） ──────────────────────────────────────────────────────────

_SYSTEM = (
    "你是工作日志助理。用户在一段时间内的工作过程中，每按一次 Enter 就自动留存了一张"
    "「当前窗口」截图。请仅依据这些截图中可见的信息，提炼这段时间用户在做什么。"
    "不要臆测、不要编造截图里看不到的内容；信息不足时相应字段留空数组或空字符串。"
    "全部用简体中文。严格只输出一个 JSON 对象，不要任何额外文字或代码块标记。"
)


def _build_user_prompt(n: int, window_label: str) -> str:
    return (
        f"以下 {n} 张截图按时间先后排列，覆盖时间段：{window_label}。\n"
        "请综合提炼这段时间的工作，输出 JSON，字段如下（数组元素为简短中文短句）：\n"
        "{\n"
        '  "summary": "2-4 句话，概述这段时间整体在做什么",\n'
        '  "highlights": ["这段时间的重点 / 关注事项"],\n'
        '  "operations": ["具体的操作 / 动作，如在某系统里做了什么"],\n'
        '  "meetings": ["涉及的会议 / 沟通，含主题与要点（若无则空数组）"],\n'
        '  "todos": ["从截图能看出的待办 / 后续事项（若无则空数组）"],\n'
        '  "apps": ["涉及到的应用 / 系统 / 网站名称"]\n'
        "}"
    )


def _parse_json(text: str) -> dict | None:
    """从模型输出里抠出 JSON 对象（容忍 ```json 围栏与前后多余文字）。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.endswith("```"):
            t = t[: t.rfind("```")]
    t = t.strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(t[i : j + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _as_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


async def _distill(window_start_ts: float, window_end_ts: float) -> dict | None:
    """对 (window_start_ts, window_end_ts] 内的截图做一次提炼。

    返回写入的 digest 记录；窗口内无截图时返回 None（不产出 digest）。
    """
    files = _list_shots(after_ts=window_start_ts, before_ts=window_end_ts)
    if not files:
        return None
    picked = _sample(files, _MAX_IMAGES)

    data_uris: list[str] = []
    for p in picked:
        uri = await asyncio.to_thread(_to_data_uri, p)
        if uri:
            data_uris.append(uri)
    if not data_uris:
        return None

    ws = dt.datetime.fromtimestamp(window_start_ts)
    we = dt.datetime.fromtimestamp(window_end_ts)
    same_day = ws.date() == we.date()
    window_label = (
        f"{ws:%Y-%m-%d %H:%M} ~ {we:%H:%M}" if same_day
        else f"{ws:%Y-%m-%d %H:%M} ~ {we:%Y-%m-%d %H:%M}"
    )

    llm = get_llm()
    user = _build_user_prompt(len(data_uris), window_label)
    raw = await llm.vision_complete(
        data_uris, user, system=_SYSTEM,
        max_tokens=2048, temperature=0.3, timeout=180, retries=1,
    )
    obj = _parse_json(raw) or {}

    rec = {
        "id": "dg-" + uuid.uuid4().hex[:8],
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "window_start": ws.isoformat(timespec="seconds"),
        "window_end": we.isoformat(timespec="seconds"),
        "window_label": window_label,
        "n_shots": len(files),
        "n_used": len(data_uris),
        "summary": str(obj.get("summary") or "").strip(),
        "highlights": _as_list(obj.get("highlights")),
        "operations": _as_list(obj.get("operations")),
        "meetings": _as_list(obj.get("meetings")),
        "todos": _as_list(obj.get("todos")),
        "apps": _as_list(obj.get("apps")),
    }
    if not rec["summary"] and not any(
        rec[k] for k in ("highlights", "operations", "meetings", "todos")
    ):
        # 模型未配置（mock）或输出无法解析——记一条占位，便于用户排查。
        rec["summary"] = "（未能从截图提炼出结构化内容：可能视觉模型未配置，或这段时间无有效画面。）"
        rec["error"] = True

    await asyncio.to_thread(_append_digest, rec)
    return rec


# ── 会话控制 ─────────────────────────────────────────────────────────────

def _set_next_run(from_ts: float | None = None) -> None:
    base = from_ts if from_ts is not None else time.time()
    nxt = base + _state["interval_min"] * 60
    _state["next_run_at"] = dt.datetime.fromtimestamp(nxt).isoformat(timespec="seconds")


def _max_duration_reached() -> bool:
    """本次会话是否已达最长时长（自「开始」起 >= _MAX_SESSION_SECONDS）。"""
    return bool(_session_start_ts) and (time.time() - _session_start_ts) >= _MAX_SESSION_SECONDS


async def _finish_max_duration() -> None:
    """到达最长时长：把剩余窗口最后提炼一次（避免这段截图丢失），再停掉钩子与循环状态。

    只能在 _loop 内部调用——这里不去 cancel/await _loop_task（那是自己），
    只置状态 + 停 Enter 钩子，让 _loop 的 while 条件自然退出。
    """
    global _cursor_ts
    async with _distill_lock:
        now = time.time()
        try:
            _state["busy"] = True
            rec = await _distill(_cursor_ts, now)
            _cursor_ts = now
            if rec:
                _state["last_run_at"] = dt.datetime.now().isoformat(timespec="seconds")
                _state["digest_count"] = _count_digests()
                log.info("auto digest %s (final flush): %d shots", rec["id"], rec["n_shots"])
        except Exception as e:  # noqa: BLE001
            _state["error"] = f"{type(e).__name__}: {e}"
            log.warning("final-flush distill failed: %s", e)
        finally:
            _state["busy"] = False
    _state["active"] = False
    _state["auto_stopped"] = True
    _state["next_run_at"] = ""
    capture_session.stop()
    log.info("auto-extract auto-stopped: reached max session duration (%.0fh)", _MAX_SESSION_SECONDS / 3600)


async def _loop() -> None:
    """后台循环：到点就提炼一次当前窗口；累计运行满 _MAX_SESSION_SECONDS 后自动停止。"""
    global _cursor_ts
    try:
        while _state["active"]:
            if _max_duration_reached():
                await _finish_max_duration()
                break
            interval_s = max(_MIN_INTERVAL, _state["interval_min"]) * 60
            target = time.time() + interval_s
            _set_next_run()
            # 分段 sleep，让 stop / 间隔变更 / 到达最长时长 更跟手
            while _state["active"] and time.time() < target and not _max_duration_reached():
                await asyncio.sleep(min(5.0, max(0.5, target - time.time())))
            if not _state["active"]:
                break
            # 睡眠期间到达最长时长：做最后一次 flush 再停
            if _max_duration_reached():
                await _finish_max_duration()
                break
            async with _distill_lock:
                now = time.time()
                try:
                    _state["busy"] = True
                    rec = await _distill(_cursor_ts, now)
                    _cursor_ts = now
                    _state["last_run_at"] = dt.datetime.now().isoformat(timespec="seconds")
                    _state["error"] = ""
                    if rec:
                        _state["digest_count"] = _count_digests()
                        log.info("auto digest %s: %d shots", rec["id"], rec["n_shots"])
                except Exception as e:  # noqa: BLE001
                    _state["error"] = f"{type(e).__name__}: {e}"
                    log.warning("auto distill failed: %s", e)
                finally:
                    _state["busy"] = False
    except asyncio.CancelledError:  # 正常停止
        pass


async def start(interval_min: int = _DEFAULT_INTERVAL) -> dict:
    """开启自动化提炼会话。目录无效 / 钩子注册失败 → 由 capture_session 抛错。"""
    global _loop_task, _cursor_ts, _session_start_ts
    interval_min = max(_MIN_INTERVAL, min(_MAX_INTERVAL, int(interval_min)))

    d = captures_dir()
    # 复用全局 Enter 钩子，截图落到私有目录（capture 失败会抛 ValueError/RuntimeError）
    capture_session.start(str(d))

    # 取消可能残留的旧循环
    if _loop_task and not _loop_task.done():
        _loop_task.cancel()

    now = time.time()
    _cursor_ts = now
    _session_start_ts = now
    _state.update({
        "active": True,
        "interval_min": interval_min,
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        "last_run_at": "",
        "error": "",
        "busy": False,
        "auto_stopped": False,
        "digest_count": _count_digests(),
    })
    _set_next_run(now)
    _loop_task = asyncio.create_task(_loop())
    log.info("auto-extract started: interval=%dmin dir=%s", interval_min, d)
    return status()


async def stop() -> dict:
    global _loop_task
    _state["active"] = False
    capture_session.stop()
    if _loop_task and not _loop_task.done():
        _loop_task.cancel()
        try:
            await _loop_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _loop_task = None
    _state["next_run_at"] = ""
    log.info("auto-extract stopped")
    return status()


async def distill_now() -> dict:
    """立即提炼当前窗口（手动触发），并重置下一次自动提炼的计时。"""
    global _cursor_ts
    if _distill_lock.locked():
        raise RuntimeError("正在提炼中，请稍候。")
    async with _distill_lock:
        now = time.time()
        start_ts = _cursor_ts if _cursor_ts else (now - _state["interval_min"] * 60)
        try:
            _state["busy"] = True
            rec = await _distill(start_ts, now)
        finally:
            _state["busy"] = False
        _cursor_ts = now
        _state["last_run_at"] = dt.datetime.now().isoformat(timespec="seconds")
        _state["digest_count"] = _count_digests()
        if _state["active"]:
            _set_next_run(now)
    if not rec:
        return {"ok": True, "digest": None, "message": "这段时间还没有新的截图。"}
    return {"ok": True, "digest": rec}


def status() -> dict:
    cap = capture_session.status()
    out = dict(_state)
    out["directory"] = cap.get("directory") or str(settings.captures_path)
    out["shot_count"] = cap.get("count", 0)
    out["last_shot_at"] = cap.get("last_at", "")
    out["capture_error"] = cap.get("error", "")
    # 钩子层若已掉线（如后端重启后），active 以钩子为准
    if _state["active"] and not cap.get("active"):
        out["active"] = False
    # 「已截 N 张」按当前提炼频率窗口计数：自上次提炼(_cursor_ts)以来待下次提炼的截图数，
    # 每次提炼后归零重新累计——而非从开始提炼起的累计总数。shot_count 仍保留为会话累计。
    out["window_shot_count"] = len(_list_shots(after_ts=_cursor_ts)) if out.get("active") else 0
    if not out.get("digest_count"):
        out["digest_count"] = _count_digests()
    return out
