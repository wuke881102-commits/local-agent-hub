"""语音速记 —— 按下才录，录完出一条可留存的文字记录。

## 三种录法，每次开录都要选

- ``mic``       只录麦克风。出去的只有你自己的声音。
- ``loopback``  只录系统声音。出去的是这台电脑在响什么 —— 会议里**别人**的声音。
- ``both``      两路各开一条转写连接，说话人分离是白送的（麦克风那路一定是你，
                回环那路一定是别人），不需要声纹聚类。

**没有"记住上次选的"。** 这是刻意的：录别人的声音这件事不该靠一个被记住的
默认值悄悄发生，每次都必须有人主动选一次。

## 为什么按下才录，而不是像摘记那样定时

个人摘记是定时器驱动、无人值守的，那对音频是**设计错误** —— 无人值守地录音
是另一回事。所以：

- 只有显式 ``start()`` 才开录，装完不会自己开始录。
- 有硬上限 ``voice_max_minutes``（默认 90 分钟）。录音是后台线程，浏览器标签页
  关了它还在录，没有上限就意味着一次忘记停止 = 无声无息录一整天。
- 「个人摘记」可以把语音速记当**来源**，但它只读**已经录好的文字**，
  永远不会去触发一次录音。这条界线在 services/memo.py 那边也写着。

## 也可以传一个文件进来

除了当场录，还能把已有的音频 / 视频丢进来转写（手机录的会议、别人发来的录音、
下载的会议回放）。它和录音是两件不同的事，所以走不同的代码路径：

- 录音   音频源源不断来，靠静音门决定什么时候提交
- 文件   音频已经全在手上，按最安静的位置切块、**一块一块串行**提交

串行不是保守：实测并发提交时定稿事件回来的顺序和提交顺序不一致，拼出来的文本
开头会是文件里最后一句话。理由写在 services/voice_asr.py 的 transcribe_pcm 里。

文件同样**不写盘** —— 连 FastAPI 的 UploadFile 都不用（它超过 1 MB 就落临时目录）。

## 音频不落盘

任何一条代码路径都不把音频写到磁盘上，连开关都不提供。落盘的只有转写文字和
提炼结果（``DATA_ROOT/voice/notes.jsonl``）。和 outlook 那边「缓存只在内存」
是同一个取舍：内容过一遍就走，磁盘上不留副本。

## 一路挂了不拖累另一路

两路各自独立：麦克风开不起来（比如这台机器没有输入设备），回环那路照录。
和 memo 两个来源互不拖累同一条原则 —— 为了凑齐而整场不录，是把小故障放大成
彻底沉默。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
import uuid

from ..config import DATA_ROOT, settings
from ..llm import get_llm
from . import voice_capture as vc
from . import voice_file
from .voice_asr import Segment, Transcriber, transcribe_pcm

log = logging.getLogger("voice")

MODE_MIC = "mic"
MODE_LOOPBACK = "loopback"
MODE_BOTH = "both"
MODES = (MODE_MIC, MODE_LOOPBACK, MODE_BOTH)
# 传进来的文件也存成一条记录，mode 记成 file —— 它不是"录法"，
# 但记录列表里必须能一眼看出这条不是这台机器录的。
MODE_FILE = "file"

# 两路同时录时的说话人标签。只录一路时不加前缀 —— 一个人自己口述，
# 每行顶一个「我：」是噪音。
_LABEL = {vc.MIC: "我", vc.LOOPBACK: "对方"}

_VOICE_DIR = DATA_ROOT / "voice"
_NOTES_FILE = _VOICE_DIR / "notes.jsonl"

_SYSTEM = ("你在整理一段口语转写。转写来自语音识别，会有错字、重复、口头语。"
           "你的任务是整理，不是创作：不要补充转写里没有的信息，不要替说话人下结论。"
           "拿不准的地方保留原话。全程用中文回答。")

_state: dict = {
    "active": False,
    "mode": "",
    "started_at": "",
    "elapsed_s": 0,
    "max_minutes": 0,
    "error": "",
    "finishing": False,
}
_routes: list[tuple[vc.Capture, Transcriber]] = []
# 传文件转写的进度。单独一份状态，和录音互不干扰 —— 两者可以同时进行
# （不共用设备，各开自己的 websocket），但同一时刻只允许一个文件任务。
_file_job: dict = {
    "active": False, "filename": "", "stage": "", "seconds": 0.0,
    "done_s": 0.0, "error": "",
}
_file_lock = asyncio.Lock()
_t0 = 0.0
_cap_task: asyncio.Task | None = None
_lock = asyncio.Lock()


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ── 设备 ─────────────────────────────────────────────────────────────────

def devices() -> dict:
    """给页面用的设备清单。设备为空时 note 里说清楚为什么。"""
    try:
        return vc.list_devices()
    except vc.CaptureError as e:
        return {"mic": [], "loopback": [], "note": str(e),
                "mic_available": False, "loopback_available": False}


# ── 录音会话 ─────────────────────────────────────────────────────────────

def _kinds_for(mode: str) -> tuple[str, ...]:
    if mode == MODE_MIC:
        return (vc.MIC,)
    if mode == MODE_LOOPBACK:
        return (vc.LOOPBACK,)
    return (vc.MIC, vc.LOOPBACK)


async def start(mode: str, mic_device: int | None = None,
                loopback_device: int | None = None) -> dict:
    """开录。已经在录就直接报错，不静默替换掉上一场。"""
    global _t0, _cap_task

    if mode not in MODES:
        raise ValueError("未知的录音方式：%r（只能是 mic / loopback / both）" % mode)

    async with _lock:
        if _state["active"]:
            raise ValueError("已经在录了。要换一种录法请先停止当前录音。")

        _routes.clear()
        _state.update({"active": True, "mode": mode, "started_at": _now_iso(),
                       "elapsed_s": 0, "max_minutes": int(settings.voice_max_minutes),
                       "error": "", "finishing": False})
        _t0 = time.monotonic()

        failures: list[str] = []
        for kind in _kinds_for(mode):
            tr = Transcriber(kind, _LABEL[kind])
            tr.start()
            dev = mic_device if kind == vc.MIC else loopback_device
            cap = vc.Capture(kind, dev, on_chunk=tr.feed,
                             on_error=lambda m, s=tr.state: setattr(s, "error", m))
            cap.start()
            # 等它把设备打开（或失败）。不等的话页面会先看到"正在录音"，
            # 几秒后才冒出错误，用户已经开始说话了。
            await asyncio.to_thread(cap.settled.wait, 8.0)
            if cap.error:
                failures.append("%s：%s" % (_LABEL[kind], cap.error))
                tr.state.error = cap.error
                await tr.stop()
                continue
            _routes.append((cap, tr))

        if not _routes:
            _state.update({"active": False, "mode": "", "started_at": "",
                           "error": "；".join(failures) or "没有一路能开起来。"})
            raise ValueError(_state["error"])

        # 一路失败另一路成功：照录，但把失败原因如实挂在状态里。
        _state["error"] = "；".join(failures)
        _cap_task = asyncio.create_task(_watchdog(), name="voice-watchdog")
        log.info("voice start: mode=%s routes=%s", mode,
                 ",".join(c.kind for c, _ in _routes))
        return status()


async def _watchdog() -> None:
    """到点强停。见模块头注：没有上限的后台录音是不能接受的。"""
    limit = max(1, int(settings.voice_max_minutes)) * 60
    try:
        while _state["active"]:
            await asyncio.sleep(1.0)
            _state["elapsed_s"] = int(time.monotonic() - _t0)
            if _state["elapsed_s"] >= limit:
                log.warning("voice hit %d min cap, stopping", limit // 60)
                await stop(reason="到了 %d 分钟上限，自动停止。" % (limit // 60))
                return
    except asyncio.CancelledError:
        pass


def _merge(routes: list[Transcriber]) -> tuple[str, int]:
    """把各路的定稿段落按时间轴合成一份纯文本。

    只录一路时不加说话人前缀；两路时加，因为这时前缀是**真实信息**
    （哪句是你说的、哪句是对方说的），不是装饰。
    """
    segs: list[Segment] = []
    for tr in routes:
        segs.extend(tr.state.segments)
    segs.sort(key=lambda s: s.at_s)
    multi = len(routes) > 1
    lines = []
    for s in segs:
        if multi:
            lines.append("%s：%s" % (_LABEL.get(s.kind, s.kind), s.text))
        else:
            lines.append(s.text)
    return "\n".join(lines), len(segs)


async def _distill(transcript: str, mode: str) -> tuple[str, str, str]:
    """把转写交给文字模型提炼。返回 (摘要, 失败原因, 实际出结果的模型)。

    转写和提炼分两段做（不让 realtime 一并输出）是刻意的：realtime 挂了、
    提炼挂了，你至少还留着逐字稿 —— 而逐字稿才是要留的东西。

    **但"失败就静默退回"是错的。** 原先这里失败只写一行日志、返回空串，
    于是页面上「没有提炼」和「提炼失败了」长得一模一样 —— 用户看到一条只有
    逐字稿的记录，无从判断是模型挂了、超时了、还是这功能压根没做。
    所以失败原因必须带回去，存进记录、显示在页面上、并且能重试。
    """
    transcript = transcript.strip()
    if not transcript:
        return "", "转写是空的，没什么可提炼。", ""
    llm = get_llm()
    if getattr(llm, "mock", False):
        # 说清去哪看、改什么。**转写和提炼吃的不是同一份配置**：转写只要 key 本身
        # （websocket 地址是内置的），提炼还要 TEXT_MODEL_PROVIDER / BASE_URL
        # （或 Azure endpoint）成立。所以完全可能"能转写但提炼是 mock"。
        # 路径用运行时真实值拼出来，不写 %LOCALAPPDATA% 这种模板 —— 用户要的是
        # 「去改哪个文件」，而不是自己去展开一个环境变量。
        return "", ("文字模型没配好（当前是 mock / 离线状态），所以没有提炼。"
                    "去「系统诊断」看文字模型那一行；配置在 %s 里的 "
                    "TEXT_MODEL_API_KEY / TEXT_MODEL_PROVIDER / TEXT_MODEL_BASE_URL。"
                    % (DATA_ROOT / ".env")), ""
    ask = ("把下面这段口语转写整理成一条可留存的记录，用 Markdown。结构：\n"
           "1. 一句话说清这段在讲什么；\n"
           "2. 「要点」——分条，按事情分组，组名用转写里出现的具体名字，"
           "不要用「某个项目」「其他」这类占位词；\n"
           "3. 「待办」——只列转写里明确说要做的事，没有就不写这一节。\n"
           "识别错的词按上下文改回来，但**不要**替说话人补充没说过的内容。")
    if mode == MODE_BOTH:
        ask += "\n转写里「我：」是本人说的，「对方：」是其他人说的，整理时区分开。"
    elif mode == MODE_FILE:
        # 文件转写没有说话人分离，多半是会议录音，人不止一个。
        ask += ("\n这段来自一个音频文件，没有说话人标注，可能有多个人在说话。"
                "**不要猜谁说了哪句**，按内容组织就好。")
    prompt = "%s\n\n<<<转写开始>>>\n%s\n<<<转写结束>>>" % (ask, transcript)
    timeout = float(settings.voice_distill_timeout_s)

    async def attempt(model: str) -> tuple[str, str]:
        try:
            out = await llm.text_complete(
                prompt, system=_SYSTEM, json_mode=False, max_tokens=1200,
                timeout=timeout, retries=0, model=model)
        except Exception as e:  # noqa: BLE001
            log.warning("voice distill[%s] failed: %s: %s", model, type(e).__name__, e)
            if isinstance(e, TimeoutError) or "timeout" in str(e).lower():
                return "", "超时（%.0f 秒没返回）" % timeout
            return "", "%s: %s" % (type(e).__name__, str(e)[:160])
        out = (out or "").strip()
        return (out, "") if out else ("", "返回了空内容")

    # 主档先来 —— 它质量最好。但它**会瞬时卡死**：用户那条 235 字的转写复现出来，
    # qwen3.7-plus 挂了 300 秒以上；隔一会儿同一段内容 33 秒就出结果，连打两次都正常。
    main_model = llm.text_model
    out, err1 = await attempt(main_model)
    if out:
        return out, "", main_model

    # 主档不行就退快档。**这不是降级凑数**：实测同一段内容 deepseek-v4-flash
    # 只要 7.2 秒（主档 33 秒），"整理一段口语"这种活儿它完全够用。
    # 有个摘要远好过一条只有逐字稿的记录 —— 而调大超时只会让人干等更久然后一场空。
    fast_model = llm.text_model_fast
    if fast_model and fast_model != main_model:
        log.info("voice distill falling back to %s (%s failed: %s)", fast_model, main_model, err1)
        out, err2 = await attempt(fast_model)
        if out:
            return out, "", fast_model
        return "", ("提炼两档都没成：%s（%s）；%s（%s）。"
                    "逐字稿已存下，可以点「重新提炼」再试。"
                    % (main_model, err1, fast_model, err2)), ""

    return "", ("提炼失败：%s（%s）。逐字稿已存下，可以点「重新提炼」再试。"
                % (main_model, err1)), ""


async def stop(reason: str = "") -> dict:
    """停录 → 合并 → **先存逐字稿** → 提炼 → 回填。返回那条记录。

    **不抛异常。** 录了十分钟结果因为提炼失败整条丢掉，是最不能接受的失败。

    ## 为什么 finishing 必须放在 try/finally 里

    这个函数最长要跑三分多钟（收尾 20 秒 + 提炼两档各 90 秒），全程页面上是
    「正在整理…」。这段时间里请求非常容易断：用户等不住刷新页面、关掉标签、
    或者干脆把浏览器关了 —— Starlette 会把这个 handler 的 task 取消，
    协程在某个 await 上抛 CancelledError 就地死掉。

    原先 finishing 只在正常返回的几条路径上清掉，于是一旦被取消，
    **状态里永远留着 finishing=True**：卡片一直显示「正在整理…」，
    重启后端才能恢复，而用户完全无从判断是还在跑还是已经卡死。
    放进 finally 才对 —— CancelledError 也会走 finally。

    ## 为什么先存后提炼

    同一个原因。原先是「提炼完 → 组装 note → 存盘」，在提炼那最长 180 秒里
    被取消，`_routes` 早已 clear，**这一整场的逐字稿就彻底没了**。
    这正是本模块头注要防的那种失败。所以先把带逐字稿的记录落盘，
    提炼完再回填；中途断了，记录还在，点「重新提炼」就能补。
    """
    global _cap_task

    async with _lock:
        # finishing 也算「有事要收尾」：它为 True 而 routes 已空，说明上一次
        # stop 被打断过。这时候再按一次停止应该能把状态收干净，而不是回一句
        # 「当前没有在录音」然后把卡片继续卡在「正在整理」。
        if not _state["active"] and not _routes and not _state["finishing"]:
            return {"ok": True, "saved": False, "message": "当前没有在录音。"}

        _state["finishing"] = True
        _state["active"] = False
        try:
            return await _stop_locked(reason)
        finally:
            # 无论正常返回、抛异常、还是被取消，卡片都必须从「正在整理」出来。
            _state.update({"mode": "", "started_at": "", "elapsed_s": 0,
                           "finishing": False})
            _routes.clear()


async def _stop_locked(reason: str) -> dict:
    """stop() 的正体。调用方已持锁、已置 finishing、并负责在 finally 里清干净。"""
    global _cap_task

    if _cap_task and not _cap_task.done():
        _cap_task.cancel()
    _cap_task = None

    # 先停采集（不再有新音频进来），再停转写（让攒着的那段提交完）。
    for cap, _ in _routes:
        cap.stop()
    for cap, _ in _routes:
        await asyncio.to_thread(cap.join, 5.0)
    trs = [tr for _, tr in _routes]
    for tr in trs:
        await tr.stop()

    transcript, n_seg = _merge(trs)
    secs = int(time.monotonic() - _t0)
    mode = _state["mode"]
    errs = [tr.state.error for tr in trs if tr.state.error]
    if _state["error"]:
        errs.insert(0, _state["error"])
    _state["error"] = "；".join(errs)

    if not trs:
        # 一路都没有 —— 说明这次进来只是为了把上一次被打断的 finishing 收干净。
        # 走下面那条「没转出文字」的话术会去怪麦克风音量，是在误导。
        return {"ok": True, "saved": False,
                "message": "上一次停止没走完（多半是当时刷新了页面），状态已经收干净了。"
                           "那一场的逐字稿如果转出过，就在下面的记录里。"}

    if not transcript.strip():
        msg = "这次没转出任何文字。"
        if errs:
            msg += "（%s）" % "；".join(errs)
        elif mode == MODE_LOOPBACK:
            msg += "系统声音那一路只有在电脑真的在放声音时才有数据。"
        else:
            msg += "可能是没说话，或者麦克风音量太低（可调 VOICE_SILENCE_RMS）。"
        return {"ok": True, "saved": False, "message": msg}

    # ── 先落盘，再提炼 ──
    # 提炼最长 180 秒（两档各 90）。这段时间里请求一断，之前的写法会连逐字稿
    # 一起丢。所以这里先把带逐字稿的记录存下来，distill_error 先写成一句
    # 「还没跑完」——万一真被打断，用户看到的就是这句加一个能点的「重新提炼」，
    # 而不是一场空。
    note = {
        "id": uuid.uuid4().hex[:12],
        "created_at": _now_iso(),
        "mode": mode,
        "seconds": secs,
        "segments": n_seg,
        "transcript": transcript,
        "summary": "",
        "error": "；".join(errs),
        "distill_error": "提炼还没跑完（可能是停止时被打断）。逐字稿已经存下，点「重新提炼」补上。",
        "distill_model": "",
    }
    _save(note)
    log.info("voice stop: %ds, %d segments, %d chars —— 逐字稿已存 %s，开始提炼",
             secs, n_seg, len(transcript), note["id"])

    summary, distill_err, distill_model = await _distill(transcript, mode)
    note["summary"] = summary
    note["distill_error"] = distill_err
    note["distill_model"] = distill_model
    if not _replace_note(note):
        # 落盘那一步成了、回填却找不到这条，只可能是文件被外部改过。
        # 不静默：至少让日志能对上。
        log.warning("voice stop: 回填提炼结果时找不到记录 %s", note["id"])
    log.info("voice stop: %s 提炼%s（%s）", note["id"],
             "完成" if summary else "失败", distill_model or distill_err[:60])

    out = {"ok": True, "saved": True, "note": note}
    if reason:
        out["message"] = reason
    if distill_err:
        out["message"] = (out.get("message", "") + distill_err).strip()
    return out


def status() -> dict:
    out = dict(_state)
    if _state["active"]:
        out["elapsed_s"] = int(time.monotonic() - _t0)
    else:
        # 没在录时 _state 里的 max_minutes 还是初始的 0，而页面在**开录之前**
        # 就要告诉用户「最长多久自动停」—— 报 0 等于告诉他没有上限，正好说反了。
        # 在录时报的是那一场开始时锁定的值（中途改配置不影响进行中的录音）。
        out["max_minutes"] = int(settings.voice_max_minutes)
    # 文件上限也给出去：页面上那两个数字必须跟着配置走，不能写死 ——
    # 写死的话改了 VOICE_FILE_MAX_MB 界面就在骗人。
    out["file_max_mb"] = int(settings.voice_file_max_mb)
    out["file_max_minutes"] = int(settings.voice_file_max_minutes)
    # 提炼到底能不能用 —— **必须在开录之前就知道**。
    # 录完一场会才发现"只有逐字稿、没有提炼"，是这个功能最难受的失败方式；
    # 而这件事在开录前就是已知的（文字客户端建不起来就是 mock）。
    try:
        out["distill_available"] = not getattr(get_llm(), "mock", False)
    except Exception:  # noqa: BLE001
        out["distill_available"] = False
    out["routes"] = [{
        "kind": tr.state.kind,
        "label": tr.state.label,
        "connected": tr.state.connected,
        "provisional": tr.state.provisional,
        "segments": [{"at_s": round(s.at_s, 1), "text": s.text} for s in tr.state.segments],
        "sent_seconds": round(tr.state.sent_seconds, 1),
        "committed": tr.state.committed,
        "error": tr.state.error,
    } for _, tr in _routes]
    out["modes"] = list(MODES)
    out["file_job"] = dict(_file_job)
    return out


# ── 传一个文件进来转写 ───────────────────────────────────────────────────

async def transcribe_file(data: bytes, filename: str = "") -> dict:
    """把一段已有的音频 / 视频转成一条记录。**不抛异常**，结论写在返回里。

    ## 音频从头到尾没写盘

    调用方（routes/voice.py）收的是**原始请求体**，不是 FastAPI 的 UploadFile ——
    后者底层是 SpooledTemporaryFile，超过 1 MB 就落到系统临时目录，那样一段会议
    录音会先在磁盘上完整存在一份，而我们"处理完即删"只能删自己那份、删不掉它的。
    这里字节进内存、解码进内存、转写完出作用域。理由和这个模块头注的
    「音频不落盘」是同一条，不是两套说法。

    ## 为什么解码放到线程里

    PyAV 解一小时的文件要跑一会儿，而**录音可能正在同时进行**。放在事件循环里
    会把采集线程投喂的那条队列一起卡住。to_thread 让 PyAV 在 C 里干活时把 GIL
    放开，两边互不影响。
    """
    async with _file_lock:
        if _file_job["active"]:
            return {"ok": False, "saved": False, "message": "已经有一个文件在转写了，等它完成。"}

        name = (filename or "").strip() or "(未命名)"
        limit = max(1, int(settings.voice_file_max_mb)) * 1024 * 1024
        if len(data) > limit:
            return {"ok": False, "saved": False,
                    "message": "文件 %.1f MB，超过上限 %d MB。" % (
                        len(data) / 1024 / 1024, settings.voice_file_max_mb)}

        _file_job.update({"active": True, "filename": name, "stage": "decoding",
                          "seconds": 0.0, "done_s": 0.0, "error": ""})
        try:
            try:
                pcm, secs, meta = await asyncio.to_thread(
                    voice_file.probe_and_decode, data, name)
            except voice_file.DecodeError as e:
                _file_job["error"] = str(e)
                return {"ok": False, "saved": False, "message": str(e)}

            cap_s = max(1, int(settings.voice_file_max_minutes)) * 60
            if secs > cap_s:
                msg = ("这个文件有 %.0f 分钟，超过上限 %d 分钟。"
                       "上限是为了别让一次误操作变成几小时的模型调用。" % (
                           secs / 60, settings.voice_file_max_minutes))
                _file_job["error"] = msg
                return {"ok": False, "saved": False, "message": msg}

            _file_job.update({"stage": "transcribing", "seconds": round(secs, 1)})

            def prog(done: float, total: float) -> None:
                _file_job["done_s"] = round(done, 1)
                _file_job["seconds"] = round(total, 1)

            segs, err = await transcribe_pcm(pcm, on_progress=prog)
            del pcm            # 尽早撒手，别让一小时的 PCM 在提炼期间还占着内存

            transcript = "\n".join(s.text for s in segs).strip()
            if not transcript:
                msg = err or "这个文件没转出任何文字（可能整段都没有人说话）。"
                _file_job["error"] = msg
                return {"ok": False, "saved": False, "message": msg}

            # ── 先落盘，再提炼 ──
            # 和 stop() 同一个道理，而这里更要紧：转写这一步已经把整段音频发出去、
            # 花了钱也花了几分钟，提炼再最多 180 秒。上传请求在这段时间里一断
            # （等不住刷新页面最常见），原先的写法会把**刚转出来的整份逐字稿**
            # 一起丢掉，等于那趟模型调用白花。所以先存，提炼完回填。
            note = {
                "id": uuid.uuid4().hex[:12],
                "created_at": _now_iso(),
                "mode": MODE_FILE,
                "seconds": int(secs),
                "segments": len(segs),
                "transcript": transcript,
                "summary": "",
                "error": err,
                "distill_error": "提炼还没跑完（可能是转写时页面断开了）。逐字稿已经存下，点「重新提炼」补上。",
                "distill_model": "",
                "source_file": name,
                "source_codec": meta.get("codec") or "",
            }
            _save(note)
            log.info("voice file: %s 逐字稿已存 %s（%d 字），开始提炼",
                     name, note["id"], len(transcript))

            _file_job["stage"] = "distilling"
            summary, distill_err, distill_model = await _distill(transcript, MODE_FILE)
            note["summary"] = summary
            note["distill_error"] = distill_err
            note["distill_model"] = distill_model
            if not _replace_note(note):
                log.warning("voice file: 回填提炼结果时找不到记录 %s", note["id"])
            _file_job["stage"] = "done"
            log.info("voice file done: %s %.1fs -> %d segments, %d chars, distilled=%s",
                     name, secs, len(segs), len(transcript), bool(summary))
            out = {"ok": True, "saved": True, "note": note}
            msgs = []
            if err:
                msgs.append(err)
            if distill_err:
                msgs.append(distill_err)
            if msgs:
                out["message"] = "；".join(msgs)
            return out
        except Exception as e:  # noqa: BLE001
            log.exception("voice file transcribe crashed")
            _file_job["error"] = "%s: %s" % (type(e).__name__, e)
            return {"ok": False, "saved": False, "message": _file_job["error"]}
        finally:
            _file_job["active"] = False


# ── 记录存取 ─────────────────────────────────────────────────────────────

def _save(note: dict) -> None:
    try:
        _VOICE_DIR.mkdir(parents=True, exist_ok=True)
        with _NOTES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(note, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        # 存不下来也不能把已经录到的东西弄丢 —— 调用方拿到的返回里带着全文。
        log.warning("voice note save failed: %s", e)


def _load_all() -> list[dict]:
    if not _NOTES_FILE.exists():
        return []
    out = []
    try:
        with _NOTES_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue      # 坏行跳过，不让一行残缺毁掉整个列表
    except Exception as e:  # noqa: BLE001
        log.warning("voice notes read failed: %s", e)
    return out


def list_notes(limit: int = 50) -> list[dict]:
    """列表视图：不带逐字稿全文（可能很长），只给个长度。"""
    items = _load_all()[-max(1, limit):]
    items.reverse()
    return [{k: v for k, v in n.items() if k != "transcript"}
            | {"transcript_chars": len(n.get("transcript") or "")} for n in items]


def get_note(note_id: str) -> dict | None:
    for n in reversed(_load_all()):
        if n.get("id") == note_id:
            return n
    return None


def _replace_note(note: dict) -> bool:
    """原地替换一条记录。整文件重写 —— jsonl 没法就地改一行。

    记录数量是「你手动录过多少次」，几十上百条量级，重写整个文件的代价可以忽略。
    """
    items = _load_all()
    hit = False
    for i, n in enumerate(items):
        if n.get("id") == note.get("id"):
            items[i] = note
            hit = True
            break
    if not hit:
        return False
    try:
        _VOICE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _NOTES_FILE.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for n in items:
                f.write(json.dumps(n, ensure_ascii=False) + "\n")
        tmp.replace(_NOTES_FILE)
    except Exception as e:  # noqa: BLE001
        log.warning("voice note replace failed: %s", e)
        return False
    return True


def delete_note(note_id: str) -> bool:
    items = _load_all()
    kept = [n for n in items if n.get("id") != note_id]
    if len(kept) == len(items):
        return False
    try:
        _VOICE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _NOTES_FILE.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for n in kept:
                f.write(json.dumps(n, ensure_ascii=False) + "\n")
        tmp.replace(_NOTES_FILE)
    except Exception as e:  # noqa: BLE001
        log.warning("voice note delete failed: %s", e)
        return False
    return True


async def redistill(note_id: str) -> dict:
    """对一条已有记录重新跑提炼，原地更新。

    **这是「转写和提炼分两段」这个设计真正的兑现点。** 逐字稿已经在盘上了，
    提炼失败（超时、模型抖动、当时没配好 key）不该让人重录一遍 —— 那份音频
    早就不存在了，重录也录不回同一场会。
    """
    note = get_note(note_id)
    if note is None:
        raise ValueError("没有这条记录。")
    transcript = (note.get("transcript") or "").strip()
    if not transcript:
        raise ValueError("这条记录没有逐字稿，无法提炼。")

    summary, err, model = await _distill(transcript, note.get("mode") or "")
    note["summary"] = summary
    note["distill_error"] = err
    note["distill_model"] = model
    note["distilled_at"] = _now_iso()
    if not _replace_note(note):
        raise ValueError("写回记录失败。")
    log.info("voice redistill %s: %s", note_id, "ok" if summary else ("failed: %s" % err))
    return {"ok": bool(summary), "note": note, "message": err}


def notes_in_window(lo: dt.datetime, hi: dt.datetime) -> list[dict]:
    """给「个人摘记」用：窗口内录好的记录。

    **只读已经录好的**，绝不触发录音。摘记是定时无人值守跑的，
    在那种上下文里开麦克风是不能接受的。
    """
    out = []
    for n in _load_all():
        try:
            ts = dt.datetime.fromisoformat(n.get("created_at") or "")
        except Exception:  # noqa: BLE001
            continue
        if lo <= ts <= hi:
            out.append(n)
    return out
