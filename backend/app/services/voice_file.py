"""把一个音频 / 视频文件解成 16 kHz 单声道 PCM16 —— 全程只在内存里。

## 为什么是 PyAV

要覆盖的是「手机录的会议、别人发来的录音、下载的会议回放」，也就是
**m4a / mp3 / mp4**，不是 WAV。而 AAC 这一格把别的选项全筛掉了：

- Python 自带 ``wave``  只认 PCM WAV
- ``soundfile`` / libsndfile   WAV / FLAC / OGG / MP3，**没有 AAC**
- ``miniaudio``               WAV / FLAC / MP3 / Vorbis，**没有 AAC**
- 系统 ffmpeg                 不能假定装了（本机 PATH 里就没有）
- Windows Media Foundation     笔记本上有，但 Windows Server 上
  ``Server-Media-Foundation`` 功能默认**未安装**（实测状态 Available）——
  等于要写一套在开发机上验不了的 COM 互操作
- **PyAV**                    自带 ffmpeg，pip 就能装，414 种封装 / 557 种编解码，
                              连视频文件的音轨也能直接取

iPhone 语音备忘录是 .m4a，安卓多半也是 .m4a / .3gp，所以 AAC 不是可选项。

代价：PyAV 落盘 68 MB（其中 63 MB 是 ffmpeg 的 DLL）。里面 30 MB 是我们完全用不到的
**视频编码器**（libx265 12.2 MB、libSvtAv1Enc 7.3 MB、libx264、libvpx、libdav1d、
swscale）。想砍得先打包实测 —— PyAV 的扩展模块是链着 avfilter / swscale 的，
乱排除会变成 import 就炸。

## 不写盘，比「处理完即删」更彻底

刻意**不用** FastAPI 的 ``UploadFile``：它底层是 ``SpooledTemporaryFile``，超过
1 MB 就落到系统临时目录 —— 也就是说一段会议录音会先在磁盘上完整存在一份，
"处理完即删"只能删我们自己那份，删不掉它的。

所以接口改成收**原始请求体**（不走 multipart），字节进内存、解码进内存、
转写完就出作用域。磁盘上从头到尾没有过音频。这和 services/voice.py 里
「音频不落盘」是同一条线，不是两套说法。
"""
from __future__ import annotations

import io
import logging
from array import array

from .voice_capture import SAMPLE_RATE

log = logging.getLogger("voice.file")

# 解码时一次交给重采样器的帧数上限，纯粹为了不让单个 numpy/array 太大。
_TARGET_LAYOUT = "mono"
_TARGET_FORMAT = "s16"


class DecodeError(RuntimeError):
    """解码侧的人话错误，调用方直接把 str(e) 给用户看。"""


def _av():
    """惰性 import。装不上 PyAV 的机器要能正常启动，只是这个功能不可用。"""
    try:
        import av  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        raise DecodeError("没装上音视频解码库（PyAV）：%s" % e) from e
    return av


def probe_and_decode(data: bytes, filename: str = "") -> tuple[bytes, float, dict]:
    """解码成 16 kHz 单声道 PCM16。返回 (pcm, 秒数, 元信息)。

    视频文件（mp4 / mov / mkv）走同一条路：``decode(audio=0)`` 只要第一条音轨，
    画面根本不解 —— 所以传一段会议录屏进来也能出文字。
    """
    av = _av()
    if not data:
        raise DecodeError("文件是空的。")

    try:
        container = av.open(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        raise DecodeError(
            "认不出这个文件的格式（%s）。支持常见的音频和视频："
            "m4a / mp3 / wav / aac / flac / ogg / opus / mp4 / mov 等。" % e
        ) from e

    out = bytearray()
    meta: dict = {"filename": filename}
    try:
        streams = [s for s in container.streams if s.type == "audio"]
        if not streams:
            raise DecodeError(
                "这个文件里没有音轨。" +
                ("（看起来是个只有画面的视频。）" if any(
                    s.type == "video" for s in container.streams) else ""))
        st = streams[0]
        meta["codec"] = getattr(getattr(st, "codec_context", None), "name", "") or ""
        meta["src_rate"] = int(getattr(st.codec_context, "sample_rate", 0) or 0)
        meta["src_channels"] = int(getattr(st.codec_context, "channels", 0) or 0)
        meta["streams"] = len(streams)

        resampler = av.audio.resampler.AudioResampler(
            format=_TARGET_FORMAT, layout=_TARGET_LAYOUT, rate=SAMPLE_RATE)

        for frame in container.decode(audio=0):
            # PyAV 的 resample 在不同版本上分别返回单个 frame 或 frame 列表，
            # 两种都得吃 —— 只按一种写，换个版本就静默丢一半音频。
            res = resampler.resample(frame)
            for f in (res if isinstance(res, (list, tuple)) else [res]):
                if f is None:
                    continue
                out += bytes(f.planes[0])[:f.samples * 2]
        # 冲掉重采样器内部残留的那一点（不 flush 会丢结尾几十毫秒）
        try:
            res = resampler.resample(None)
            for f in (res if isinstance(res, (list, tuple)) else [res]):
                if f is not None:
                    out += bytes(f.planes[0])[:f.samples * 2]
        except Exception:  # noqa: BLE001
            pass
    except DecodeError:
        raise
    except Exception as e:  # noqa: BLE001
        raise DecodeError("解码失败：%s" % e) from e
    finally:
        try:
            container.close()
        except Exception:  # noqa: BLE001
            pass

    pcm = bytes(out)
    secs = len(pcm) / 2 / SAMPLE_RATE
    if secs < 0.2:
        raise DecodeError("解出来的音频只有 %.2f 秒，基本是空的。" % secs)
    meta["seconds"] = round(secs, 2)
    log.info("decoded %s: codec=%s %dHz/%dch -> %.2fs @16k mono",
             filename or "(unnamed)", meta.get("codec"), meta.get("src_rate") or 0,
             meta.get("src_channels") or 0, secs)
    return pcm, secs, meta


def split_for_commit(pcm: bytes, max_audio_s: float, *, search_s: float = 2.5,
                     frame_ms: int = 100) -> list[bytes]:
    """按不超过 ``max_audio_s`` 秒切块，切口尽量落在**最安静**的地方。

    为什么不直接每 N 秒硬切：服务端缓冲上限 30 秒（实测报错原文见
    voice_asr._MAX_SEGMENT_AUDIO_S），所以**必须**切；但硬切会切在词中间，
    那个词两边都识别不出来。这里在每块末尾 ``search_s`` 秒的窗口里找 RMS
    最低的一帧当切口 —— 说话人多半在那儿换气。

    只在时间轴上找最小值，不做任何频域分析：代价是偶尔切得不完美，
    收益是不用引第二个依赖，而且逻辑一眼能看懂。
    """
    step = int(max_audio_s * SAMPLE_RATE) * 2
    if step <= 0 or len(pcm) <= step:
        return [pcm] if pcm else []

    frame_bytes = max(2, int(SAMPLE_RATE * frame_ms / 1000) * 2)
    search_bytes = max(frame_bytes, int(search_s * SAMPLE_RATE) * 2)
    out: list[bytes] = []
    pos = 0
    while pos < len(pcm):
        end = pos + step
        if end >= len(pcm):
            out.append(pcm[pos:])
            break
        lo = max(pos + frame_bytes, end - search_bytes)
        best_at, best_rms = end, None
        at = lo
        while at + frame_bytes <= end:
            s = array("h")
            s.frombytes(pcm[at:at + frame_bytes])
            r = (sum(v * v for v in s) / len(s)) ** 0.5 if len(s) else 0.0
            if best_rms is None or r < best_rms:
                best_rms, best_at = r, at
            at += frame_bytes
        out.append(pcm[pos:best_at])
        pos = best_at
    return [c for c in out if c]
