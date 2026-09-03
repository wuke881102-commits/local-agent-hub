"""AIHOT（aihot.virxact.com）数据接入 —— 新闻走公开 API v1，模型榜解析榜单页。

为什么分两条路（重要，改这个文件前先读）：
  · **AI 新闻**：站点在 /agent 与 llms.txt 明确提供匿名只读的 `/api/v1/*`（items /
    hot-topics / stories / dailies），并在 robots.txt 里对通用 UA `Allow: /api/v1/`。
    有 ETag + `s-maxage`，是官方推荐的机读入口 → 直接用，最稳。
  · **模型榜**：`/api/v1/leaderboard` 返回 404，站点没有榜单 API（2026-08 核实）。
    榜单页 /leaderboard 是 Next.js SSR，整张榜以 JSON 内联在 RSC flight 负载里
    （`"entries":[{rank,name,provider,score,...}]`），比刮 DOM 稳得多 → 解析它。
    robots.txt 的 `User-agent: *` 组没有 Disallow /leaderboard，可抓。
    这条路天生比 API 脆（站点改版即失效），所以：解析失败给明确报错 +
    `AIHOT_LEADERBOARD_PATH` 可覆盖，并建议向站方（X-AIHOT-Contact）申请榜单 API。

合规（不要绕过）：
  响应头 `X-AIHOT-Commercial-Use: written-authorization-required`。本模块把它连同
  attribution 一起带进 payload，由两个 Agent 在产出页脚强制展示；产物只落本地草稿，
  不自动写回飞书 / 不自动外发。

缓存与限流：
  站点公开 API 限速 60r/m（nginx），页面另有桶。这里按 `s-maxage` 量级做磁盘缓存
  （DATA_ROOT/cache/aihot/*.json）+ ETag 条件请求：TTL 内不发包，过期发
  If-None-Match，304 就续期。429 直接抛错并转述 Retry-After，绝不重试打穿。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import DATA_ROOT, settings
from ..html.design_system import LUMEN_LIGHT_ROOT_CSS

log = logging.getLogger("services.aihot")

BS = chr(92)   # 反斜杠字面量（见 _grab_array 的字符串扫描），避免源码里出现转义歧义

# 各端点的缓存 TTL（秒）。取自站点 Cache-Control 的 s-maxage 量级，
# 宁可比它长一点：本地工作台不需要秒级新鲜度，少打人家接口。
_TTL = {
    "items": 120,
    "hot-topics": 300,
    "stories": 300,
    "dailies": 900,
    "leaderboard": 900,
}

_CATEGORIES = ("ai-models", "ai-products", "industry", "paper", "tip")
_WINDOWS = ("24h", "7d")
_MODES = ("selected", "all")

CATEGORY_CN = {
    "ai-models": "模型发布/更新",
    "ai-products": "产品发布",
    "industry": "行业事件",
    "paper": "论文",
    "tip": "教程/技巧",
}

# 站点只在 /api/v1/* 的响应上带策略头；榜单页（HTML）不带。为了让「模型榜」的产出
# 页脚也照样显示商用限制，这里存一份 2026-08-21 实测到的声明作为兜底。
DEFAULT_POLICY = {
    "commercial_use": "written-authorization-required",
    "policy_version": "1.0",
    "contact": "wzglyay@virxact.com",
    "release": "",
}


def merge_policy(policy: dict | None) -> dict:
    """把实测到的策略头与兜底值合并（空字段用兜底填上）。

    榜单页不带策略头，直接透传会得到一堆空字符串，前端的「商用须授权」提示就不显示了——
    而那条提示恰恰是最不能丢的。所以按字段兜底，而不是整体二选一。
    """
    out = dict(DEFAULT_POLICY)
    for k, v in (policy or {}).items():
        if v:
            out[k] = v
    return out


class AihotError(RuntimeError):
    """对外统一的错误类型，message 直接给用户看（中文、可操作）。"""


def base_url() -> str:
    """站点根地址（无尾斜杠）。拼详情页链接时也用它，别自己硬编码域名。"""
    return (settings.aihot_base_url or "https://aihot.virxact.com").rstrip("/")


_base = base_url   # 兼容本模块内的旧调用名


def _ua() -> str:
    """诚实标明身份的 UA。

    不伪装浏览器：站点用 nginx UA 黑名单挡「商业采集器」，但 robots.txt 里写明
    AI/agent 流量全放行。伪装反而让对方无法识别与限流我们，出问题也没法联系。
    """
    if settings.aihot_user_agent:
        return settings.aihot_user_agent
    from ..main import APP_VERSION  # 局部导入避免循环依赖
    return "LocalAgentHub/%s (local personal agent; +aihot scene)" % APP_VERSION


def _cache_dir() -> Path:
    p = DATA_ROOT / "cache" / "aihot"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_file(kind: str, key: str) -> Path:
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return _cache_dir() / ("%s-%s.json" % (kind, h))


def _read_cache(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 缓存坏了就当没有
        return None


def _write_cache(path: Path, obj: dict) -> None:
    try:
        path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _policy(headers: httpx.Headers) -> dict:
    """抽出站点的策略/署名相关响应头，带进 payload 供页脚展示。"""
    return {
        "commercial_use": headers.get("X-AIHOT-Commercial-Use", ""),
        "policy_version": headers.get("X-AIHOT-Policy-Version", ""),
        "contact": headers.get("X-AIHOT-Contact", ""),
        "release": headers.get("X-Aihot-Release", ""),
    }


def _raise_for_status(r: httpx.Response, what: str) -> None:
    if r.status_code == 429:
        ra = r.headers.get("Retry-After", "")
        raise AihotError(
            "AIHOT 接口限流（429）。请 %s 秒后再试——本机已做磁盘缓存，"
            "短时间内重复运行不会再打接口。" % (ra or "稍后")
        )
    if r.status_code == 403:
        raise AihotError(
            "AIHOT 拒绝了本次请求（403，%s）。可能触发了站点的 UA / 频率防护，稍后重试；"
            "若持续失败可在 .env 里调整 AIHOT_USER_AGENT，或联系站方。" % what
        )
    if r.status_code >= 400:
        try:
            j = r.json()
            detail = "：%s %s" % (j.get("title") or "", j.get("detail") or "")
        except Exception:  # noqa: BLE001
            detail = "：%s" % r.text[:160]
        raise AihotError("AIHOT %s 返回 HTTP %d%s" % (what, r.status_code, detail.strip()))


async def _get(path: str, *, kind: str, params: dict | None = None,
               force: bool = False, raw: bool = False) -> tuple[Any, dict]:
    """带 TTL + ETag 的 GET。返回 (data, meta)。

    raw=True 时 data 是响应文本（榜单页），否则是 JSON。
    meta 含 cached / age_s / policy 等，供结果页展示「数据几点抓的」。
    """
    url = _base() + path
    key = url + "?" + json.dumps(params or {}, sort_keys=True)
    cf = _cache_file(kind, key)
    cached = _read_cache(cf)
    ttl = _TTL.get(kind, 300)
    now = time.time()

    if cached and not force and (now - float(cached.get("fetched_at") or 0)) < ttl:
        meta = dict(cached.get("meta") or {})
        meta["cached"] = True
        meta["age_s"] = int(now - float(cached["fetched_at"]))
        return cached.get("data"), meta

    headers = {
        "User-Agent": _ua(),
        "Accept": "text/html,application/xhtml+xml" if raw else "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if cached and cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]

    timeout = float(settings.aihot_timeout or 30)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cli:
            r = await cli.get(url, params=params, headers=headers)
    except httpx.HTTPError as e:
        if cached:   # 网络挂了但有旧缓存：用旧的总比整个任务失败好
            meta = dict(cached.get("meta") or {})
            meta.update({"cached": True, "stale": True,
                         "age_s": int(now - float(cached.get("fetched_at") or now))})
            log.warning("aihot %s 请求失败，回退过期缓存：%s", path, e)
            return cached.get("data"), meta
        raise AihotError("连接 AIHOT 失败（%s）：%s" % (type(e).__name__, str(e)[:160])) from e

    if r.status_code == 304 and cached:
        cached["fetched_at"] = now
        _write_cache(cf, cached)
        meta = dict(cached.get("meta") or {})
        meta.update({"cached": True, "not_modified": True, "age_s": 0})
        return cached.get("data"), meta

    _raise_for_status(r, path)

    data: Any = r.text if raw else r.json()
    meta = {"cached": False, "age_s": 0, "url": str(r.url), "policy": _policy(r.headers),
            "fetched_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))}
    _write_cache(cf, {"fetched_at": now, "etag": r.headers.get("ETag", ""),
                      "data": data, "meta": meta})
    return data, meta


# ── AI 新闻：公开 API v1 ────────────────────────────────────────────

async def fetch_items(*, window: str = "24h", mode: str = "selected",
                      category: str = "", q: str = "", limit: int = 40,
                      force: bool = False) -> tuple[list[dict], dict]:
    """/api/v1/items —— 资讯列表。q 非空时按关键词检索（站点会自动放宽到全量池）。"""
    params: dict[str, Any] = {
        "mode": mode if mode in _MODES else "selected",
        "window": window if window in _WINDOWS else "24h",
        "limit": max(1, min(int(limit or 40), 100)),
    }
    if category in _CATEGORIES:
        params["category"] = category
    q = (q or "").strip()
    if len(q) >= 2:
        params["q"] = q[:200]
    data, meta = await _get("/api/v1/items", kind="items", params=params, force=force)
    return list((data or {}).get("items") or []), meta


async def fetch_hot_topics(force: bool = False) -> tuple[list[dict], dict]:
    """/api/v1/hot-topics —— 当前热点 Top 10（含 rank，不返回热度值）。"""
    data, meta = await _get("/api/v1/hot-topics", kind="hot-topics", force=force)
    return list((data or {}).get("items") or []), meta


async def fetch_story(public_id: str, force: bool = False) -> tuple[dict, dict]:
    """/api/v1/stories/{publicId} —— 事件报道时间线 + 随演化更新的 AI 综述。"""
    pid = (public_id or "").strip()
    if not pid:
        raise AihotError("缺少事件 publicId")
    data, meta = await _get("/api/v1/stories/" + pid, kind="stories", force=force)
    return (data or {}), meta


async def fetch_daily_latest(force: bool = False) -> tuple[dict, dict]:
    """/api/v1/dailies/latest —— 最新一期 AI 日报（站点每天 08:00 北京时间发布）。"""
    data, meta = await _get("/api/v1/dailies/latest", kind="dailies", force=force)
    return ((data or {}).get("report") or {}), meta


def story_id_from_url(url: str) -> str:
    """从 links.story（.../story/<uuid>）里取 publicId。"""
    m = re.search(r"/story/([0-9a-zA-Z-]{8,})", url or "")
    return m.group(1) if m else ""


# ── 模型榜：解析 /leaderboard 的 RSC flight 负载 ──────────────────────

def _flight_text(html: str) -> str:
    """把页面里所有 self.__next_f.push([1,"…"]) 的字符串块拼成 flight 文本。

    这些块是标准 JS 字符串字面量，用 JSONDecoder.raw_decode 逐个正确解码
    （正则贪婪/非贪婪都会在内嵌 `"]` 处切错，别改回纯正则截取）。
    """
    dec = json.JSONDecoder()
    parts: list[str] = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,\s*', html):
        try:
            s, _ = dec.raw_decode(html, m.end())
        except ValueError:
            continue
        if isinstance(s, str):
            parts.append(s)
    return "".join(parts)


def _grab_array(txt: str, key: str) -> str | None:
    """在 txt 里找 "<key>":[ … ] 并做括号配平（跳过字符串内的括号）。"""
    k = '"%s":[' % key
    i = txt.find(k)
    if i < 0:
        return None
    start = i + len(k) - 1
    depth = 0
    instr = False
    esc = False
    for j in range(start, len(txt)):
        c = txt[j]
        if instr:
            if esc:
                esc = False
            elif c == BS:
                esc = True
            elif c == '"':
                instr = False
            continue
        if c == '"':
            instr = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return txt[start:j + 1]
    return None


_UPDATED_RE = re.compile(r'"更新于 ","([^"]{3,40})"')
_BOARDS_RE = re.compile(r'"综合 ",\[[^\]]{0,200}?"children":(\d+)')


async def fetch_leaderboard(force: bool = False) -> dict:
    """抓 + 解析模型榜。返回 {entries, updated_label, board_count, source_url, meta}。

    entries 保留站点原字段（rank/name/provider/score/coverage/confidence/价格/
    证据分项…），归一化交给调用方，这里只负责「拿到真数据」。
    """
    url_path = settings.aihot_leaderboard_path or "/leaderboard"
    html, meta = await _get(url_path, kind="leaderboard", force=force, raw=True)
    flight = _flight_text(html or "")
    raw = _grab_array(flight, "entries")
    if not raw:
        raise AihotError(
            "解析 AIHOT 模型榜失败：榜单页里没找到 entries 数据块。站点可能改版了"
            "（本功能靠解析榜单页，因为站点未提供榜单 API）。可先人工核对 "
            + _base() + url_path + "，或在 .env 里用 AIHOT_LEADERBOARD_PATH 指向新路径。"
        )
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AihotError("AIHOT 模型榜 entries 不是合法 JSON（站点改版？）：%s" % e) from e
    if not isinstance(entries, list) or not entries:
        raise AihotError("AIHOT 模型榜解析结果为空（站点改版？）")

    m = _UPDATED_RE.search(flight)
    b = _BOARDS_RE.search(flight)
    return {
        "entries": entries,
        "updated_label": m.group(1) if m else "",
        "board_count": int(b.group(1)) if b else 0,
        "source_url": _base() + url_path,
        "meta": meta,
    }


# ── 产出页：Lumen-light 单文件 HTML 外壳 ───────────────────────────────

_PAGE_CSS = """
*{box-sizing:border-box}
body{margin:0;background:var(--surface-page);color:var(--text-primary);
  font:14px/1.65 "Inter","PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:40px 24px 64px}
.hd{border-bottom:2px solid var(--brand-500);padding-bottom:16px;margin-bottom:28px}
.hd h1{margin:0 0 6px;font-size:26px;font-weight:650;letter-spacing:-.01em}
.hd .sub{color:var(--text-tertiary);font-size:13px}
.card{background:var(--surface-elevated);border:1px solid var(--border-default);
  border-radius:12px;padding:20px 22px;margin:0 0 20px}
.card > h2{margin:0 0 14px;font-size:17px;font-weight:620}
.card > h2 .n{color:var(--text-tertiary);font-weight:400;font-size:13px;margin-left:8px}
h3{font-size:15px;margin:20px 0 8px}
p{margin:0 0 10px;color:var(--text-secondary)}
ul,ol{margin:0 0 12px;padding-left:22px;color:var(--text-secondary)}
li{margin:3px 0}
a{color:var(--brand-600);text-decoration:none}
a:hover{text-decoration:underline}
code{background:var(--surface-subtle);border-radius:4px;padding:1px 5px;font-size:.92em}
.tw{overflow-x:auto;margin:0 -4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:var(--surface-subtle);text-align:left;padding:9px 12px;font-weight:600;
  color:var(--text-secondary);border-bottom:1px solid var(--border-default);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid var(--border-subtle);color:var(--text-secondary)}
tr:hover td{background:var(--surface-hover)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.rank{font-weight:700;color:var(--text-primary);font-variant-numeric:tabular-nums}
.mname{font-weight:600;color:var(--text-primary)}
.mprov{color:var(--text-tertiary);font-size:12px}
.pill{display:inline-block;border-radius:999px;padding:1px 9px;font-size:11.5px;font-weight:600;line-height:1.7}
.p-hi{background:var(--success-bg);color:var(--success)}
.p-md{background:var(--warning-bg);color:var(--warning)}
.p-lo{background:var(--error-bg);color:var(--error)}
.p-info{background:var(--info-bg);color:var(--info)}
.p-mine{background:var(--brand-50);color:var(--brand-700);border:1px solid var(--brand-200)}
.note{background:var(--surface-subtle);border-left:3px solid var(--border-strong);
  padding:12px 16px;border-radius:0 8px 8px 0;font-size:12.5px;color:var(--text-tertiary);margin:0 0 20px}
.src{font-size:12px;color:var(--text-tertiary)}
.ft{margin-top:32px;padding-top:16px;border-top:1px solid var(--border-default);
  font-size:12px;color:var(--text-tertiary);line-height:1.8}
.ft b{color:var(--text-secondary)}
"""


def esc(s: Any) -> str:
    import html as _h
    return _h.escape(str(s if s is not None else ""))


def render_page(*, title: str, subtitle: str, body: str, footer: str) -> str:
    """把片段拼成单文件 HTML（Lumen-light token + 本页版式），供草稿预览/下载。

    刻意不让模型直接生成 HTML：榜单/简报都是结构化数据，确定性渲染既省 token，
    又不可能把链接和数字写错。
    """
    # LUMEN_LIGHT_ROOT_CSS 本是「喂给模型的片段」，它自己闭不闭合 `:root{` 由那边决定。
    # 这里按实际括号数补齐，避免写死一个 `}` 而在对方已闭合时留下多余的括号。
    root = LUMEN_LIGHT_ROOT_CSS
    missing = root.count("{") - root.count("}")
    if missing > 0:
        root += "\n" + "}" * missing
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "<title>" + esc(title) + "</title>\n<style>\n" + root + "\n"
        + _PAGE_CSS + "</style>\n</head>\n<body>\n<div class=\"wrap\">\n"
        '<div class="hd"><h1>' + esc(title) + '</h1><div class="sub">' + subtitle + "</div></div>\n"
        + body + '\n<div class="ft">' + footer + "</div>\n</div>\n</body>\n</html>\n"
    )


def attribution_footer(policy: dict, *, source_url: str, extra: str = "") -> str:
    """统一页脚：署名 + 商用授权提示 + 补充说明。两个 Agent 都必须带。"""
    policy = policy or {}
    bits = [
        '数据来源 <a href="%s" target="_blank" rel="noreferrer">AIHOT</a>（%s）'
        % (esc(source_url), esc(source_url)),
    ]
    cu = policy.get("commercial_use") or DEFAULT_POLICY["commercial_use"]
    if cu:
        contact = policy.get("contact") or DEFAULT_POLICY["contact"]
        bits.append(
            "<b>商用须先获授权</b>（站点声明 X-AIHOT-Commercial-Use: " + esc(cu)
            + ("，联系 " + esc(contact) if contact else "")
            + "）。本页为本地工作草稿，未对外发布。"
        )
    if extra:
        bits.append(extra)
    return "<br>".join(bits)


# ── 归一化：站点原始字段 → 页面与 Agent 共用的行结构 ────────────────────
# 放在 service 层而不是各自的 Agent 里：「模型榜 / AI 新闻」页面要直接展示同样的
# 数据，两处各写一份归一化早晚会漂移（同一个价格两个页面显示不一样最难查）。

CONF_CN = {"HIGH": "高", "MEDIUM": "中", "LOW": "低"}

# 本机三档模型配置 → (展示名, settings 属性, 用途说明)。
# 这里必须照实标注哪些档真有调用点 —— 否则会建议用户去换一个根本不生效的档。
# 最强档曾经是空的（max/preview 实测超时而被弃用），现已重新启用：
# HTML 页面生成的两个形状都走它，实测依据见 agents/html_page.py 类顶注释。
LOCAL_TIERS = [
    ("均衡", "text_model", "会议纪要 / 表格分析 / 协作分发 / 问数据 / PDF 识别与金额测算"),
    ("快省", "text_model_fast", "批量回填 / 治理复核 / 邮件分类 / 主档超时后的兜底"),
    ("最强", "text_model_best", "HTML 页面生成（内容重组 + 自由版式直出）"),
]

# 本机在用、但**不参与榜单比较**的模型 → (用途, settings 属性, provider 属性, 用途说明)。
# AIHot 比的是通用文本模型；读图/生图/语音这几路要么不在它的评比范围内，要么型号
# 根本不会出现在榜上。所以它们不能塞进 LOCAL_TIERS —— 那会让四个全标「未上榜」，
# 而「未上榜」读起来是「排名靠后/查不到」，不是「这张榜不管这类模型」。
#
# 但也不能不列：在此之前，生图和语音这两个模型在整个界面里没有任何一处能看到，
# 想确认语音速记在用哪个模型只能去翻 .env。
OTHER_MODELS = [
    ("视觉", "vision_model", "vision_model_provider",
     "PDF 识别 / 本地读图 / HTML 页面生成的读图"),
    ("视觉兜底", "vision_fallback_model", "text_model_provider",
     "主档读图失败时顶上，走文本端点"),
    ("生图", "image_model", "image_model_provider",
     "多维表格分析里画架构图 / 关系图"),
    ("语音", "audio_model", "",
     "语音速记的实时转写，realtime WebSocket"),
]

DEFAULT_IN_OUT_RATIO = 3.0   # 性价比口径：假设输入:输出 token ≈ 1:3


def norm_model_name(s: str) -> str:
    """模型名归一：去掉所有非字母数字，用于跨写法匹配。

    "Qwen3.8 Max" / "qwen3.8-max" / "qwen3-8-max" → "qwen38max"
    """
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def blended_price(inp: float | None, out: float | None,
                  ratio: float = DEFAULT_IN_OUT_RATIO) -> float | None:
    """混合价：(输入 + ratio×输出) / (1+ratio)。ratio=3 即假设输入:输出 = 1:3。"""
    if inp is None and out is None:
        return None
    i = float(inp if inp is not None else 0.0)
    o = float(out if out is not None else 0.0)
    b = (i + ratio * o) / (1.0 + ratio)
    return b if b > 0 else None


def fmt_price(row: dict, key: str) -> str:
    v = row.get(key)
    if v is None:
        return row.get("price_label") or "待核验"
    return "¥%s" % ("%.2f" % float(v)).rstrip("0").rstrip(".")


def leaderboard_rows(entries: list[dict], ratio: float = DEFAULT_IN_OUT_RATIO) -> list[dict]:
    """榜单原始条目 → 表格行，并算好本机口径的性价比与其排名。

    前 8 列完全对齐榜单页；value / value_rank 是本地计算（站点没有这个指标）。
    价格「待核验」的模型 value=None，不参与性价比排名。
    """
    rows = [_lb_row(e, ratio) for e in entries]
    priced = [r for r in rows if r["value"]]
    for i, r in enumerate(sorted(priced, key=lambda x: -x["value"]), start=1):
        r["value_rank"] = i
    for r in rows:
        r.setdefault("value_rank", None)
    return rows


def _lb_row(e: dict, ratio: float) -> dict:
    inp = e.get("inputPricePerMillionCny")
    out = e.get("outputPricePerMillionCny")
    blended = blended_price(inp, out, ratio)
    score = float(e.get("score") or 0)
    cov = e.get("coverage")
    slug = e.get("slug") or ""
    return {
        "rank": e.get("rank"),
        "previous_rank": e.get("previousRank"),
        "rank_change": e.get("rankChange") or 0,
        "name": e.get("name") or "",
        "provider": e.get("provider") or "",
        "provider_slug": e.get("providerSlug") or "",
        "slug": slug,
        "detail_url": (base_url() + "/leaderboard/" + slug) if slug else "",
        "released": (e.get("releasedAt") or "")[:10],
        "score": score,
        "uncertainty": e.get("uncertainty"),
        "rank_from": e.get("possibleRankFrom"),
        "rank_to": e.get("possibleRankTo"),
        "completeness": round(float(cov) * 100, 1) if isinstance(cov, (int, float)) else None,
        "confidence": (e.get("confidence") or "").upper(),
        "metric_count": e.get("metricCount"),
        "summary": e.get("summary") or "",
        "context_tokens": e.get("contextWindowTokens"),
        "price_in": inp,
        "price_out": out,
        "price_in_usd": e.get("inputPricePerMillionUsd"),
        "price_out_usd": e.get("outputPricePerMillionUsd"),
        "price_blended": round(blended, 2) if blended else None,
        "value": round(score / blended, 2) if (blended and score) else None,
        "price_kind": e.get("pricingKind") or "",
        "price_label": e.get("pricingDisplayLabel") or "",
        "price_quote": e.get("priceQuote") or "",
        "price_source": e.get("pricingSourceName") or "",
        "price_source_url": e.get("pricingSourceUrl") or "",
        "price_verified_at": e.get("pricingVerifiedAt") or "",
        "price_basis": e.get("pricingBasis") or "",
        "official_model_id": e.get("pricingOfficialModelId") or "",
        "components": e.get("components") or {},
    }


def match_local_models(rows: list[dict]) -> list[dict]:
    """本机三档配置在榜里的位置。找不到就标 on_board=False（本身也是有用信息）。"""
    by_key: dict[str, dict] = {}
    for r in rows:
        for cand in (r["official_model_id"], r["slug"], r["name"]):
            k = norm_model_name(cand)
            if k and k not in by_key:
                by_key[k] = r
    out = []
    for label, attr, usage in LOCAL_TIERS:
        conf = getattr(settings, attr, "") or ""
        hit = by_key.get(norm_model_name(conf))
        out.append({
            "tier": label, "setting": attr.upper(), "model": conf, "usage": usage,
            "on_board": bool(hit),
            "rank": hit["rank"] if hit else None,
            "score": hit["score"] if hit else None,
            "value": hit["value"] if hit else None,
            "confidence": hit["confidence"] if hit else "",
            "board_name": hit["name"] if hit else "",
        })
    return out


def other_models() -> list[dict]:
    """本机在用、但不在这张榜比较范围内的模型（读图 / 兜底读图 / 生图 / 语音）。

    和 match_local_models 分两份返回，而不是合成一张表：那边每行有名次、共识分、
    性价比，这边一个都没有。混在一列里，空着的名次会被读成「查不到」，
    而实际是「不适用」——这两件事的处置完全不同。
    """
    out = []
    for label, attr, prov_attr, usage in OTHER_MODELS:
        out.append({
            "kind": label,
            "setting": attr.upper(),
            "model": str(getattr(settings, attr, "") or ""),
            "provider": str(getattr(settings, prov_attr, "") or "") if prov_attr else "",
            "usage": usage,
        })
    return out


def short_dt(iso: str) -> str:
    """ISO8601（UTC）→ 本地可读 `08-21 04:27`。解析失败原样截断。"""
    s = (iso or "").strip()
    if not s:
        return ""
    try:
        import datetime as _dt
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
        return d.strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return s[:16]


def news_item(it: dict) -> dict:
    """/items 或 /dailies 的一条资讯 → 统一行结构。"""
    links = it.get("links") or {}
    src = it.get("source") or {}
    cat = it.get("category") or ""
    return {
        "id": it.get("id") or "",
        "title": (it.get("title") or "").strip(),
        "original_title": (it.get("originalTitle") or "").strip(),
        "summary": (it.get("summary") or "").strip(),
        # reason = 站点写的「为什么值得看」，比 summary 更有判断力，务必带上
        "reason": (it.get("reason") or "").strip(),
        "category": cat,
        "category_zh": CATEGORY_CN.get(cat, cat or "其它"),
        "source_name": (src.get("name") or "").strip(),
        "url": links.get("original") or "",
        "aihot_url": links.get("aihot") or "",
        "story_url": links.get("story") or "",
        "published_at": it.get("publishedAt") or "",
        "published_zh": short_dt(it.get("publishedAt") or ""),
        "score": it.get("score"),
        "selected": bool(it.get("selected")),
    }


def dedupe_items(items: list[dict]) -> list[dict]:
    """按原文链接去重（站点已合并大部分同源转载，这里兜一层）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        key = (it["url"] or it["aihot_url"] or it["title"]).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def merge_hot_topics(items: list[dict], hot: list[dict]) -> int:
    """热点榜名次回填到条目上；榜上但不在本窗口资讯池里的事件补进 items。

    返回补进的条数。不补会出事：热点榜里当天最重要的事拿不到编号，
    Agent 侧的「引用体检」会把提到它的内容整条丢掉（实测丢 4 条）。
    """
    by_key: dict[str, dict] = {}
    for h in hot:
        L = h.get("links") or {}
        for k in (h.get("id"), L.get("original"), L.get("aihot")):
            if k:
                by_key[str(k).lower()] = h
    matched: set[int] = set()
    for it in items:
        for k in (it["id"], it["url"], it["aihot_url"]):
            h = by_key.get(str(k).lower()) if k else None
            if h:
                it["hot_rank"] = h.get("rank")
                it["story_url"] = it["story_url"] or (h.get("links") or {}).get("story") or ""
                matched.add(id(h))
                break
    added = 0
    for h in hot:
        if id(h) in matched:
            continue
        L = h.get("links") or {}
        names = h.get("sourceNames") or []
        items.append({
            "id": h.get("id") or "",
            "title": (h.get("title") or "").strip(),
            "original_title": "", "summary": "", "reason": "",
            "category": "", "category_zh": "热点事件",
            "source_name": ((h.get("source") or {}).get("name") or (names[0] if names else "")).strip(),
            "url": L.get("original") or "",
            "aihot_url": L.get("aihot") or "",
            "story_url": L.get("story") or "",
            "published_at": h.get("latestAt") or "",
            "published_zh": short_dt(h.get("latestAt") or ""),
            "score": None, "selected": False,
            "hot_rank": h.get("rank"),
            "source_count": h.get("sourceCount"),
        })
        added += 1
    return added


def story_summary(st: dict) -> dict:
    """/stories 响应 → 事件时间线行结构。"""
    s = st.get("story") or st or {}
    reports = s.get("reports") or []
    return {
        "public_id": s.get("publicId") or "",
        "title": (s.get("title") or "").strip(),
        "status": s.get("status") or "",
        "digest": (s.get("digest") or "").strip(),
        "latest": (s.get("latest") or "").strip() if isinstance(s.get("latest"), str) else "",
        "source_count": s.get("sourceCount"),
        "report_count": s.get("reportCount"),
        "url": (s.get("links") or {}).get("aihot") or "",
        "timeline": [{
            "at": short_dt(r.get("publishedAt") or r.get("at") or ""),
            "title": (r.get("title") or "").strip(),
            "source": ((r.get("source") or {}).get("name") or "").strip(),
            "url": (r.get("links") or {}).get("original") or "",
        } for r in reports[:12]],
    }
