#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tts_server.py —— 本机 TTS 合成服务（sound 动作后端，Plan B）

分工：
  uiagent.py 的 sound 动作 → POST /tts {"text":"..."} → 本服务合成 MP3（edge-tts）+ 缓存
                              → 返回 {"url":"http://127.0.0.1:43030/tts_cache/<hash>.mp3",
                                       "seconds":12.3, "cached":false}
  TtsService（HAP 内无窗口服务）→ 用 AVPlayer 流式播放该 url

跑法（在装 HAP 的同一台机器上，如平板终端）：
  python3 tts_server.py                     # 监听 127.0.0.1:43030，音色 zh-CN-YunxiNeural
  python3 tts_server.py --port 43030 --voice zh-CN-XiaoxiaoNeural
  python3 tts_server.py --speak "测试句"     # 一次性：只合成并打印 JSON，不起服务
  python3 tts_server.py --list-voices       # 列出中文音色

依赖：
  pip install edge-tts          # 或 pip install --target ./tts_deps edge-tts（配 PYTHONPATH）
  edge-tts 需要出网到微软 TTS 端点；若直连被墙，设环境变量
  https_proxy=http://127.0.0.1:28080 再启动。

接口：
  POST /tts          {"text":"...", "voice"?: "..."} → {"url","seconds","cached","voice","chars"}
  POST /tts_stream   {"text":"...", "voice"?: "..."} → 秒回：
                     缓存命中 {"id":null,"cached":true,"url","seconds","chars"}
                     未命中   {"id":"<sid>","cached":false,"url":".../tts_stream/<sid>","chars"}
  GET  /tts_stream/<sid>  chunked 音频（边生成边吐；客户端比生成器快时等块；完成/失败即关）
  GET  /tts_status/<sid>  {"state":"generating|done|failed","total_seconds","total_bytes",
                           "first_byte_rel","connect_rel","error","chars"}
                           total_seconds = done 时全文件重新校时（Xing > 逐帧走 > CBR 字节÷码率）；
                                          生成中=字节/首帧码率增量估算（09-12 起帧头按 11-bit sync 正确解析）
                           first_byte_rel = POST→首音频块生成秒数；connect_rel = POST→AVPlayer 连入秒数
                           （客户端 pacing：出声 ≈ POST + max(两者) + 0.3s）
  GET  /tts_cache/x  音频文件本体（audio/mpeg）
  GET  /health       服务状态（含 streaming:true 能力标志）
"""
import argparse
import asyncio
import hashlib
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_VOICE = 'zh-CN-YunxiNeural'   # 云希（男声，自然）；备选 zh-CN-XiaoxiaoNeural（晓晓，女声）
FALLBACK_RATE = 4.5                    # 时长解析失败时的兜底估算：字/秒
SYNTH_TIMEOUT = 30                     # 单次合成超时（秒）——edge-tts 自身无超时，必须兜住
SYNTH_RETRIES = 3                      # 瞬时失败（空响应/网络抖动）重试次数，退避 1s/2s
STREAM_TIMEOUT = 180                   # 流式合成全程超时（秒）：≤600 字 ≈ ≤130s 音频，合成 ~1.5 倍实时
STREAM_DEFAULT_RATE = 48000            # 首帧码率解析失败时的兜底码率（09-12 逐帧实锤 edge-tts = MPEG2 L3 48kbps/24kHz CBR）


# ---------------------------------------------------------------- MP3 时长解析（纯标准库）
# 09-12 修：旧版按 12-bit sync 语义取位（version=(b1>>4)&3 / layer=(b1>>2)&3），位偏移整体错 1 位——
# 把 edge-tts 的 MPEG2 L3 48kbps/24kHz 帧头（0xF3）误读成 MPEG1 80kbps/48kHz，
# 所有时长系统性低估 40%（21.17s vs 逐帧实锤 35.28s），即"声音没播完就翻页"的真根因。
# 现按 ISO 11172-3 正确的 11-bit sync 语义解析，时长优先级：
#   Xing 标签（总帧数精确）> 逐帧走（VBR/CBR 都准）> CBR 字节÷码率 > 字数兜底

_BR_TABLES = {
    3: (0, 32000, 40000, 48000, 56000, 64000, 80000, 96000, 112000, 128000, 160000, 192000, 224000, 256000, 320000),
    2: (0, 8000, 16000, 24000, 32000, 40000, 48000, 56000, 64000, 80000, 96000, 112000, 128000, 144000, 160000),
    0: (0, 8000, 16000, 24000, 32000, 40000, 48000, 56000, 64000, 80000, 96000, 112000, 128000, 144000, 160000),
}
_SR_TABLES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (22050, 24000, 16000)}
_SLF = {3: 144, 2: 72, 0: 72}           # 帧长 = slf × br / sr + pad
_SPF = {3: 1152, 2: 576, 0: 576}        # 每帧采样点
_SIDE_L3 = {3: (17, 32), 2: (9, 17), 0: (9, 17)}  # L3 side info 字节数 (单声道, 立体声)


def _estimate(chars: int) -> float:
    return max(0.5, chars / FALLBACK_RATE)


def _mp3_header_at(buf: bytes, idx: int):
    """解析 buf[idx] 处 4 字节帧头（11-bit sync 语义，ISO 11172-3）
    → (version, br, sr, pad, flen, spf) 或 None。仅支持 Layer III（edge-tts 全是 L3）。"""
    n = len(buf)
    if idx + 4 > n or buf[idx] != 0xFF or (buf[idx + 1] & 0xE0) != 0xE0:
        return None
    w1 = buf[idx] << 8 | buf[idx + 1]
    w2 = buf[idx + 2] << 8 | buf[idx + 3]
    version = (w1 >> 3) & 0x3        # 3=MPEG1 2=MPEG2 0=MPEG2.5（1=保留）
    layer = (w1 >> 1) & 0x3          # 1=Layer III
    br_idx = (w2 >> 12) & 0xF
    sr_idx = (w2 >> 10) & 0x3
    if version == 1 or layer != 1 or br_idx in (0, 15) or sr_idx == 3:
        return None
    br = _BR_TABLES[version][br_idx]
    sr = _SR_TABLES[version][sr_idx]
    pad = (w2 >> 9) & 0x1
    flen = _SLF[version] * br // sr + pad
    return version, br, sr, pad, flen, _SPF[version]


def _walk_mp3_duration(data: bytes, i: int):
    """从第 i 帧起逐帧走到底，累加每帧时长（(spf+pad)/sr，VBR/CBR 都准）→ 秒。
    失步或尾部残留 >64B → None（调用方回落 CBR 估算）。09-12 实锤文件 1470 帧 0 失步。"""
    n = len(data)
    pos = i
    total = 0.0
    frames = 0
    while pos + 4 <= n:
        h = _mp3_header_at(data, pos)
        if h is None or pos + h[4] > n:
            break
        _, _, sr, pad, flen, spf = h
        total += (spf + pad) / sr
        frames += 1
        pos += flen
    if frames < 2 or n - pos > 64:
        return None
    return total


def parse_mp3_duration(path: str, fallback_chars: int = 0) -> float:
    """MP3 时长（秒）。ID3v2 + Xing(VBR 总帧数) + 逐帧走 + CBR 估算；失败返回兜底估计。"""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return _estimate(fallback_chars)
    return parse_mp3_duration_buf(data, fallback_chars)


def parse_mp3_duration_buf(data: bytes, fallback_chars: int = 0) -> float:
    """同 parse_mp3_duration，直接解析完整字节缓冲（流式路径 done 时重新校时用，09-12）。"""
    n = len(data)
    pos = 0
    # 1) 跳过 ID3v2 头
    if n >= 10 and data[:3] == b'ID3':
        size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        pos = min(n, 10 + size)
    # 2) 找第一个帧同步字
    i = pos
    while i + 4 <= n:
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            break
        i += 1
    else:
        return _estimate(fallback_chars)
    hdr = _mp3_header_at(data, i)
    if hdr is None:
        return _estimate(fallback_chars)
    version, br, sr, pad, flen, spf = hdr
    # 3) 首帧内找 Xing 标签（VBR：总帧数 → 精确时长）
    mono = ((data[i + 2] >> 6) & 0x3) == 3    # channel mode 3 = single channel
    side_info = _SIDE_L3[version][0 if mono else 1]
    tag_off = i + 4 + side_info
    if tag_off + 8 <= n and data[tag_off:tag_off + 4] == b'Xing':
        total_frames = int.from_bytes(data[tag_off + 4:tag_off + 8], 'big')
        if total_frames > 0:
            return total_frames * spf / sr
    # 4) 逐帧走（09-12：edge-tts 无 Xing 标签，逐帧走是唯一权威路径；CBR/VBR 均准）
    walked = _walk_mp3_duration(data, i)
    if walked is not None and 0.2 < walked < 3600:
        return walked
    # 5) CBR 兜底：剩余字节 × 8 / 码率
    duration = (n - i) * 8 / br
    if 0.2 < duration < 3600:
        return duration
    return _estimate(fallback_chars)


# ---------------------------------------------------------------- 合成（edge-tts，惰性加载）

_edge_checked = False


def edge_available() -> bool:
    global _edge_checked
    if not _edge_checked:
        try:
            import edge_tts  # noqa: F401
        except Exception as e:
            print(f'[tts] edge-tts 不可用: {e}', flush=True)
        _edge_checked = True
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return False


async def _synth(text: str, voice: str, out_path: str) -> None:
    import edge_tts
    await edge_tts.Communicate(text, voice).save(out_path)


# ---------------------------------------------------------------- 合成后端（与 HTTP 解耦，--speak 模式不占端口）

class SynthBackend:
    def __init__(self, args):
        self.bind = args.bind
        self.port = args.port
        self.advertise = args.advertise
        self.voice = args.voice
        self.cache_dir = Path(args.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def base_url(self) -> str:
        return f'http://{self.advertise}:{self.port}'

    def synthesize(self, text: str, voice: str):
        """合成（或命中缓存）→ 元信息 dict；失败返回 None"""
        key = hashlib.md5(f'{voice}\x1f{text}'.encode('utf-8')).hexdigest()
        fp = self.cache_dir / f'{key}.mp3'
        meta_fp = self.cache_dir / f'{key}.json'
        if fp.is_file():
            # 09-12：时长恒从文件重解析（旧 JSON seconds 按误读的 80k 算，偏短 40%；重解析让旧缓存自愈）
            seconds = parse_mp3_duration(str(fp), len(text))
            return {'url': f'{self.base_url()}/tts_cache/{fp.name}',
                    'seconds': round(seconds, 2), 'cached': True,
                    'voice': voice, 'chars': len(text)}
        if not edge_available():
            return None
        tmp = fp.with_suffix('.tmp')
        last_err = None
        for attempt in range(1, SYNTH_RETRIES + 1):
            try:
                asyncio.run(asyncio.wait_for(_synth(text, voice, str(tmp)), SYNTH_TIMEOUT))
                if not tmp.is_file() or tmp.stat().st_size < 200:
                    raise RuntimeError('edge-tts 输出为空（多为出网被拦或瞬时空响应，可设 https_proxy）')
                tmp.replace(fp)
                last_err = None
                break
            except Exception as e:
                last_err = e
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                print(f'[tts] 合成第 {attempt}/{SYNTH_RETRIES} 次失败: {e}', flush=True)
                if attempt < SYNTH_RETRIES:
                    time.sleep(1.0 * attempt)
        if last_err is not None:
            print(f'[tts] 合成失败（{SYNTH_RETRIES} 次用尽）: {last_err}', flush=True)
            return None
        seconds = parse_mp3_duration(str(fp), len(text))
        meta_fp.write_text(json.dumps({'seconds': seconds, 'voice': voice,
                                       'chars': len(text), 'ts': time.time()},
                                      ensure_ascii=False), 'utf-8')
        return {'url': f'{self.base_url()}/tts_cache/{fp.name}',
                'seconds': round(seconds, 2), 'cached': False,
                'voice': voice, 'chars': len(text)}

    def stream_start(self, text: str, voice: str):
        """流式合成入口：缓存命中 → 直接给缓存 url（id=null，无需流式）；
        未命中 → 起后台流任务，秒回流 url。edge-tts 缺失返回 None（调用方回 502）。"""
        _prune_streams()
        key = hashlib.md5(f'{voice}\x1f{text}'.encode('utf-8')).hexdigest()
        fp = self.cache_dir / f'{key}.mp3'
        meta_fp = self.cache_dir / f'{key}.json'
        if fp.is_file():
            # 09-12：同 synthesize——时长恒从文件重解析（旧 JSON seconds 偏短 40%，重解析自愈）
            seconds = parse_mp3_duration(str(fp), len(text))
            return {'id': None, 'cached': True,
                    'url': f'{self.base_url()}/tts_cache/{fp.name}',
                    'seconds': round(seconds, 2), 'chars': len(text)}
        if not edge_available():
            return None
        sid = uuid.uuid4().hex[:12]
        job = _StreamJob(sid, text, voice, self.cache_dir)
        with _STREAMS_LOCK:
            _STREAMS[sid] = job
        threading.Thread(target=_gen_stream, args=(job,), daemon=True).start()
        return {'id': sid, 'cached': False,
                'url': f'{self.base_url()}/tts_stream/{sid}', 'chars': len(text)}


# ---------------------------------------------------------------- 流式合成（09-11：边生成边播，免拆段）

def _find_bitrate(buf: bytes):
    """在前导字节里找第一个有效 MP3 帧头 → 码率 bps；找不到返回 None（调用方走兜底码率）。
    09-12：与时长解析共用 _mp3_header_at（旧版同样的 12-bit sync 位偏移 bug，把 48k 误读成 80k）。"""
    n = len(buf)
    i = 0
    if n >= 10 and buf[:3] == b'ID3':
        size = ((buf[6] & 0x7F) << 21) | ((buf[7] & 0x7F) << 14) | ((buf[8] & 0x7F) << 7) | (buf[9] & 0x7F)
        i = min(n, 10 + size)
    while i + 4 <= n:
        if buf[i] == 0xFF and (buf[i + 1] & 0xE0) == 0xE0:
            h = _mp3_header_at(buf, i)
            return h[1] if h is not None else None   # 同步字但头不合法 → 不采信，交给兜底
        i += 1
    return None


class _StreamJob:
    """一次流式合成任务：后台线程跑 edge-tts stream()，HTTP 客户端（AVPlayer）逐块拉取。"""

    def __init__(self, sid, text, voice, cache_dir=None):
        self.sid = sid
        self.text = text
        self.voice = voice
        self.cache_dir = cache_dir  # 完成后落缓存目录（与 stream_start 同 key 体系）
        self.chunks = []            # list[bytes]，按到达顺序
        self.cond = threading.Condition()
        self.state = 'generating'   # generating | done | failed
        self.error = None
        self.total_bytes = 0
        self.bitrate = None         # 首帧实测；None → STREAM_DEFAULT_RATE
        self._hdr_buf = b''
        self.created = time.time()
        self.chars = len(text)
        self.first_byte_at = None      # 首个音频块生成时刻（pacing：POST→首包延迟，09-12）
        self.player_connect_at = None  # AVPlayer 首次 GET 连入时刻（pacing：同上）
        self.duration_exact = None     # done 时全文件重新校时（Xing 精确值优先，09-12）


_STREAMS = {}
_STREAMS_LOCK = threading.Lock()


def _prune_streams():
    """清理 1 小时前的任务，防内存无界增长（每个任务持有全量音频字节）。"""
    now = time.time()
    with _STREAMS_LOCK:
        for k in [k for k, j in _STREAMS.items() if now - j.created > 3600]:
            del _STREAMS[k]


def _gen_stream(job):
    """后台线程：edge-tts stream() 逐块合成 → job.chunks（累计真实字节数，供时长实测）。
    09-12：零音频输出的瞬时失败（NoAudioReceived 等，微软侧偶发）自动重试——退避 1s/2s，
    最多 SYNTH_RETRIES 次（此时 AVPlayer 未播任何音频，重试干净无重播）；
    已有音频输出 / 慢挂 ≥60s 则不重试（防重播/重试无益），立即失败交客户端早期重试决策。"""
    import edge_tts

    async def _run_once():
        comm = edge_tts.Communicate(job.text, job.voice)
        async for chunk in comm.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                data = chunk["data"]
                with job.cond:
                    if job.first_byte_at is None:
                        job.first_byte_at = time.time()
                    job.total_bytes += len(data)
                    job.chunks.append(data)
                    if job.bitrate is None:
                        if len(job._hdr_buf) <= 8192:
                            job._hdr_buf += data
                            job.bitrate = _find_bitrate(job._hdr_buf)
                            if job.bitrate:
                                job._hdr_buf = None
                        else:
                            job._hdr_buf = None   # 前 8KB 没找到帧头 → 放弃，用兜底码率
                    job.cond.notify_all()

    async def _run():
        for attempt in range(1, SYNTH_RETRIES + 1):
            try:
                await asyncio.wait_for(_run_once(), STREAM_TIMEOUT)
                return
            except Exception as e:
                with job.cond:
                    had_audio = job.total_bytes > 0
                if had_audio or (time.time() - job.created) >= 60:
                    raise   # 已出声→重试会重播；慢挂→重试大概率同样挂；都立即失败
                if attempt >= SYNTH_RETRIES:
                    raise
                print(f'[tts] 流 {job.sid} 第 {attempt}/{SYNTH_RETRIES} 次失败（零音频输出）: '
                      f'{type(e).__name__}: {str(e)[:120]} → {attempt}s 后重试', flush=True)
                await asyncio.sleep(float(attempt))

    try:
        asyncio.run(_run())
        job.state = 'done'
        # 09-12：文件齐全后用已验证解析器重新校时（Xing 精确值优先/CBR 字节÷码率），
        # 替代"总字节÷首帧码率"的增量估算——VBR 文件下首帧码率≠平均码率，会系统性偏短
        job.duration_exact = parse_mp3_duration_buf(b''.join(job.chunks), job.chars)
        dur = job.duration_exact
        print(f'[tts] 流 {job.sid} 完成: {job.total_bytes}B (~{dur:.1f}s 已校时) {job.chars} 字 '
              f'码率={job.bitrate or "兜底"}', flush=True)
        # 落缓存（与 stream_start 同 key）：同文本二次请求直接命中，省一次云合成
        if job.cache_dir is not None:
            try:
                key = hashlib.md5(f'{job.voice}\x1f{job.text}'.encode('utf-8')).hexdigest()
                fp = job.cache_dir / f'{key}.mp3'
                meta_fp = job.cache_dir / f'{key}.json'
                if not fp.is_file():
                    tmp = fp.with_suffix('.mp3.part')
                    with open(tmp, 'wb') as f:
                        for c in job.chunks:
                            f.write(c)
                    tmp.replace(fp)
                    meta_fp.write_text(json.dumps({'seconds': round(dur, 2), 'voice': job.voice,
                                                   'chars': job.chars, 'ts': time.time()},
                                                  ensure_ascii=False), 'utf-8')
                    print(f'[tts] 流 {job.sid} 已落缓存: {fp.name}', flush=True)
            except Exception as ce:
                print(f'[tts] 流 {job.sid} 落缓存失败（不影响本次播放）: {ce}', flush=True)
    except Exception as e:
        job.state = 'failed'
        job.error = f'{type(e).__name__}: {str(e)[:160]}'
        print(f'[tts] 流 {job.sid} 失败: {job.error}', flush=True)
    finally:
        with job.cond:
            job.cond.notify_all()


class TTSServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, args):
        super().__init__((args.bind, args.port), Handler)
        self.backend = SynthBackend(args)


class Handler(BaseHTTPRequestHandler):
    server_version = 'UiAgentTTS/1.0'

    def log_message(self, fmt, *args):
        print(f'[{time.strftime("%H:%M:%S")}] {self.address_string()} {fmt % args}', flush=True)

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        srv = self.server.backend  # type: ignore[union-attr]
        path = urlsplit(self.path).path
        if path == '/health':
            cached = len(list(srv.cache_dir.glob('*.mp3')))
            self._send(200, {'ok': True, 'voice': srv.voice, 'cached': cached,
                             'edge_tts': edge_available(), 'streaming': True})
            return
        if path.startswith('/tts_stream/'):
            sid = os.path.basename(path)
            with _STREAMS_LOCK:
                job = _STREAMS.get(sid)
            if job is None:
                self._send(404, {'error': 'unknown stream id'})
                return
            self._serve_stream(job)
            return
        if path.startswith('/tts_status/'):
            sid = os.path.basename(path)
            with _STREAMS_LOCK:
                job = _STREAMS.get(sid)
            if job is None:
                self._send(404, {'error': 'unknown stream id'})
                return
            fb, pc = job.first_byte_at, job.player_connect_at
            if job.state == 'done' and job.duration_exact is not None:
                total_seconds = round(job.duration_exact, 2)   # 全文件重新校时（Xing 精确值优先）
            else:
                total_seconds = round(job.total_bytes * 8.0
                                      / (job.bitrate or STREAM_DEFAULT_RATE), 2)  # 生成中：增量估算
            self._send(200, {'state': job.state,
                             'total_seconds': total_seconds,
                             'total_bytes': job.total_bytes,
                             'first_byte_rel': round(fb - job.created, 3) if fb is not None else None,
                             'connect_rel': round(pc - job.created, 3) if pc is not None else None,
                             'error': job.error, 'chars': job.chars})
            return
        if path.startswith('/tts_cache/'):
            fp = srv.cache_dir / os.path.basename(path)
            if not fp.is_file():
                self._send(404, {'error': 'not found'})
                return
            self._send(200, fp.read_bytes(), 'audio/mpeg')
            return
        self._send(404, {'error': 'unknown path'})

    def _serve_stream(self, job):
        """chunked 下发：边生成边吐。客户端（AVPlayer）比生成器快时等块；
        任务终结（done/failed）后把剩余块发完即关。客户端断开不影响生成线程。"""
        with job.cond:
            if job.player_connect_at is None:
                job.player_connect_at = time.time()
        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Transfer-Encoding', 'chunked')
        self.send_header('Connection', 'close')
        self.end_headers()
        offset = 0
        try:
            while True:
                with job.cond:
                    while offset >= len(job.chunks) and job.state == 'generating':
                        job.cond.wait(timeout=15.0)
                    if offset >= len(job.chunks):
                        break   # 无新块且已终结（done/failed）→ 关流
                    data = job.chunks[offset]
                    offset += 1
                self.wfile.write(b'%x\r\n' % len(data))
                self.wfile.write(data)
                self.wfile.write(b'\r\n')
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass   # 客户端走了（uiagent 放弃重试/播放器关闭），生成继续跑完

    def do_POST(self):
        srv = self.server.backend  # type: ignore[union-attr]
        path = urlsplit(self.path).path
        if path not in ('/tts', '/tts_stream'):
            self._send(404, {'error': 'unknown path'})
            return
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b'{}'
        try:
            req = json.loads(raw.decode('utf-8'))
        except Exception:
            self._send(400, {'error': 'bad json'})
            return
        text = (req.get('text') or '').strip()
        if not text:
            self._send(400, {'error': 'empty text'})
            return
        voice = (req.get('voice') or srv.voice).strip()
        if path == '/tts_stream':
            result = srv.stream_start(text, voice)
            if result is None:
                self._send(502, {'error': 'edge-tts 不可用（流式合成不可用，见服务日志）'})
                return
            self._send(200, result)
            return
        t0 = time.time()
        result = srv.synthesize(text, voice)
        if result is None:
            self._send(502, {'error': 'synthesis failed（edge-tts 缺失或出网被拦，见服务日志）'})
            return
        result['synth_ms'] = int((time.time() - t0) * 1000)
        self._send(200, result)


def main():
    ap = argparse.ArgumentParser(description='UiAgent TTS server (edge-tts + cache)')
    ap.add_argument('--port', type=int, default=43030)
    ap.add_argument('--bind', default='127.0.0.1', help='监听地址（默认仅本机）')
    ap.add_argument('--advertise', default='127.0.0.1', help='回给调用方 URL 里的主机名')
    ap.add_argument('--voice', default=DEFAULT_VOICE)
    ap.add_argument('--cache-dir', default=str(Path(__file__).resolve().parent / 'tts_cache'))
    ap.add_argument('--speak', metavar='TEXT', help='一次性模式：合成并打印 JSON，不起服务')
    ap.add_argument('--list-voices', action='store_true', help='列出中文音色')
    args = ap.parse_args()

    if args.list_voices:
        if not edge_available():
            print('edge-tts 不可用', flush=True)
            return
        import edge_tts
        for v in asyncio.run(edge_tts.list_voices()):
            if v.get('Locale', '').startswith('zh'):
                print(v['ShortName'], v['Gender'])
        return

    if args.speak:
        # 一次性模式：只用合成后端，不绑定端口（可与常驻服务共存）
        r = SynthBackend(args).synthesize(args.speak, args.voice)
        print(json.dumps(r or {'error': 'synth failed'}, ensure_ascii=False, indent=2))
        return
    # 09-12：日志落盘——uiagent 自动拉起时 stdout=DEVNULL，合成失败/重试行在实机上无处可查。
    # 这里把 stdout tee 到同目录 tts_server.log（超 1MB 先截断保最后 512KB），stderr 并入同文件。
    import sys as _sys
    try:
        _log_fp = Path(__file__).resolve().parent / 'tts_server.log'
        if _log_fp.is_file() and _log_fp.stat().st_size > 1000000:
            _log_fp.write_bytes(b'\n[log truncated, keeping last 512KB]\n' + _log_fp.read_bytes()[-512000:])
        _log_file = open(_log_fp, 'a', buffering=1)

        class _Tee:
            def __init__(self, *streams):
                self.streams = streams

            def write(self, s):
                for st in self.streams:
                    try:
                        st.write(s)
                    except Exception:
                        pass

            def flush(self):
                for st in self.streams:
                    try:
                        st.flush()
                    except Exception:
                        pass

        _sys.stdout = _Tee(_sys.__stdout__, _log_file)
        _sys.stderr = _sys.stdout
    except OSError:
        pass   # 日志打不开不影响服务

    server = TTSServer(args)
    print(f'UiAgentTTS listening on {server.backend.base_url()}  voice={args.voice}  cache={args.cache_dir}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[tts] stopped', flush=True)


if __name__ == '__main__':
    main()
