"""语音采集 —— 麦克风与系统回环两路，都归一到 16 kHz 单声道 PCM16。

## 为什么是 pyaudiowpatch，不是 sounddevice

sounddevice 0.5.6 捆的 PortAudio V19.7.0-devel 里，``WasapiSettings`` 的签名是
``(exclusive, auto_convert, explicit_sample_format)`` —— **没有 loopback 这个参数**。
它压根不提供 WASAPI 回环，而回环是「录到会议里对方声音」的唯一途径。
pyaudiowpatch 是 PyAudio 的分支，专门补了回环，且普通麦克风照样能开 ——
所以两路共用一个依赖、一套设备枚举、一套读法，而不是拼两个库。

## 绝对不用阻塞式 read()

WASAPI 回环有个不看文档就一定会踩的坑：**渲染端点空闲时不产生任何数据包**，
而不是产生静音包。本机实测（Windows Server，无声卡，音频靠 RDP 重定向）：

    p.open(...)              0.28 s   成功
    get_read_available()     0        恒为 0
    read(1024)               永久阻塞，25 秒看门狗强杀

所以采集循环只能轮询 ``get_read_available()``：有多少读多少，没有就睡 10 ms。
实测轮询版跑 4 秒 606 次轮询、364 次空转，从不阻塞；且在播放 2 秒 440 Hz 正弦时
抓到 peak amplitude=12000（正是生成时的幅度，比特级一致）。

顺带白捡一个好处：没人放声音时我们一个字节都不往云端发 —— 静音不花钱，
不需要额外做 VAD 来省这笔。

这条「不许阻塞」的纪律和 services/outlook.py 是同一类：那边是 COM 属性读取会
无限期挂住，这边是音频端点空闲会无限期挂住。都没法从外部取消，只能不发那个调用。

## 重采样为什么是纯 Python

Python 3.13 删掉了 ``audioop``（捆绑运行时正是 3.13.3），而 numpy 要多 15 MB
安装包。实测纯 Python 的「立体声降混 + 抽点重采样」处理 2.52 秒音频花 130 ms
CPU，约单核 5.2% —— 16 kHz 这个量级完全够用，不值得为它多一个依赖。

抽点（取最近样本）而不是线性插值：fun-asr 对这点混叠不敏感，实测转写逐字正确。
"""
from __future__ import annotations

import logging
import threading
import time
from array import array
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("voice.capture")

# fun-asr 的原生采样率。**不是可调项** —— 实测 16 kHz 转写逐字正确，而服务端
# 不回显 input_audio_format，改了也没法验证，只能靠转写质量反推。
SAMPLE_RATE = 16000

MIC = "mic"
LOOPBACK = "loopback"
KINDS = (MIC, LOOPBACK)

# 轮询间隔。10 ms 是「不空转太狠」和「延迟看不出来」的折中：
# 16 kHz 下 10 ms = 160 个样本，人耳和 ASR 都察觉不到。
_POLL_SLEEP = 0.01
# 单次 read 的上限帧数。设备一次能给的量偶尔会很大（我们睡过头、或系统卡了一下），
# 分次读完而不是一口气要，避免一次分配出一个巨大的 buffer。
_READ_CAP = 8192
# 打开采集流用的缓冲大小。
_FRAMES_PER_BUFFER = 1024

# 枚举设备要短暂地 Pa_Initialize / Pa_Terminate，会和采集线程里那个实例并存。
# 加锁只为避免两个请求同时枚举时撞在一起 —— 不保护采集线程（它有自己的实例）。
_enum_lock = threading.Lock()


class CaptureError(RuntimeError):
    """采集侧的人话错误。调用方直接把 str(e) 给用户看。"""


def _pa():
    """惰性 import。

    和 services/outlook.py 同一个理由：装不上 / 没这个平台的机器要能正常启动，
    只是这个场景用不了，而不是整个后端起不来。pyaudiowpatch 是 Windows 专属，
    在别的平台上 import 就会失败。
    """
    try:
        import pyaudiowpatch  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        raise CaptureError(
            "没装上音频采集库（pyaudiowpatch）：%s。语音速记只在 Windows 上可用。" % e
        ) from e
    return pyaudiowpatch


@dataclass
class Chunk:
    """一片已经归一到 16 kHz 单声道 PCM16 的音频。

    ``rms`` 是这一片的均方根幅度。它在转换过程里顺手算出来（反正要遍历一遍样本），
    给上层做「说完了没有」的静音判定用 —— 这样上层不必再遍历一次。
    """
    pcm: bytes
    rms: int
    frames: int


class _Resampler:
    """定点相位累加的抽点重采样 + 多声道降混。

    **相位必须跨片保留。** 每一片都从 0 重新开始会在片边界上重复或丢掉样本，
    单片听不出来，几分钟累积下来就是可听的节奏抖动，而且会让时长和真实时间错位
    （录 30 分钟对不上 30 分钟）。这里把不足一步的余量 ``pos`` 带到下一片。
    """

    def __init__(self, src_rate: int, channels: int) -> None:
        self.step = float(src_rate) / float(SAMPLE_RATE)
        self.channels = max(1, int(channels))
        self.pos = 0.0

    def feed(self, raw: bytes) -> Chunk:
        src = array("h")
        # 奇数字节（半个样本）会让 frombytes 直接抛。设备正常不会给，
        # 但真给了的话丢掉那一个字节，好过整条录音崩掉。
        usable = len(raw) - (len(raw) % 2)
        src.frombytes(raw[:usable])

        ch = self.channels
        frames = len(src) // ch
        if frames <= 0:
            return Chunk(b"", 0, 0)

        # 降混：多声道取平均。用 // 保持整数，避免 float 往返。
        if ch == 1:
            mono = src
        else:
            mono = array("h", bytes(frames * 2))
            for i in range(frames):
                base = i * ch
                mono[i] = sum(src[base:base + ch]) // ch

        pos = self.pos
        out = array("h")
        acc = 0
        n = 0
        while pos < frames:
            v = mono[int(pos)]
            out.append(v)
            acc += v * v
            n += 1
            pos += self.step
        self.pos = pos - frames

        rms = int((acc / n) ** 0.5) if n else 0
        return Chunk(out.tobytes(), rms, n)


def list_devices() -> dict:
    """列出可用的麦克风和回环端点。

    回环设备在 pyaudiowpatch 的枚举里是**额外追加**的条目（本机上真实设备只到
    索引 4，回环是 5），靠 ``isLoopbackDevice`` 区分 —— 不要靠名字里的
    "[Loopback]"，那是显示名，会随系统语言变。

    返回里带 ``note``：设备为空时**说清楚为什么**，而不是给一个空下拉框让用户猜。
    """
    pa = _pa()
    mics: list[dict] = []
    loops: list[dict] = []
    err = ""
    with _enum_lock:
        p = None
        try:
            p = pa.PyAudio()
            try:
                api_idx = p.get_host_api_info_by_type(pa.paWASAPI)["index"]
            except Exception as e:  # noqa: BLE001
                raise CaptureError("这台机器没有 WASAPI 音频接口：%s" % e) from e

            default_lb = -1
            try:
                default_lb = int(p.get_default_wasapi_loopback()["index"])
            except Exception:  # noqa: BLE001
                pass       # 没有默认输出端点，下面的回环列表大概也是空的

            for i in range(p.get_device_count()):
                try:
                    d = p.get_device_info_by_index(i)
                except Exception:  # noqa: BLE001
                    continue
                if d.get("hostApi") != api_idx or int(d.get("maxInputChannels") or 0) <= 0:
                    continue
                item = {
                    "index": int(d["index"]),
                    "name": str(d.get("name") or "?"),
                    "channels": int(d["maxInputChannels"]),
                    "rate": int(d.get("defaultSampleRate") or 0),
                }
                if d.get("isLoopbackDevice"):
                    item["default"] = item["index"] == default_lb
                    loops.append(item)
                else:
                    mics.append(item)
        except CaptureError as e:
            err = str(e)
        except Exception as e:  # noqa: BLE001
            err = "枚举音频设备失败：%s" % e
            log.warning("list_devices failed: %s", e)
        finally:
            if p is not None:
                try:
                    p.terminate()
                except Exception:  # noqa: BLE001
                    pass

    note = err
    if not err:
        if not mics and not loops:
            note = "这台机器上一个音频设备都没有（常见于虚拟机 / Windows Server）。"
        elif not mics:
            note = "没有输入设备，麦克风这一路不可用；系统声音（回环）可以录。"
        elif not loops:
            note = "没有输出端点，系统声音（回环）不可用；麦克风可以录。"

    return {"mic": mics, "loopback": loops, "note": note,
            "mic_available": bool(mics), "loopback_available": bool(loops)}


class Capture(threading.Thread):
    """一路采集。自带 PyAudio 实例，从头到尾只在自己这个线程里碰它。

    为什么每路一个线程一个实例：WASAPI 走 COM，跨线程共用同一个流是自找麻烦。
    「两路同时录」就是起两个 Capture，各自独立 —— 一路挂了不影响另一路
    （和 memo 的两个来源互不拖累是同一条原则）。
    """

    def __init__(self, kind: str, device_index: int | None,
                 on_chunk: Callable[[Chunk], None],
                 on_error: Callable[[str], None] | None = None) -> None:
        super().__init__(name="voice-capture-%s" % kind, daemon=True)
        if kind not in KINDS:
            raise CaptureError("未知的采集类型：%r" % kind)
        self.kind = kind
        self.device_index = device_index
        self._on_chunk = on_chunk
        self._on_error = on_error
        self._stop = threading.Event()
        self.error = ""
        self.frames_out = 0
        self.device_name = ""
        # 起来了（成功或失败都会置位）。调用方用它区分「开录失败」和「录着但没声音」。
        self.settled = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _resolve(self, p) -> dict:
        if self.device_index is not None:
            d = p.get_device_info_by_index(int(self.device_index))
            if int(d.get("maxInputChannels") or 0) <= 0:
                raise CaptureError("设备「%s」不能用来录音（没有输入声道）。" % d.get("name"))
            return d
        if self.kind == LOOPBACK:
            try:
                return p.get_default_wasapi_loopback()
            except Exception as e:  # noqa: BLE001
                raise CaptureError("找不到默认输出端点，录不了系统声音：%s" % e) from e
        try:
            return p.get_device_info_by_index(p.get_default_input_device_info()["index"])
        except Exception as e:  # noqa: BLE001
            raise CaptureError(
                "这台机器没有默认麦克风（%s）。虚拟机 / Windows Server 常常"
                "一个输入设备都没有。" % e
            ) from e

    def run(self) -> None:
        p = None
        stream = None
        try:
            pa = _pa()
            p = pa.PyAudio()
            d = self._resolve(p)
            rate = int(d.get("defaultSampleRate") or SAMPLE_RATE)
            ch = int(d.get("maxInputChannels") or 1)
            self.device_name = str(d.get("name") or "?")
            # 按设备原生格式打开，自己转到 16 kHz。
            # 不向 WASAPI 要 16 kHz：非原生采样率会被拒（或静默走一条我们验证不了的
            # 内部转换），而自己转的代价实测只有单核 5.2%。
            stream = p.open(format=pa.paInt16, channels=ch, rate=rate, input=True,
                            input_device_index=int(d["index"]),
                            frames_per_buffer=_FRAMES_PER_BUFFER)
            res = _Resampler(rate, ch)
            log.info("capture %s open: %s rate=%d ch=%d", self.kind, self.device_name, rate, ch)
            self.settled.set()

            bytes_per_frame = 2 * ch
            while not self._stop.is_set():
                try:
                    avail = stream.get_read_available()
                except Exception as e:  # noqa: BLE001
                    raise CaptureError("读音频流失败：%s" % e) from e
                if avail <= 0:
                    # 空闲。回环上这就是「现在没人放声音」；麦克风上一般不会走到这。
                    # **这里绝不能改成阻塞 read** —— 见模块头注。
                    time.sleep(_POLL_SLEEP)
                    continue
                raw = stream.read(min(int(avail), _READ_CAP), exception_on_overflow=False)
                if not raw or len(raw) < bytes_per_frame:
                    continue
                chunk = res.feed(raw)
                if chunk.frames:
                    self.frames_out += chunk.frames
                    self._on_chunk(chunk)
        except CaptureError as e:
            self.error = str(e)
            log.warning("capture %s failed: %s", self.kind, e)
        except Exception as e:  # noqa: BLE001
            self.error = "%s: %s" % (type(e).__name__, e)
            log.exception("capture %s crashed", self.kind)
        finally:
            self.settled.set()      # 失败也要放行，否则调用方一直等在这儿
            if self.error and self._on_error:
                try:
                    self._on_error(self.error)
                except Exception:  # noqa: BLE001
                    pass
            for closer in (
                lambda: stream and stream.stop_stream(),
                lambda: stream and stream.close(),
                lambda: p and p.terminate(),
            ):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
            log.info("capture %s stopped after %d frames", self.kind, self.frames_out)
