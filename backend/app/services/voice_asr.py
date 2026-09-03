"""语音转写 —— 把一路 16 kHz PCM 流式送到 qwen realtime，收回文字。

## 会话必须配成「只转写、不对话」

这个模型默认是**对话**模型，不是转写模型。实测（2026-08-28，本机，真实握手）
默认会话是：

    {"modalities": ["text", "audio"], "voice": "longanqian",
     "input_audio_transcription": {"model": "fun-asr"},
     "turn_detection": {"type": "server_vad", "silence_duration_ms": 800}}

你说一句「The quick brown fox...」，它一边给你转写，一边**回你一句话**：
「That's the famous pangram! It's a fun sentence because it uses every letter」——
还按 usage 计费（一轮 input_tokens 358，其中 text_tokens 287 是累积的会话上下文，
output_tokens 22 是我们扔掉的回话）。一小时的会有几百轮，那个 text_tokens 会一直涨。

试过三条压制路线，只有第三条有效：

1. ``modalities: ["text"]`` —— 只是不合成语音了，**照样回话**。
2. ``turn_detection.create_response: false`` —— **不支持**。``session.updated``
   回来的 turn_detection 里根本没有这个字段，模型照样回话。
3. ``turn_detection: None`` + 自己发 ``input_audio_buffer.commit`` +
   **永不发 response.create** —— 实测 ``response.*`` 事件一个都不出现，彻底干净。

顺带记一笔：URL 上加 ``intent=transcription``（OpenAI 那套转写专用模式）在这里
**反而更糟**。``transcription_session.update`` 被拒（"Unsupported or unknown event
type"），于是会话退回默认值，开始吐 ``response.audio.delta`` —— 真的在合成语音。

## 增量文字在 stash 字段里

``conversation.item.input_audio_transcription.delta`` 的形状是：

    {"text": "", "stash": "The quick brown fox jump", ...}

**文字在 stash，text 恒为空。** 按 text 读会永远拿到空串，看起来像模型坏了。
而且 stash 是**滑动的不稳定假设**，会自我纠正也会退回去 —— 实测中间出现过
``' over the lazy dog. This is a testof theECREC P'`` 这种，几百毫秒后就修好了。
所以 stash 只能当「灰色临时文字」显示，落账一律以
``...transcription.completed`` 的 ``transcript`` 为准。

## 为什么按静音提交，而不是定时提交

试过每 2.5 秒提交一次：**所有增量被堵到最后一刻才一起吐出来**，而且多出一段
空转写。定时提交把流式变成了批式，等于把 realtime 唯一的优势抹掉。

现在改成：客户端按 RMS 判静音，说完一句停下来才提交。附带两个好处 ——
提交点落在自然停顿上不切词；静音期间**一个字节都不往云端发**（还带 300 ms
预卷，免得把字头吃掉）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from ..config import settings
from .voice_capture import SAMPLE_RATE, Chunk

log = logging.getLogger("voice.asr")

# 预卷：静音期不外发，但要留住说话开头那一下，否则每句都少半个字。
# 300 ms 是常规做法，也够覆盖 RMS 判定本身的一片延迟。
_PREROLL_S = 0.3
# 一段最多攒多少**秒音频**就强制提交。
#
# 这不是我们自己定的保守值，**服务端有硬限制**（实测报错原文）：
#
#     Input audio buffer exceeded maximum duration (30s).
#     Please commit or clear the buffer.
#
# 超了之后 append 被逐条拒绝，但**转写照样会返回一段看起来完整的文字** ——
# 实测 53 秒音频只在结尾提交一次，拿回来的是断在句子中间的 433 个字符，
# 没有任何迹象表明后面 20 多秒被丢了。这是最糟的失败方式：静默丢内容、
# 结果却像是完整的。所以这个上限是**正确性要求**，不是优化。
#
# 25 而不是 30：留 5 秒余量。边界上再撞一次就是丢内容，不值得为 5 秒省这点。
#
# **必须按「已追加的音频秒数」算，不能按墙钟算。** 两者在这个功能里天生不等：
# 静音期我们不追加，所以墙钟总是走得比音频快；而连续说话时两者又几乎相等 ——
# 也就是说用墙钟量的话，平时提早提交（无害），最需要它生效的连续讲话场景里
# 却刚好压在服务端的线上（有害）。
_MAX_SEGMENT_AUDIO_S = 25.0
# 掉线重连次数。会议可能开一小时，中间抖一下不该让整场录音报废。
_RECONNECT_TRIES = 5
# 到点主动换一条连接。
#
# 服务端会在「180 秒没产生 response」时单方面关掉会话（实测报错原文）：
#
#     code=response_idle_timeout
#     Your session was closed because no response was generated for 180 seconds.
#
# 而我们**永远不会产生 response** —— 整个「只转写不对话」的设计就建立在
# turn_detection=None + 从不发 response.create 之上（见模块头注）。提交音频
# 不算 response：实测喂 240 秒纯静音，181 秒准时被关。
#
# 也就是说**任何超过 3 分钟的录音都必然撞上**，这不是偶发网络抖动，是设计
# 后果。既然回不了话，就只能在撞线之前自己把连接换掉。
#
# 140 留 40 秒余量：够容纳一段最长 _MAX_SEGMENT_AUDIO_S(25s) 的在途音频，
# 加上等它定稿回来的时间。换连接对上层透明 —— 采集线程照旧往队列投，
# _pump 重启后接着取，一片音频都不丢。
_RECYCLE_S = 140.0


@dataclass
class Segment:
    """一段定稿的转写。``at_s`` 是这段话开始时距开录多少秒，用来和另一路合并。"""
    at_s: float
    text: str
    kind: str = ""


@dataclass
class RouteState:
    """一路转写的对外可见状态。页面直接渲染这个。"""
    kind: str
    label: str
    segments: list[Segment] = field(default_factory=list)
    provisional: str = ""          # 灰色临时文字（stash），随时会变
    error: str = ""
    connected: bool = False
    sent_seconds: float = 0.0      # 真正发出去的音频秒数（不含静音）
    committed: int = 0


def _api_key() -> str:
    """audio 的 key 留空就复用 text 的 —— 同一个 dashscope key，别让人填两遍。"""
    return (settings.audio_model_api_key or settings.text_model_api_key or "").strip()


def _ws_url() -> str:
    sep = "&" if "?" in settings.audio_model_ws_url else "?"
    return "%s%smodel=%s" % (settings.audio_model_ws_url, sep, settings.audio_model)


_SESSION_PATCH = {
    "modalities": ["text"],
    "input_audio_format": "pcm16",
    "input_audio_transcription": {"model": "fun-asr"},
    # None 而不是省略：省略的话服务端保留默认的 server_vad，就会自动提交并回话。
    "turn_detection": None,
}


class Transcriber:
    """一路音频 → 一条 websocket → 文字。

    生命周期由 services/voice.py 的会话管起来。采集线程通过 ``feed()`` 投喂，
    是跨线程的，所以内部用 ``call_soon_threadsafe`` 交回事件循环，不做轮询。
    """

    def __init__(self, kind: str, label: str) -> None:
        self.state = RouteState(kind=kind, label=label)
        self._q: asyncio.Queue[Chunk | None] = asyncio.Queue(maxsize=2000)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._t0 = 0.0
        self._stopping = False
        # 每次提交时把「这段是几秒开始的」压进来，收到 completed 时弹出去。
        # 不能只存一个"当前段起点"：completed 是异步回来的，很可能下一段已经
        # 开始说了才收到上一段的定稿，那样时间戳会错位 —— 两路合并时就体现为
        # 「对方」那句被排到「我」那句前面。FIFO 顺序天然对得上，因为提交是
        # 顺序发的、定稿也是顺序回的。
        self._starts: deque[float] = deque()
        # reader 退出 = 这条连接没了。_pump 靠它发现「空闲时被单方面关掉」，
        # 见 _pump 里那条 "dead" 的注释。
        self._reader_done = asyncio.Event()

    # ── 对外 ────────────────────────────────────────────────────────────

    def feed(self, chunk: Chunk) -> None:
        """从采集线程调。**绝不阻塞采集线程** —— 队列满了就丢这一片。

        丢片好过卡住采集：卡住会让 WASAPI 缓冲溢出，之后的音频整段错乱，
        而丢一片 10 ms 只是少几个字。真丢到了会记在日志里。
        """
        loop = self._loop
        if loop is None or self._stopping:
            return

        def put() -> None:
            try:
                self._q.put_nowait(chunk)
            except asyncio.QueueFull:
                log.warning("voice %s queue full, dropping %d frames", self.state.kind, chunk.frames)

        try:
            loop.call_soon_threadsafe(put)
        except RuntimeError:
            pass          # 循环已经关了，正在收尾

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._t0 = time.monotonic()
        self._task = asyncio.create_task(self._run(), name="voice-asr-%s" % self.state.kind)

    async def stop(self) -> None:
        """收尾：让已经攒着的音频有机会提交完，再关连接。"""
        self._stopping = True
        try:
            self._q.put_nowait(None)          # 收尾哨兵
        except asyncio.QueueFull:
            pass
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except asyncio.TimeoutError:
                self._task.cancel()
                log.warning("voice %s did not finish in 15s, cancelled", self.state.kind)
            except Exception:  # noqa: BLE001
                pass
        self.state.connected = False

    # ── 内部 ────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        key = _api_key()
        if not key:
            self.state.error = "没配模型 API key（TEXT_MODEL_API_KEY 或 AUDIO_MODEL_API_KEY）。"
            return
        try:
            from websockets.asyncio.client import connect  # noqa: PLC0415
        except Exception as e:  # noqa: BLE001
            self.state.error = "缺 websockets 库：%s" % e
            return

        url, tries = _ws_url(), 0
        while not self._stopping and tries <= _RECONNECT_TRIES:
            try:
                async with connect(url, additional_headers={"Authorization": "bearer " + key},
                                   open_timeout=20, close_timeout=5, max_size=None) as ws:
                    self.state.connected = True
                    self.state.error = ""
                    tries = 0            # 连上了就把重试计数清掉
                    await ws.send(json.dumps({"type": "session.update", "session": _SESSION_PATCH}))
                    self._reader_done = asyncio.Event()
                    reader = asyncio.create_task(self._reader(ws))
                    try:
                        why = await self._pump(ws)
                    finally:
                        reader.cancel()
                if self._stopping or why == "done":
                    break
                if why == "dead":
                    # 我们没在发东西的时候连接悄悄没了。当成一次意外掉线记账，
                    # 保住重试预算 —— 否则服务端持续拒绝时会退化成无限重连。
                    self.state.connected = False
                    tries += 1
                    if tries > _RECONNECT_TRIES:
                        self.state.error = ("转写连接反复中断，重连 %d 次都没成功。"
                                            % _RECONNECT_TRIES)
                        log.warning("voice %s gave up after repeated drops", self.state.kind)
                        break
                    await asyncio.sleep(min(2.0 * tries, 8.0))
                if why == "recycle":
                    log.info("voice %s recycling ws after %.0fs (avoid server 180s idle kill)",
                             self.state.kind, _RECYCLE_S)
                # why == "recycle"：计划内换连接，直接进下一轮重连。
                # 这里**故意不把 connected 置 False** —— 换连接不到一秒就完成、
                # 音频一片没丢，让状态栏每 140 秒闪一次「未连接」只会让人以为出事了。
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.state.connected = False
                tries += 1
                if self._stopping:
                    break
                if tries > _RECONNECT_TRIES:
                    self.state.error = "转写连接断了，重连 %d 次都没成功：%s" % (_RECONNECT_TRIES, e)
                    log.warning("voice %s gave up: %s", self.state.kind, e)
                    break
                self.state.error = "转写连接断了，正在重连（第 %d 次）：%s" % (tries, e)
                log.warning("voice %s reconnect %d: %s", self.state.kind, tries, e)
                await asyncio.sleep(min(2.0 * tries, 8.0))
        self.state.connected = False

    async def _reader(self, ws) -> None:
        try:
            await self._read_loop(ws)
        finally:
            # 无论是正常退出、连接被关、还是被 cancel，都要点亮 —— _pump 只认这个。
            self._reader_done.set()

    async def _read_loop(self, ws) -> None:
        while True:
            try:
                ev = json.loads(await ws.recv())
            except Exception:  # noqa: BLE001
                return
            t = ev.get("type") or ""
            if t == "conversation.item.input_audio_transcription.delta":
                # 文字在 stash，不在 text。见模块头注。
                self.state.provisional = (ev.get("stash") or ev.get("text") or "").strip()
            elif t == "conversation.item.input_audio_transcription.completed":
                text = (ev.get("transcript") or "").strip()
                self.state.provisional = ""
                at = self._starts.popleft() if self._starts else 0.0
                # 空转写要丢掉：提交了一段全静音就会得到一个空 transcript
                # （实测每 2.5 秒定时提交时就冒出来过）。但**起点照样要弹出**，
                # 否则后面每一段的时间戳都会错开一位。
                if text:
                    self.state.segments.append(
                        Segment(at_s=at, text=text, kind=self.state.kind))
            elif t == "error":
                err = ev.get("error") or ev
                msg = str(err.get("message") if isinstance(err, dict) else err)
                if "maximum duration" in msg:
                    # 这条特别标出来：它意味着**这一段有音频被服务端丢掉了**，
                    # 而不是某次请求失败。_MAX_SEGMENT_AUDIO_S 本该在撞线前先提交，
                    # 走到这里说明那个上限失效了（配置被改坏、或服务端把限制调小了）。
                    self.state.error = ("服务端缓冲超了 30 秒上限，这一段有内容被丢掉。"
                                        "请把 voice_asr._MAX_SEGMENT_AUDIO_S 调小。")
                else:
                    self.state.error = "转写报错：%s" % msg
                log.warning("voice %s server error: %s", self.state.kind,
                            json.dumps(ev, ensure_ascii=False)[:400])
            elif t.startswith("response."):
                # 不该出现。出现了说明「只转写不对话」那套配置失效了 ——
                # 那是在悄悄花钱，必须暴露出来而不是忍着。
                log.warning("voice %s unexpected %s —— 压制失效，正在产生对话回复",
                            self.state.kind, t)

    async def _pump(self, ws) -> str:
        """从队列取音频，按静音判定决定发不发、什么时候提交。

        返回值告诉 _run 为什么回来了：

        - ``done``    收到收尾哨兵，录音结束
        - ``recycle`` 到了 _RECYCLE_S，计划内换连接（无异常、不记重试）
        - ``dead``    连接没了，当作意外掉线处理
        """
        conn_t0 = time.monotonic()
        preroll: list[bytes] = []
        preroll_frames = 0
        preroll_cap = int(_PREROLL_S * SAMPLE_RATE)
        talking = False
        pending = False
        last_voice = time.monotonic()
        seg_started = 0.0
        seg_audio = 0.0            # 距上次提交，已经**追加**了多少秒音频
        thresh = max(0, int(settings.voice_silence_rms))
        hold = max(0.2, float(settings.voice_commit_silence_ms) / 1000.0)

        async def commit() -> None:
            nonlocal pending, talking, seg_audio
            if not pending:
                return
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            self._starts.append(seg_started)
            self.state.committed += 1
            pending = False
            talking = False
            seg_audio = 0.0        # 提交即清空服务端缓冲，计数跟着归零

        async def drain(timeout: float = 5.0) -> None:
            """等已提交段的定稿回来。_starts 空 = 全都对上了。

            换连接之前必须清干净：completed 是异步回来的，新连接不会补发旧连接
            欠下的定稿，留在 _starts 里的起点会让**之后每一段**的时间戳错开一格
            （两路合并时就表现为「对方」那句被排到「我」那句前面）。等不到就丢掉，
            宁可少一段的起点，也不能让后面全错位。
            """
            t0 = time.monotonic()
            while self._starts and time.monotonic() - t0 < timeout:
                await asyncio.sleep(0.05)
            self._starts.clear()

        while True:
            if self._reader_done.is_set():
                # 这条连接已经没了。
                #
                # **这个检查必须有。** 空闲的那一路（没人放声音时的回环）从不往外
                # 发东西，光靠 ws.send 抛异常永远发现不了掉线 —— 实测服务端在
                # 181 秒关掉会话后，这一路又「连着」空跑了 60 秒，一个字没有，
                # 页面还显示已连接。
                return "dead"

            if time.monotonic() - conn_t0 >= _RECYCLE_S:
                # 到点换连接，赶在服务端 180 秒看门狗开火之前。
                # 先把在途的一段提交、等定稿回来，换连接时手里就没有音频了。
                await commit()
                await drain()
                return "recycle"

            try:
                item = await asyncio.wait_for(self._q.get(), timeout=0.2)
            except asyncio.TimeoutError:
                # 没有新音频。回环上这很常见（没人放声音时端点压根不产生数据），
                # 所以「该不该提交」必须看墙钟，不能靠数静音片 —— 静音片根本不来。
                if pending and time.monotonic() - last_voice >= hold:
                    await commit()
                continue

            if item is None:                       # 收尾哨兵
                await commit()
                # 给服务端一点时间把最后那段的 completed 发回来
                await asyncio.sleep(2.5)
                return "done"

            now = time.monotonic()
            voiced = item.rms >= thresh
            if voiced:
                last_voice = now

            if not talking:
                if not voiced:
                    # 静音期：只留预卷，不外发。
                    preroll.append(item.pcm)
                    preroll_frames += item.frames
                    while preroll_frames > preroll_cap and len(preroll) > 1:
                        preroll_frames -= len(preroll[0]) // 2
                        preroll.pop(0)
                    if pending and now - last_voice >= hold:
                        await commit()
                    continue
                # 开口了：把预卷一起补上，字头才不会缺。
                talking = True
                seg_started = now - self._t0
                if preroll:
                    head = b"".join(preroll)
                    preroll.clear()
                    preroll_frames = 0
                    seg_audio += await self._append(ws, head)
            seg_audio += await self._append(ws, item.pcm)
            pending = True

            if seg_audio >= _MAX_SEGMENT_AUDIO_S:
                # 撞到服务端 30 秒缓冲上限之前先切一刀。见 _MAX_SEGMENT_AUDIO_S
                # 的注记：超了不会报错停下来，而是静默丢掉后面的音频。
                await commit()
            elif now - last_voice >= hold:
                # 说完一句、停下来了 —— 提交。
                #
                # **这一条必须在「有音频流入」这条路径上也做，不能只放在上面那个
                # wait_for 超时的分支里。** 两路的静音表现完全不同：
                #   回环   静音时端点压根不产生数据 → wait_for 会超时 → 那个分支够用
                #   麦克风 没人说话时照样持续产生静音片 → wait_for 永不超时
                # 只写超时那一个分支的话，麦克风这一路永远提交不了，只能等 30 秒
                # 硬切 —— 表现是「说完一句要等半分钟才出定稿」。
                await commit()

    async def _append(self, ws, pcm: bytes) -> float:
        """追加一片音频，返回这一片有多少秒 —— 调用方要用它数服务端缓冲。"""
        if not pcm:
            return 0.0
        await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                  "audio": base64.b64encode(pcm).decode("ascii")}))
        secs = len(pcm) / 2 / SAMPLE_RATE
        self.state.sent_seconds += secs
        return secs


# ── 批量：把一段已经有的音频转成文字 ────────────────────────────────────────

# 每块提交后等定稿的超时。25 秒音频实测 2~4 秒出定稿，给到 90 秒是留给
# 网络抖动和服务端排队，不是留给"可能永远不回"——真不回就得报错，不能干等。
_BATCH_CHUNK_TIMEOUT = 90.0


async def transcribe_pcm(pcm: bytes, *, on_progress=None) -> tuple[list[Segment], str]:
    """把一整段 16 kHz 单声道 PCM16 转成文字。返回 (段落列表, 错误说明)。

    ## 必须一块一块串行来

    实测把 53 秒音频以 11.6 倍实时推进去、连着提交三次，**定稿事件回来的顺序
    和提交顺序不一致** —— 拼出来的文本开头是脚本里的最后一句话。实时录音那条路
    走 1 倍速、提交间隔几十秒，先后关系天然成立；批量转写不行，得显式串行：
    提交一块 → 等它的 completed → 再提交下一块。

    代价是慢一点（等定稿的往返串起来了），换来的是顺序一定对。实测 53 秒音频
    串行下来仍然是数倍于实时，这个交换很划算。

    ## 为什么不复用 Transcriber

    Transcriber 是为「音频正在源源不断进来」设计的：静音门、预卷、按停顿提交。
    批量这边音频已经全在手上，切块由 voice_file.split_for_commit 按最安静的
    位置来切，两套逻辑合在一个类里只会互相碍事。共用的是同一份会话配置
    （_SESSION_PATCH）和同一套「不许对话」的纪律。
    """
    from .voice_file import split_for_commit  # noqa: PLC0415 —— 避免模块级循环导入

    key = _api_key()
    if not key:
        return [], "没配模型 API key（TEXT_MODEL_API_KEY 或 AUDIO_MODEL_API_KEY）。"
    try:
        from websockets.asyncio.client import connect  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        return [], "缺 websockets 库：%s" % e

    chunks = split_for_commit(pcm, _MAX_SEGMENT_AUDIO_S)
    if not chunks:
        return [], "没有可转写的音频。"

    segments: list[Segment] = []
    total_s = len(pcm) / 2 / SAMPLE_RATE
    done_s = 0.0
    err = ""
    pending: asyncio.Future | None = None
    loop = asyncio.get_running_loop()

    async def reader(ws) -> None:
        nonlocal err
        while True:
            try:
                ev = json.loads(await ws.recv())
            except Exception:  # noqa: BLE001
                if pending is not None and not pending.done():
                    pending.set_exception(ConnectionError("转写连接断了"))
                return
            t = ev.get("type") or ""
            if t == "conversation.item.input_audio_transcription.completed":
                if pending is not None and not pending.done():
                    pending.set_result((ev.get("transcript") or "").strip())
            elif t == "error":
                msg = str((ev.get("error") or {}).get("message") or ev)
                if "maximum duration" in msg:
                    # 走到这里说明切块失效了：服务端 30 秒上限被撞，
                    # 这一块**有内容被丢掉**，而转写照样会回一段看着完整的文字。
                    err = ("服务端缓冲超了 30 秒上限，有内容被丢掉。"
                           "请把 voice_asr._MAX_SEGMENT_AUDIO_S 调小。")
                else:
                    err = "转写报错：%s" % msg
                log.warning("batch transcribe error: %s", msg[:300])
            elif t.startswith("response."):
                # 不该出现：出现了就是「只转写不对话」那套配置失效，在悄悄花钱。
                log.warning("batch transcribe unexpected %s —— 压制失效", t)

    try:
        async with connect(_ws_url(), additional_headers={"Authorization": "bearer " + key},
                           open_timeout=20, close_timeout=5, max_size=None) as ws:
            await ws.recv()                                   # session.created
            await ws.send(json.dumps({"type": "session.update", "session": _SESSION_PATCH}))
            rt = asyncio.create_task(reader(ws))
            try:
                for chunk in chunks:
                    at_s = done_s
                    pending = loop.create_future()
                    # 按 100 ms 一片推。整块一次 send 也能过，但分片能让服务端
                    # 早点开始解码，首字更快出来。
                    step = SAMPLE_RATE * 2 // 10
                    for i in range(0, len(chunk), step):
                        await ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chunk[i:i + step]).decode("ascii")}))
                    await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    try:
                        text = await asyncio.wait_for(pending, timeout=_BATCH_CHUNK_TIMEOUT)
                    except asyncio.TimeoutError:
                        err = err or ("有一段等了 %d 秒没等到转写结果，后面的没再继续。"
                                      % int(_BATCH_CHUNK_TIMEOUT))
                        break
                    except ConnectionError as e:
                        err = err or str(e)
                        break
                    finally:
                        pending = None
                    if text:
                        segments.append(Segment(at_s=round(at_s, 1), text=text, kind="file"))
                    done_s += len(chunk) / 2 / SAMPLE_RATE
                    if on_progress:
                        try:
                            on_progress(done_s, total_s)
                        except Exception:  # noqa: BLE001
                            pass
            finally:
                rt.cancel()
    except Exception as e:  # noqa: BLE001
        # 已经转出来的段落照样返回 —— 半份逐字稿远好过什么都没有。
        err = err or "转写连接失败：%s" % e
        log.warning("batch transcribe failed: %s", e)

    return segments, err
