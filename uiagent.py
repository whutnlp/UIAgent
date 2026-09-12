#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import base64
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "LLM base url"
DEFAULT_MODEL = "qwen3.8"
DEFAULT_API_KEY = "EMPTY"          # vLLM 默认接受任意 key，EMPTY 是惯例
DEVICE_SHOT = "/data/local/tmp/_uiagent_shot.png"
DEFAULT_RECONNECT_TARGET = "127.0.0.1:33897"   # 本机本地 hdc 的 target；掉线（list targets 为空）时自动 tconn 到这里
VLM_TIMEOUT = 60          # VLM 调用读超时（秒）：reasoning 模型慢，长任务靠它兜底防挂死
DEFAULT_REASONING_EFFORT = "medium"   # thinking 强度（thinking 开时随请求下发；服务端实测接受不报错）
MAX_TOKENS = 200000                  # 生成长度上限（thinking 的思考 token 同在此额度内）。
# 注意：服务端上下文总长 262144，要求 max_tokens + prompt ≤ 262144——直接填 262144 会被 400
# 拒绝（实测）。取 200000 留出 62K 给 prompt（系统提示+截图+历史，长任务历史会增长）。

# ---- request 动作（网络请求）----
REQUEST_METHODS = ("GET", "HEAD", "POST")   # 只放行读类+POST，禁止 PUT/DELETE 等改写类方法
REQ_MAX_PREVIEW = 800                        # 文本类响应回喂给模型的预览长度（字符）
DOWNLOAD_DIR_NAME = "uiagent_downloads"      # 下载落盘目录（与脚本同级）

# ---- sound 动作（TTS 发音）----
TTS_SERVER = "http://127.0.0.1:43030"   # tts_server.py（edge-tts 云合成+本地缓存，agent 与设备同机，走 loopback）
TTS_BUNDLE = "com.workbuddy.uiagent"    # UIAgent App 的 HAP（哑播放器载体）
TTS_ABILITY = "TtsAbility"              # 隐形 UIAbility（透明/无焦点窗口，AVPlayer 流式播 mp3，截图/输入零干扰）
TTS_TIMEOUT = 180                       # 合成 HTTP 读超时（秒）：edge-tts 云合成偶发慢（服务端自带 30s×3 重试）
SOUND_MAX_CHARS = 100                   # 单段上限（≈22s 音频 @4.5 字/秒）；超长文本本地按句边界自动拆段连播（不发回 VLM 重新生成）；
                                        # 预取：本段播放期间后台线程合成下一段，合成等待藏进播放窗口（09-11 飞哥批）

# 与 App 端 VlmClient.ets 同族（动作空间/坐标系/输出格式一致）；此处额外强化“已完成识别”，
# 防止目标已打开/在前景时仍反复点击（MatePad Edge 实测：切窗口/加载慢会诱发同位置死循环）。
# 2026-09-08 Phase 1 切 PC 桌面模式：环境声明为鸿蒙二合一平板 PC（无鼠标/无滚轮），
# 动作空间加 double_click（双击）/ long_press（长按=右键弹上下文菜单）。
SYSTEM_PROMPT = (
    'You are a computer-control GUI agent: given a screenshot + instruction, output the next action. '
    'The computer runs HarmonyOS.\n'
    'FIRST check if the goal is ALREADY met.\n'
    'Coordinates: normalized integers 0-1000 (x: 0=left..1000=right; y: 0=top..1000=bottom). '
    'Return ONLY one JSON object, no prose, in this shape (single action):\n'
    '{"thought":"...","action":"click|double_click|long_press|type|scroll|key|wait|drag|sound|done","x":null,"y":null,"x2":null,"y2":null,"velocity":null,"text":null,"dy":null,"key":null,"seconds":null,"save":null}\n'
    'Actions:\n'
    '- click: x,y = the element CENTER.\n'
    '- double_click: x,y = element CENTER. Double tap: opens a file, selects a word. Use it when a single '
    'click does not open the item.\n'
    '- long_press: x,y = element CENTER. Long press acts like RIGHT-CLICK\n'
    '- type: text AND x,y of the target field. ATOMIC: it clicks the field and types in ONE step.\n'
    '- scroll: dy (+down / -up) and x,y (the position to scroll at). The only way to move through lists/pages.\n'
    '- drag: x,y = start, x2,y2 = end, optional velocity 200-40000 (default 300): press at (x,y), hold, '
    'drag continuously to (x2,y2), release. \n'
    '- key: key = enter|back|home|tab|space|delete|esc|shift_right, "ctrl+e"-style combos (ctrl/shift + a-z), '
    'a single letter, or a raw keycode.\n'
    '- wait: seconds 1-5. Pause without touching the screen, then re-screenshot. never wait twice in a row without progress.\n'
    '- sound: text = what to SPEAK ALOUD (TTS narration). It does NOT touch the screen. Use it to narrate/explain/lecture. '
    '- done: goal reached, stop. Requires VISUAL PROOF the task is complete. '
    'Rules:\n'
    '1. IME: the Chinese IME is usually ACTIVE and intercepts typed latin text.  Then press key=shift_right ONCE to switch the IME to English. \n'
    '2. Self-correction: your OWN action history is provided - ALWAYS review it before acting. If you performed the SAME or a very similar '
    'action 2+ times and the screen has not progressed, you MUST NOT repeat it. '
    'Ask WHY it failed and take a FUNDAMENTALLY different action.\n'
    '3. THERE IS NO GIVE-UP ACTION: not_found is DISABLED, never output it.\n'
    '4. JSON SAFETY: When quoting text you see on screen inside "thought", use single quotes only - never double quotes: they break the JSON.\n'
    '5. MULTI-PAGE LECTURE: for a multi-page PPT/document lecture, explaining only the current page is NOT the end. '
    'After a page is fully explained, go to the NEXT page (slideshow mode: swipe left / click on the slide; '
    'verify the bottom page number changed), then explain the new page. '
    'Repeat until the task\'s stated end page or the last page.\n'
    'Power features (optional):\n'
    'Output only the JSON, nothing else.'
)


# ----------------------------- 基础工具 -----------------------------

TARGET = None   # resolve_target() 锁定的设备 connect-key；设置后所有 hdc 调用自动加 -t


def hdc(*args, timeout=90):
    """跑一条 hdc 命令，返回 (returncode, 合并输出)。
    若已 resolve_target() 锁定设备，自动加 -t <target>，避免多 target 时 uitest 报 need connect-key。"""
    cmd = ["hdc"]
    if TARGET:
        cmd += ["-t", TARGET]
    cmd += list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "[超时] " + " ".join(cmd)
    except FileNotFoundError:
        return -1, "找不到 hdc 命令（确认 hdc 在 PATH 上）"


def _list_targets():
    """跑 hdc list targets，返回解析出的 target 列表（过滤 [Empty]/空行/提示行）。"""
    rc, out = hdc("list", "targets")
    targets = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "Empty" in line or line.startswith("["):
            continue
        targets.append(line)
    return targets


def try_reconnect(target):
    """设备掉线（list targets 为空）时自动重连，返回 (是否连上, 解析出的 target 列表)。
    两级：① 直接 hdc tconn；② 还不行就 hdc kill 重启 hdc 服务再 tconn。"""
    print("无设备，尝试自动重连: hdc tconn %s ..." % target)
    hdc("tconn", target, timeout=30)
    time.sleep(1)
    targets = _list_targets()
    if len(targets) >= 1:
        return True, targets
    print("tconn 后仍无设备，重启 hdc 服务再试: hdc kill -> tconn %s ..." % target)
    hdc("kill", timeout=30)
    time.sleep(1)
    hdc("list", "targets")   # 顺带把 server 拉起来（client 会自动起 server）
    hdc("tconn", target, timeout=30)
    time.sleep(1)
    targets = _list_targets()
    return (len(targets) >= 1), targets


def resolve_target(cli_target, reconnect_target=None, auto_reconnect=True):
    """锁定要操作的设备 connect-key（hdc list targets 返回的标识符）。
    - 给了 --target：用它（多设备/歧义场景手动指定）。
    - 否则 list targets：恰好 1 个→锁定；多个→报错让用户选；0 个→（可选）自动重连后重试。
    多 target 时 uitest 会报 'need connect-key'——本机 127.0.0.1 与 localhost 会被当成两个。"""
    global TARGET
    targets = _list_targets()
    if cli_target:
        TARGET = cli_target
        if cli_target not in targets:
            print("（提示: --target %s 未在 list targets 里解析到，仍按原样使用）" % cli_target)
        else:
            print("设备: %s（--target 指定）" % cli_target)
        return
    if len(targets) == 1:
        TARGET = targets[0]
        print("设备: %s（自动锁定唯一 target）" % TARGET)
        return
    if len(targets) > 1:
        print("[错误] 检测到 %d 个 hdc target，uitest 无法确定用哪个（'need connect-key'）。" % len(targets))
        for t in targets:
            print("       - %s" % t)
        print("       二选一：① 重跑时加 --target <上面的标识符>；② 断开多余的: hdc tdis <标识符>")
        raise SystemExit(1)
    # len(targets) == 0：无设备，尝试自动重连
    if auto_reconnect and reconnect_target:
        ok, targets = try_reconnect(reconnect_target)
        if len(targets) == 1:
            TARGET = targets[0]
            print("设备: %s（自动重连成功）" % TARGET)
            return
        if len(targets) > 1:
            print("[提示] 自动重连后出现多个 target，请指定一个再跑：")
            for t in targets:
                print("       - %s" % t)
            print("       重跑时加 --target <上面的标识符>")
            raise SystemExit(1)
    print("[错误] 没有可用设备（hdc list targets 为空%s）。"
          % ("，自动重连 %s 也失败" % reconnect_target if (auto_reconnect and reconnect_target) else ""))
    print("       手动试: hdc tconn %s   （仍空则 hdc kill; hdc start; hdc tconn %s）"
          % (reconnect_target or "127.0.0.1:33897", reconnect_target or "127.0.0.1:33897"))
    raise SystemExit(1)


def remote_exists(path):
    """确认设备上的文件真的生成了（解析 ls 输出，不信任 hdc 的 rc）。"""
    rc, out = hdc("shell", "ls", path)
    return ("No such file" not in out) and (path in out)


def png_size(path):
    """从 PNG 头读宽高，失败返回 None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def px(norm, total):
    """0-1000 归一化 → 物理像素，越界钳制。"""
    if norm is None or total <= 0:
        return 0
    v = int(round(norm / 1000.0 * total))
    return max(0, min(total - 1, v))


def dev_quote(s):
    """为设备端 sh 把文本包成【一个】参数：用单引号包住（POSIX sh 里单引号内不做任何
    解释/展开），文本里若有单引号用 '\\'' 转义（收引号 + 转义单引号 + 再开引号）。
    带空格/中文/特殊字符的文本经此处理后才能整段传给 uitest 的 text/inputText，
    不会被空格拆成多个词（京东任务实测坑：拆多 argv 让 hdc 自行 join 会破坏引号）。"""
    return "'" + s.replace("'", "'\\''") + "'"


# ----------------------------- 眼：截图 -----------------------------

class Screen:
    """全屏截图（物理像素）。按顺序尝试 4 种方式，每种都校验文件真的落盘：
    ① snapshot_display -f <path>          （最快，部分量产 ROM 没有/静默失败）
    ② uitest screenCap -p <path>          （官方 uitest 截图，-p 仅限 /data/local/tmp/ 下）
    ③ uitest screenCap（无参）             （默认存 时间戳.png，从输出里解析路径）
    ④ snapshot_display（无参）             （部分 ROM 会把存盘路径打到 stdout）
    """

    def __init__(self, out_dir):
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.w = 0
        self.h = 0
        self.last_method = None

    @staticmethod
    def _extract_png(out):
        m = re.search(r"(/\S+?\.png)", out or "")
        return m.group(1) if m else None

    def shot(self, step):
        local = os.path.join(self.out_dir, "shot_step%d.png" % step)
        hdc("shell", "rm", "-f", DEVICE_SHOT)   # 先清掉旧图，防止误用上一张
        remote = None
        errors = []
        # ① snapshot_display -f
        rc, out = hdc("shell", "snapshot_display", "-f", DEVICE_SHOT)
        if rc == 0 and remote_exists(DEVICE_SHOT):
            remote, self.last_method = DEVICE_SHOT, "snapshot_display -f"
        else:
            errors.append("[snapshot_display -f] rc=%s: %s" % (rc, out.strip()))
        # ② uitest screenCap -p
        if remote is None:
            rc, out = hdc("shell", "uitest", "screenCap", "-p", DEVICE_SHOT)
            if rc == 0 and remote_exists(DEVICE_SHOT):
                remote, self.last_method = DEVICE_SHOT, "uitest screenCap -p"
            else:
                errors.append("[uitest screenCap -p] rc=%s: %s" % (rc, out.strip()))
        # ③ uitest screenCap（无参，从输出解析路径）
        if remote is None:
            rc, out = hdc("shell", "uitest", "screenCap")
            path = self._extract_png(out)
            if rc == 0 and path and remote_exists(path):
                remote, self.last_method = path, "uitest screenCap(解析路径)"
            else:
                errors.append("[uitest screenCap] rc=%s: %s" % (rc, out.strip()))
        # ④ snapshot_display（无参，从输出解析路径）
        if remote is None:
            rc, out = hdc("shell", "snapshot_display")
            path = self._extract_png(out)
            if rc == 0 and path and remote_exists(path):
                remote, self.last_method = path, "snapshot_display(解析路径)"
            else:
                errors.append("[snapshot_display] rc=%s: %s" % (rc, out.strip()))
        if remote is None:
            raise RuntimeError("截图全部方式失败:\n  " + "\n  ".join(errors))
        rc, out = hdc("file", "recv", remote, local)
        if rc != 0 or not os.path.exists(local) or os.path.getsize(local) == 0:
            raise RuntimeError("hdc file recv 失败(%s): %s" % (remote, out.strip()))
        size = png_size(local)
        if size:
            self.w, self.h = size
        return local


# ----------------------------- 脑：VLM -----------------------------

VALID_ACTIONS = ("click", "double_click", "long_press", "type", "scroll", "key", "wait", "drag",
                 "request", "sound", "done", "not_found")


def _blank_action():
    return {"thought": "", "action": "", "x": None, "y": None, "text": None,
            "dy": None, "key": None, "seconds": None,
            "x2": None, "y2": None, "velocity": None,
            "url": None, "method": None, "data": None, "save": None, "headers": None}


def _norm_fields(a, obj):
    for k in ("thought", "action", "text", "key", "url", "method", "data", "save"):
        v = obj.get(k)
        if isinstance(v, str) and v != "":
            a[k] = v
    for k in ("x", "y", "dy", "seconds", "x2", "y2", "velocity"):
        v = obj.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            a[k] = v
    # 模型偶发（xhigh thinking 实测）把坐标输出成二元数组 "x":[587,959],"y":[587,959] —— 恢复：首元素=x，次元素=y
    def _pair(v):
        return isinstance(v, (list, tuple)) and len(v) == 2 and \
            all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in v)
    if _pair(obj.get("x")):
        a["x"], a["y"] = obj["x"][0], obj["x"][1]
    elif a["x"] is None and _pair(obj.get("y")):
        a["x"], a["y"] = obj["y"][0], obj["y"][1]
    h = obj.get("headers")
    if isinstance(h, dict):
        a["headers"] = {str(k): str(v) for k, v in h.items() if isinstance(v, (str, int))}
    return a


class ActionParseError(ValueError):
    """模型输出解析不出合法动作（JSON 截断/非法/缺 action）。
    本地模式必须抛错交重试层，绝不能兜底成 done——截断 JSON 误判 done = 静默假完成
    （WPS 任务 step4 事故：输出在 "action": 处截断，旧逻辑默认 done，4 步即"结束: done"）。"""


def parse_action(raw, strict=True):
    """从第一个 { 到最后一个 } 抠 JSON（与 App 端一致）解析动作。
    strict=True（本地模式默认）：解析不出合法动作 → 抛 ActionParseError 交重试层，
    绝不兜底 done（假完成比报错糟糕得多）。
    strict=False（App 端旧行为）：解析不了安全地当 done。
    两个扩展（App 端没有）：
    - "actions":[{...},...] 批量动作：每个元素按单动作解析，只保留 action 合法的 → a["actions"]
    - "view":[step 号] 且未给出可用动作 → a["action"]="__view__"（请求回溯历史截图）"""
    a = _blank_action()
    a["thought"] = (raw or "")[:200]
    a["action"] = "done"
    a["actions"] = None
    a["view"] = None
    r = raw or ""
    i0, i1 = r.find("{"), r.rfind("}")
    if i0 < 0 or i1 <= i0:
        if strict:
            raise ActionParseError("输出里没有完整 JSON 对象（可能被截断）: %r..." % r[:120])
        return a
    try:
        obj = json.loads(r[i0:i1 + 1])
    except Exception as e:
        if strict:
            raise ActionParseError("JSON 解析失败: %s; 输出: %r..." % (e, r[:120]))
        return a
    if not isinstance(obj, dict):
        if strict:
            raise ActionParseError("JSON 顶层不是对象: %r" % str(obj)[:120])
        return a
    _norm_fields(a, obj)
    explicit = isinstance(obj.get("action"), str) and obj.get("action") in VALID_ACTIONS
    arr = obj.get("actions")
    if isinstance(arr, list):
        subs = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            sa = _norm_fields(_blank_action(), item)
            if sa["action"] in VALID_ACTIONS:
                subs.append(sa)
        if subs:
            a["actions"] = subs
            a["action"] = ""   # 批量模式：清掉默认的 done，防止被主循环误判成"任务完成"
    if not explicit:
        v = obj.get("view")
        if isinstance(v, list):
            nums = [int(n) for n in v
                    if isinstance(n, (int, float)) and not isinstance(n, bool) and n >= 1]
            if nums:
                a["action"] = "__view__"
                a["view"] = nums
    # 注意必须用 explicit 判断而非 a["action"]：默认初值就是 "done"，"JSON 合法但缺 action 字段"
    # 时 a["action"] 仍是默认 done，按值判断会漏掉——只有显式给出合法 action/批量/view 才算解析成功
    if strict and not explicit and a["action"] != "__view__" and a["actions"] is None:
        raise ActionParseError("JSON 合法但未给出可用动作（action=%r）: %r..." % (obj.get("action"), r[:120]))
    return a


class Vlm:
    """OpenAI 兼容调用。
    thinking 模式（默认开）：chat_template_kwargs.enable_thinking=True + reasoning_effort（默认 xhigh），
    模型先思考后输出 JSON；思考 token 计入 max_tokens（统一 MAX_TOKENS=200000，服务端上下文总长 262144
    要求 max_tokens+prompt 一并装下，直接填 262144 会被 400 拒）。
    返回 (a, ms, think_chars)——think_chars 为思考内容字符数（thinking 关时 0）。"""

    def __init__(self, base_url, model, api_key, enable_thinking=True, reasoning_effort="xhigh"):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.api_key = api_key
        self.enable_thinking = enable_thinking
        self.reasoning_effort = reasoning_effort

    def next_action(self, img_path, instruction, feedback, history_text="",
                    extra_images=None, view_note="", fix_note=""):
        """extra_images: [(标注, 本地路径), ...] 要一起附上的历史截图（排在当前截图之前）；
        view_note: 随历史截图附带的说明（如"对比后请直接输出动作"）；
        fix_note: 纠正式提示（重试层在上一轮 JSON 解析失败时附加：同输入盲重发必同型失败，
        必须改输入——明确告知模型上条输出不是合法 JSON，要求只输出合法 JSON 对象）。"""
        with open(img_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        parts = [instruction]
        if extra_images:
            labels = ["%s" % lab for lab, _ in extra_images]
            parts.append("[附图说明: " + "；".join(labels) + "；图%d=当前截图(最新)]" % (len(labels) + 1))
            if view_note:
                parts.append(view_note)
        if history_text:
            parts.append("[你的历史动作记录（最近几步，含当时的思路与执行结果）:\n" + history_text + "]")
        if feedback:
            parts.append("[上一步结果: %s]" % feedback)
        if fix_note:
            parts.append(fix_note)
        text = "\n".join(parts)
        content = [{"type": "text", "text": text}]
        for _lab, p in (extra_images or []):
            with open(p, "rb") as f:
                content.append({"type": "image_url",
                                "image_url": {"url": "data:image/png;base64," + base64.b64encode(f.read()).decode()}})
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + b64}})
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "chat_template_kwargs": {"enable_thinking": bool(self.enable_thinking)},
        }
        if self.enable_thinking and self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Authorization", "Bearer " + self.api_key)
        req.add_header("Content-Type", "application/json")
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=VLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        ms = int((time.time() - t0) * 1000)
        choice = data["choices"][0]
        msg = choice["message"]
        raw = (msg.get("content") or "").strip()
        if choice.get("finish_reason") == "length":
            # 输出被 token 上限截断：JSON 很可能不完整。strict parse 会抛 ActionParseError 交重试层；
            # 即使这次碰巧解析成功，也要在日志留痕（定位：服务端输出上限 vs thinking 烧额度）
            print("[警告] VLM 输出被 token 上限截断（finish_reason=length），输出结尾: %r" % (raw[-100:] if raw else "空"))
        if not raw:
            # content 为空 = 思考把 max_tokens 烧光了被截断。必须抛错交给重试层，
            # 不能拿 reasoning 字段兜底——那里面是思考文本不是 JSON
            raise RuntimeError("VLM 返回空 content（可能 thinking 被 max_tokens 截断；"
                               "可 --no-thinking 或调大 MAX_TOKENS）")
        think_chars = len(msg.get("reasoning") or msg.get("reasoning_content") or "")
        # strict=True（默认）：截断/非法 JSON 抛 ActionParseError → _vlm_call 重试层，绝不兜底 done
        return parse_action(raw), ms, think_chars


# ----------------------------- 手：uitest 注入 -----------------------------

FAIL_MARKERS = ("incorrect", "Missing parameter", "Invalid parameters", "Too many parameters",
                "Please confirm", "out of range", "not supported")


def uitest_result(out):
    """看输出文案判 uitest 成败，不信退出码：hdc shell 常常不传回远端进程退出码
    （实测参数错误时 rc 也是 0）。成功文案是 'No Error'，失败带特定措辞。"""
    if "No Error" in out:
        return True
    return not any(k in out for k in FAIL_MARKERS)


def uitest(*args, timeout=30):
    """uitest uiInput 子命令。返回 (ok, 输出)。
    关键实现：把整条命令拼成一个字符串，作为【单个参数】交给 hdc shell，
    由设备端 sh -c 按 shell 规则解析（引号/空格/转义）。
    之前把各参数拆成多个 argv 让 hdc 自行 join，文本里的引号在 hdc 的拼接/转义中
    被破坏，带空格文本被拆成多个词 → uitest 报 'The number of parameters is incorrect'。"""
    cmd_str = "uitest uiInput " + " ".join(args)
    rc, out = hdc("shell", cmd_str, timeout=timeout)
    out = out.strip()
    ok = (rc == 0) and uitest_result(out)
    return ok, out


# --display-id：uitest uiInput 各子命令支持尾参 [displayId]（Phase 0 实测）；None=不追加（主屏）
DISPLAY_ID = None


def _display_suffix():
    return [str(DISPLAY_ID)] if DISPLAY_ID is not None else []


# uitest keyEvent 键码（官方 @ohos.multimodalInput.keyCode 表逐值核实，oh_key_code.h）
# 注意：2000~2009 是数字 0~9（2005=数字5，不是 Esc！）；Esc=2070；Ctrl+X=2072+字母键（CLI 支持 2~3 键组合）
KEY_MAP = {
    "enter": "2054", "return": "2054",
    "back": "Back", "home": "Home", "power": "Power",
    "tab": "2049", "space": "2050",
    "delete": "2055", "backspace": "2055", "del": "2055",
    "esc": "2070", "escape": "2070",
    "shift": "2047", "shift_right": "2048",   # 右 Shift=2048（官方表核实）：本机上切换输入法中/英
    "rshift": "2048",
    "ctrl": "2072", "control": "2072",
    "f6": "2095",
    "ctrl_a": "2072 2017", "ctrl_c": "2072 2019", "ctrl_v": "2072 2038",
}


def _key_token(tok):
    """解析单个键 token：KEY_MAP 名 / 单字母 a-z（A=2017..Z=2042 连续，官方表核实）/ f1-f12（2090-2101）/ 纯数字键码。未知返回 None。"""
    t = (tok or "").strip().lower()
    if t in KEY_MAP:
        return KEY_MAP[t]
    if re.fullmatch(r"[a-z]", t):
        return str(2017 + ord(t) - ord("a"))
    m = re.fullmatch(r"f([1-9]|1[0-2])", t)
    if m:
        return str(2089 + int(m.group(1)))
    if re.fullmatch(r"\d+", t):
        return t
    return None


def key_code(name):
    """key 名 → uitest keyEvent 参数（空格分隔 1~3 个键码，设备端 CombinedKeys，ui_input.cpp 源码核实）。
    兼容全部写法：enter/esc/shift_right 等名、"ctrl+e"/"control e"/"ctrl-e" 式组合（→ "2072 2021"）、
    单字母（a=2017）、f1-f12、原始数字键码、数字组合（"2072 2017"）。未知返回 None。"""
    n = (name or "").strip().lower()
    if not n:
        return None
    if n in KEY_MAP:
        return KEY_MAP[n]
    parts = [p for p in re.split(r"[+\s\-]+", n) if p]
    if len(parts) == 1:
        return _key_token(parts[0])
    if 2 <= len(parts) <= 3:
        codes = [_key_token(p) for p in parts]
        if None not in codes:
            return " ".join(codes)
    return None


def exec_action(a, screen, args=None):
    """执行一个动作，返回 (ok, 描述/错误信息)。坐标是物理像素。request 动作不需要屏幕。"""
    if a["action"] == "click":
        if a.get("x") is None or a.get("y") is None:
            return False, "click 缺坐标"
        x, y = px(a["x"], screen.w), px(a["y"], screen.h)
        ok, out = uitest("click", str(x), str(y), *_display_suffix())
        return ok, "click(%d,%d) %s" % (x, y, out if not ok else "OK")
    if a["action"] == "double_click":
        # 双击（uitest doubleClick，Phase 0 实测存在）：打开文件/选中词
        if a.get("x") is None or a.get("y") is None:
            return False, "double_click 缺坐标"
        x, y = px(a["x"], screen.w), px(a["y"], screen.h)
        ok, out = uitest("doubleClick", str(x), str(y), *_display_suffix())
        return ok, "double_click(%d,%d) %s" % (x, y, out if not ok else "OK")
    if a["action"] == "long_press":
        # 长按（uitest longClick，Phase 0 实测存在）= 右键：弹上下文菜单（打开方式/重命名/属性/…），随后 click 菜单项
        if a.get("x") is None or a.get("y") is None:
            return False, "long_press 缺坐标"
        x, y = px(a["x"], screen.w), px(a["y"], screen.h)
        ok, out = uitest("longClick", str(x), str(y), *_display_suffix())
        return ok, "long_press(%d,%d) %s" % (x, y, out if not ok else "OK")
    if a["action"] == "type":
        t = a.get("text") or ""
        if a.get("x") is not None and a.get("y") is not None:
            x, y = px(a["x"], screen.w), px(a["y"], screen.h)
            if t == "":
                ok, out = uitest("click", str(x), str(y), *_display_suffix())
                return ok, "click-only(%d,%d) %s" % (x, y, out if not ok else "OK")
            # inputText <x> <y> <text>：uitest 点该点、等 500ms 聚焦、再注入文本，一步到位。
            # 注意 CLI 的 inputText 必须 3 参全给——只给文本是参数错误、静默无效（实测坑）。
            # 文本用 dev_quote 包成单个参数，整条命令作为单字符串交给设备端 sh 解析。
            ok, out = uitest("inputText", str(x), str(y), dev_quote(t), *_display_suffix())
            return ok, "type %r @(%d,%d) %s" % (t, x, y, out if not ok else "OK")
        if t == "":
            return False, "type 缺文本且缺坐标"
        if re.fullmatch(r"[\r\n\t]+", t):
            # 模型拿换行/制表符表达"按回车"（京东任务实测行为）→ 直接映射成 Enter 键
            ok, out = uitest("keyEvent", KEY_MAP["enter"], *_display_suffix())
            return ok, "key enter（由 %r 自动转换）%s" % (t, out if not ok else "OK")
        # text <text>：注入当前已聚焦的输入框（需上一步已点中聚焦）
        ok, out = uitest("text", dev_quote(t), *_display_suffix())
        return ok, "type %r (focused) %s" % (t, out if not ok else "OK")
    if a["action"] == "key":
        code = key_code(a.get("key"))
        if code is None:
            return False, "未知按键 %r（支持 enter/back/home/tab/space/delete/esc/shift_right、单字母 a-z、f1-f12、" \
                          "ctrl+e 式组合、或原始键码/数字组合）" % (a.get("key"),)
        ok, out = uitest("keyEvent", code, *_display_suffix())
        return ok, "key %s %s" % (a.get("key"), out if not ok else "OK")
    if a["action"] == "scroll":
        cx = px(a["x"], screen.w) if a.get("x") is not None else screen.w // 2
        cy = px(a["y"], screen.h) if a.get("y") is not None else screen.h // 2
        dy = a.get("dy") if a.get("dy") is not None else 1
        dist = min(240, max(60, screen.h // 12))
        if dy >= 0:  # 内容下移 = 手指从下往上滑
            y1, y2 = min(screen.h - 1, cy + dist // 2), max(0, cy - dist // 2)
        else:        # 内容上移 = 手指从上往下滑
            y1, y2 = max(0, cy - dist // 2), min(screen.h - 1, cy + dist // 2)
        ok, out = uitest("swipe", str(cx), str(y1), str(cx), str(y2), "1000", *_display_suffix())
        return ok, "swipe(%d,%d)->(%d,%d) %s" % (cx, y1, cx, y2, out if not ok else "OK")
    if a["action"] == "drag":
        # drag = touch 源按住起点连续拖到终点再松开（一次完整手势；滚动/滑条/选中文本/拖物体，
        # 指针独占画布不响应）。uitest 的 swipe/drag 是同一条命令（源码 ui_input.cpp:300），第 5 参是【速度 pps】
        # 范围 200~40000（不是时长！），默认 600；内部按 ~50 步插值移动点，画布 App 记为连续笔画。
        if any(a.get(k) is None for k in ("x", "y", "x2", "y2")):
            return False, "drag 缺坐标（需起点 x,y 和终点 x2,y2）"
        x1, y1 = px(a["x"], screen.w), px(a["y"], screen.h)
        x2, y2 = px(a["x2"], screen.w), px(a["y2"], screen.h)
        if x1 == x2 and y1 == y2:
            return False, "drag 起点与终点相同（零长度拖动无效，请给不同的终点）"
        try:
            vel = int(a.get("velocity") or 300)
        except (TypeError, ValueError):
            vel = 300
        vel = max(200, min(40000, vel))   # 钳制到官方范围；默认 300（慢=笔画实，快=甩动）
        ok, out = uitest("drag", str(x1), str(y1), str(x2), str(y2), str(vel), *_display_suffix())
        return ok, "drag(%d,%d)->(%d,%d) v=%d %s" % (x1, y1, x2, y2, vel, out if not ok else "OK")
    if a["action"] == "wait":
        try:
            sec = float(a.get("seconds") or 2)
        except (TypeError, ValueError):
            sec = 2.0
        sec = max(1.0, min(10.0, sec))   # 钳制 1~10 秒，防模型给离谱值空烧时间
        time.sleep(sec)
        return True, "wait %ds（暂停等加载/过渡，不碰屏幕）" % int(sec)
    if a["action"] == "request":
        return exec_request(a, args)
    if a["action"] == "sound":
        return exec_sound(a, args, screen)
    return False, "未识别的动作: %s" % a["action"]


# ----------------------------- 网：HTTP 请求 -----------------------------

def _safe_filename(name, fallback):
    """把模型给的保存名清成安全的本地文件名：取 basename、去危险字符、空白转下划线、限长 120。"""
    n = (name or "").strip().replace("\\", "/")
    n = os.path.basename(n)
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", n)
    n = re.sub(r"\s+", "_", n).strip("._ ")
    if len(n) > 120:
        n = n[:120].rstrip("._ ")
    return n or fallback


def _derive_save_name(resp, url):
    """没给 save 时的默认文件名：优先 Content-Disposition（含 UTF-8'' 编码），其次 URL 路径段，
    再不行用时间戳兜底。"""
    cd = resp.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.I)
    if m:
        try:
            n = urllib.parse.unquote(m.group(1).strip())
        except Exception:
            n = ""
        if n:
            return n
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    if m and m.group(1).strip():
        return m.group(1).strip()
    base = os.path.basename(urllib.parse.urlsplit(url).path)
    if base:
        return base
    return "download_%d.bin" % int(time.time())


def exec_request(a, args):
    """执行 request 动作：在屏幕之外发起 HTTP 请求（纯标准库 urllib，App/设备完全无感）。
    - 文本类响应（html/json/text 等）→ 回喂 状态码/类型/大小/预览 给模型读；
    - 文件类（二进制）或指定了 save → 流式落盘到 uiagent_downloads/（重名自动加序号不覆盖），
      二进制响应即使没给 save 也自动落盘（否则模型拿不到文件）；
    - HEAD → 只验链接是否活着（返回 Content-Length）。
    返回 (ok, info)：ok=False 表示这次请求失败（死链/网络错/超限）——是"这条链接不行"，
    不是任务失败，模型应换链接/换入口继续，脚本不停止。"""
    url = (a.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return False, "request 的 url 必须以 http:// 或 https:// 开头（收到: %r）" % url[:120]
    method = (a.get("method") or "GET").strip().upper()
    if method not in REQUEST_METHODS:
        return False, "request 方法只支持 GET/HEAD/POST（收到 %r）" % method
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", "Mozilla/5.0 (Linux; OpenHarmony) UIAgent/1.0")
    hdrs = a.get("headers")
    if isinstance(hdrs, dict):
        for k, v in hdrs.items():
            if k and k.lower() not in ("host", "content-length", "user-agent"):
                req.add_header(k, v)
    body = None
    if method == "POST" and a.get("data"):
        body = a["data"].encode("utf-8")
    timeout = getattr(args, "req_timeout", 60) if args is not None else 60
    max_bytes = (getattr(args, "max_download_mb", 2048) if args is not None else 2048) * 1024 * 1024
    dl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), DOWNLOAD_DIR_NAME)
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, data=body, timeout=timeout)
    except urllib.error.HTTPError as e:
        return False, "HTTP %s %s（该链接打不开/已失效——回页面找【另一条】链接，或换搜索词/换站点）" % (e.code, url[:120])
    except Exception as e:
        return False, "请求失败: %s（网络/DNS/超时——检查 url 拼写，或换个网络入口再试）" % str(e)[:160]
    status = resp.getcode()
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    clen = resp.headers.get("Content-Length")
    try:
        if method == "HEAD":
            resp.close()
            return True, "HEAD %s → %s %s %sB %.1fs（链接有效；要下载请改用 GET 并带 \"save\"）" % (
                url[:100], status, ctype or "未知类型", clen or "?", time.time() - t0)
        textish = (ctype.startswith("text/") or ctype in (
            "application/json", "application/xml", "text/xml", "application/javascript",
            "application/x-javascript") or ctype == "")
        if a.get("save") or not textish:
            # —— 落盘下载 ——
            derived = _derive_save_name(resp, url)
            if a.get("save"):
                name = _safe_filename(a.get("save"), derived)
            else:
                name = _safe_filename(derived, "download_%d.bin" % int(time.time()))
            os.makedirs(dl_dir, exist_ok=True)
            path = os.path.join(dl_dir, name)
            base, ext = os.path.splitext(path)
            i = 1
            while os.path.exists(path):   # 重名加序号，不覆盖已有文件
                path = "%s_%d%s" % (base, i, ext)
                i += 1
            total = 0
            tmp = path + ".part"
            try:
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise RuntimeError("超过单次下载上限 %dMB" % (max_bytes // 1024 // 1024))
                        f.write(chunk)
                os.replace(tmp, path)
            except Exception as e:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return False, "下载中断: %s（部分文件已丢弃；可能是假链接/限速——换另一条链接试）" % str(e)[:160]
            resp.close()
            return True, "%s %s → %s %s %.1fMB %.1fs | 已保存到: %s" % (
                method, url[:90], status, ctype or "未知类型", total / 1048576.0, time.time() - t0, path)
        raw = resp.read(6000)
        resp.close()
        preview = raw.decode("utf-8", errors="replace")
        preview = re.sub(r"\s+", " ", preview).strip()
        if len(preview) > REQ_MAX_PREVIEW:
            preview = preview[:REQ_MAX_PREVIEW] + "…"
        return True, "GET %s → %s %s %sB %.1fs | 预览: %s" % (
            url[:100], status, ctype or "未知类型", clen or "%d+" % len(raw), time.time() - t0, preview)
    finally:
        try:
            resp.close()
        except Exception:
            pass


# ----------------------------- 声：TTS 发音 -----------------------------

def _ensure_tts_server(timeout=15):
    """启动时确保 tts_server 在跑：/health 有响应立即返回（与已有实例共存，不抢端口）；
    不活则用与 uiagent 相同的 python 后台拉起同目录 tts_server.py（输出丢弃、独立会话，
    uiagent 退出不会带走它），再轮询 health 至多 timeout 秒。拉起失败不退出进程——
    由 exec_sound 现有「TTS 服务不可达」回喂机制兜底。返回 (ok, msg) 供启动日志。"""
    def _health():
        try:
            req = urllib.request.Request(TTS_SERVER + "/health")
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read().decode())
        except Exception:
            return None
    h = _health()
    if h is not None:
        if h.get("edge_tts") is False:
            print("[警告] tts_server 在跑但 edge_tts 不可用：只能播缓存句，"
                  "对应 python 执行 python3 -m pip install edge-tts 后云合成才可用")
        return True, "tts_server 已在运行"
    server_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_server.py")
    if not os.path.exists(server_path):
        return False, "未找到 tts_server.py（应与 uiagent.py 同目录），新句合成将失败，缓存句不受影响"
    try:
        subprocess.Popen([sys.executable, server_path], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return False, "拉起 tts_server.py 失败: %s" % e
    for _ in range(timeout):
        time.sleep(1)
        h = _health()
        if h is not None:
            if h.get("edge_tts") is False:
                print("[警告] tts_server 自动拉起成功但 edge_tts 不可用：只能播缓存句，"
                      "对应 python 执行 python3 -m pip install edge-tts 后云合成才可用")
            return True, "tts_server 已自动拉起"
    return False, "tts_server 拉起超时（%d 秒未就绪），发音时会按需再试" % timeout


def _split_sound_text(t, max_chars=SOUND_MAX_CHARS):
    """本地拆句：≤max_chars 原样返回单元素列表；超长则每段 ≤max_chars 切分。
    每窗内优先在最后一个句边界（。！？!?；;\\n）处切（边界字符留在前段尾），
    窗内无可用边界（距窗首 ≥30 字）才硬切 max_chars。纯函数：同文本拆法恒定，
    各段可跨次调用稳定命中 TTS 缓存。"""
    if len(t) <= max_chars:
        return [t]
    segs = []
    start, n = 0, len(t)
    while start < n:
        end = min(start + max_chars, n)
        if end < n:
            cut = start
            for b in "。！？!?；;\n":
                p = t.rfind(b, start + 30, end)   # 边界前至少 30 字，避免切出碎段
                if p > cut:
                    cut = p
            if cut > start:
                end = cut + 1
        segs.append(t[start:end])
        start = end
    segs = [s for s in segs if s.strip()]
    # 尾部碎段（<30 字）并入前段（前段允许小幅超上限），避免孤立的几个字单独成段起播
    while len(segs) > 1 and len(segs[-1]) < 30:
        segs[-2] += segs[-1]
        segs.pop()
    return segs


def _seg_tail(i, total):
    """多段拆播中第 i 段失败时，回喂已读/未读进度，让模型精确补讲。"""
    if total <= 1:
        return ""
    return ("（本句已拆 %d 段连播，第 %d 段失败，前 %d 段已读，余 %d 段未读，请补讲）"
            % (total, i, i - 1, total - i + 1))


def _tts_synth(seg):
    """向 TTS 服务合成一段，返回 (ok, payload_or_errmsg)；失败时 errmsg 即面向模型的反馈文案。
    无副作用，主线程与预取线程均可调（tts_server 是 ThreadingHTTPServer，并发合成互不阻塞）。"""
    req = urllib.request.Request(TTS_SERVER + "/tts",
                                 data=json.dumps({"text": seg}).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TTS_TIMEOUT) as resp:
            return True, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return False, ("TTS 合成 HTTP %s（%s）——可稍后重试这句话或先继续其它操作"
                       % (e.code, str(e)[:120]))
    except Exception as e:
        return False, ("TTS 服务不可达: %s（确认 tts_server.py 已启动: python3 tts_server.py；"
                       "发音失败不阻塞屏幕操作，可先继续其它操作）"
                       % str(e)[:160])


class _Prefetch:
    """预取：本段播放期间，后台线程合成下一段，把 10~20s 合成等待藏进播放窗口。
    预取失败/超时由调用方降级为主线程当场合成（等价串行行为），不引入新失败模式。"""

    def __init__(self, seg):
        self.result = None   # (ok, payload_or_errmsg)，预取线程完成后置
        self._t = threading.Thread(target=self._run, args=(seg,), daemon=True)
        self._t.start()

    def _run(self, seg):
        self.result = _tts_synth(seg)

    def wait(self, timeout=None):
        """阻塞到预取完成；True=已完成可取 result，False=超时（调用方降级当场合成）。"""
        self._t.join(timeout if timeout is not None else TTS_TIMEOUT + 30)
        return self.result is not None


# ---------------- sound 流式路径（09-11 23:5x：流式为主，逐段为 fallback） ----------------

PLAY_HEADROOM = 0.5       # aa start → 出声（文件就绪路径：缓存命中/逐段 fallback；09-11 实测 0.09~0.18s，取 0.5 保守）
PLAY_START_MARGIN = 0.3   # 流式：首字节 → 出声（09-11 探测 hilog 实测 0.09~0.18s，取 0.3 保守）
PLAY_TAIL_MARGIN = 0.5    # 流式：音频末字节 → 翻页（解码尾/时钟抖动余量）
FIRST_AUDIO_FALLBACK = 2.5  # 流式：旧服务无 first_byte_rel 字段 → edge-tts 首包延迟经验中上值（1~4s）
STREAM_MAX_CHARS = 600    # 流式主路径单条文本上限；超过走本地拆句+逐段播放（fallback 路径）
_STREAM_UNSUPPORTED = [False]   # 本 tts_server 版本无 /tts_stream 时置 True，本次运行不再试


def _tts_stream_post(text):
    """POST /tts_stream（服务器秒回流 url，或缓存命中直接给缓存 url）。
    返回 (status, data)：
      (None, None)        —— 服务不可达（调用方走逐段 fallback）
      ("unsupported", N)  —— 旧服务版本（无流式端点），本次运行不再试（走 fallback）
      (True, dict)        —— 成功
      (False, errmsg)     —— 请求被拒（HTTP 400/502 等）
    """
    req = urllib.request.Request(TTS_SERVER + "/tts_stream",
                                 data=json.dumps({"text": text}).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            _STREAM_UNSUPPORTED[0] = True
            return "unsupported", None
        return False, "TTS 流式接口 HTTP %s（%s）" % (e.code, str(e)[:120])
    except Exception:
        return None, None


def _first_audio_rel(sd):
    """流式 pacing 基准：POST 返回 → 出声的秒数（服务器实测：
    max(首块生成, 播放器连入) + 首字节→出声）。
    音频从未开始（服务器未上报 first_byte_rel）时返回 None。"""
    fb = sd.get("first_byte_rel")
    if fb is None:
        return None
    try:
        fb = float(fb)
    except (TypeError, ValueError):
        fb = 0.0
    pc = sd.get("connect_rel")
    try:
        pc = float(pc) if pc is not None else 0.0
    except (TypeError, ValueError):
        pc = 0.0
    return max(fb, pc, 0.0) + PLAY_START_MARGIN


def _sound_page_turn(screen):
    """全部音频播完后自动左滑翻一页（放映模式=下一页；滑动非点击，不会误触发 PPT 超链接）。"""
    if screen is None:
        return ""
    x1, x2, y = screen.w * 3 // 4, screen.w // 4, screen.h // 2
    turn_ok, turn_out = uitest("swipe", str(x1), str(y), str(x2), str(y), "1000", *_display_suffix())
    return " | 翻页swipe OK" if turn_ok else " | 翻页swipe失败: %s" % turn_out[:60]


def _exec_sound_stream(t, screen):
    """流式主路径：POST /tts_stream → aa start（AVPlayer 渐进播放，~2-4s 出声，
    09-11 实测：文件传到 8% 即 playing；首包延迟大头= edge-tts 首块生成 1~4s）
    → 轮询 /tts_status 拿实测总时长（按实际生成字节测得）+ 实测首音延迟
    （服务器同时量得"首块生成"与"播放器连入"时刻）
    → sleep 到 (POST + 首音 + 实测时长 + 尾余量) → 翻页。
    早期失败（出声还很少）自动重试一次（用户无感）。
    返回 (ok, info)；None = 流式不可用（不可达/旧服务/超长），调用方走逐段 fallback。"""
    if _STREAM_UNSUPPORTED[0] or len(t) > STREAM_MAX_CHARS:
        return None
    T0 = time.perf_counter()
    for attempt in (1, 2):
        _ta = time.perf_counter()   # 流请求开始
        st, data = _tts_stream_post(t)
        _t_post = time.perf_counter()
        if st in (None, "unsupported"):
            return None
        if not st:
            return False, data + "（本句没读出，可稍后重试这句话或先继续其它操作）"
        url = (data.get("url") or "").strip()
        if not url:
            return False, "TTS 服务未返回音频 url: %s" % str(data)[:160]
        # 缓存命中（id=None）：直接播缓存 url，无需轮询
        if not data.get("id"):
            try:
                sec = float(data.get("seconds") or 0)
            except (TypeError, ValueError):
                sec = 0.0
            rc, out = hdc("shell", "aa", "start", "-b", TTS_BUNDLE, "-a", TTS_ABILITY,
                          "--ps", "url", url, timeout=30)
            if rc != 0 or "successfully" not in out.lower():
                return False, ("拉起 TtsAbility 失败: rc=%s %s（确认 HAP 已安装: "
                               "hdc install -r harmony/entry/build/default/outputs/default/entry-default-signed.hap）"
                               % (rc, out.strip()[:160]))
            if sec > 0:
                time.sleep(sec + 0.5)
            preview = t if len(t) <= 40 else t[:37] + "..."
            return True, ("已播放 %.1fs 音频 %r（流式端缓存命中）%s"
                          % (sec, preview, _sound_page_turn(screen)))
        # 流式：先起播（渐进），不等合成
        t_post_wall = time.time()      # 墙钟：POST 返回时刻（与服务器 job.created 同基准，pacing 用）
        t_aa_wall = time.time()        # 墙钟：起播时刻（统计用）
        _t_aa = time.perf_counter()    # perf：aa start 开始（耗时统计用）
        rc, out = hdc("shell", "aa", "start", "-b", TTS_BUNDLE, "-a", TTS_ABILITY,
                      "--ps", "url", url, timeout=30)
        _t_aa_end = time.perf_counter()
        if rc != 0 or "successfully" not in out.lower():
            return False, ("拉起 TtsAbility 失败: rc=%s %s（确认 HAP 已安装: "
                           "hdc install -r harmony/entry/build/default/outputs/default/entry-default-signed.hap）"
                           % (rc, out.strip()[:160]))
        sid = data["id"]
        # 轮询合成进度：合成 ~1.5 倍实时，通常藏进播放窗口；来不及则音频中途卡住
        # （网络劣化），继续等合成完成，stall 计入耗时行
        est_total = len(t) / 4.5
        deadline = time.time() + max(120.0, est_total * 2.0)
        total_sec = None
        t_done_wall = None
        _t_done = None
        sd_done = None
        early_retry = False
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                req = urllib.request.Request(TTS_SERVER + "/tts_status/" + sid)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    sd = json.loads(resp.read().decode())
            except Exception:
                continue   # 单次轮询失败（服务忙），重试
            if sd.get("state") == "done":
                try:
                    total_sec = float(sd.get("total_seconds") or 0)
                except (TypeError, ValueError):
                    total_sec = est_total
                t_done_wall = time.time()
                _t_done = time.perf_counter()
                sd_done = sd
                break
            if sd.get("state") == "failed":
                _fa_fail = _first_audio_rel(sd)
                heard = (max(0.0, time.time() - t_post_wall - _fa_fail)
                         if _fa_fail is not None else 0.0)
                if heard < 6.0 and attempt == 1:
                    early_retry = True   # 早期失败：出声还很少，跳出轮询换新 sid 重试
                else:
                    heard_chars = min(len(t), int(heard * 4.5))
                    return False, ("TTS 流式合成中途失败: %s（已读约前 %d/%d 字，请补讲余下或先继续其它操作）"
                                   % (str(sd.get("error") or "未知错误")[:120], heard_chars, len(t)))
                break
        if early_retry:
            continue   # for attempt：早期失败自动重试一次（用户无感）
        if total_sec is None:
            return False, "TTS 流式合成超时（%.0fs 内无结果，可稍后重试这句话）" % (time.time() - t_aa_wall)
        # 睡到音频结束（09-12 修"声音没播完就翻页"：出声基准从"aa start + 0.5s"改为服务器实测
        # "POST + max(首块生成, 播放器连入) + 首字节→出声"——旧基准没算 edge-tts 首包延迟 1~4s）
        fa = _first_audio_rel(sd_done)
        if fa is None:
            fa = FIRST_AUDIO_FALLBACK + PLAY_START_MARGIN   # 旧服务无首音字段：经验常数兜底
        if total_sec > 0:
            remain = t_post_wall + fa + total_sec + PLAY_TAIL_MARGIN - time.time()
            if remain > 0:
                time.sleep(remain)
        stall = max(0.0, t_done_wall - (t_post_wall + fa + total_sec))
        _t_turn_start = time.perf_counter()
        turn_note = _sound_page_turn(screen)
        _t_end = time.perf_counter()
        timing_str = (" | 耗时: 流请求%.1fs 起播%.1fs 首音%.1fs 合成%.1fs(卡顿%.1fs)"
                      " | 翻页%.1fs 总%.1fs(音频%.1fs/开销%.1fs)"
                      % (_t_post - _ta, _t_aa_end - _t_aa, fa, _t_done - T0, stall,
                         _t_end - _t_turn_start, _t_end - T0, total_sec,
                         _t_end - T0 - total_sec))
        preview = t if len(t) <= 40 else t[:37] + "..."
        return True, ("已播放 %.1fs 音频 %r（流式）%s%s"
                      % (total_sec, preview, turn_note, timing_str))


def _exec_sound_segments(t, screen=None):
    """Fallback 路径：本地拆句（100 字窗）+ 逐段 aa start（单 url）。
    seg1 主线程合成先播；播 seg1 期间后台线程预取合成 seg2；之后每段播完 aa start 下一段
    （新 HAP onNewWant 切换 ~0.1s，段间 ~0.3s）。
    urls 播放列表已废（09-11 实测：后台 'completed' 事件不触发，app 内链播不可靠）。"""
    segs = _split_sound_text(t)
    n = len(segs)
    T0 = time.perf_counter()
    # ① 第一段：主线程合成 → 起播
    ok, data0 = _tts_synth(segs[0])
    _t1 = time.perf_counter()
    if not ok:
        return False, ("第1/%d段: %s%s" % (n, data0, _seg_tail(1, n))) if n > 1 else \
                      ("第1段: " + data0)
    url0 = (data0.get("url") or "").strip()
    if not url0:
        return False, "第1段: TTS 服务未返回音频 url: %s" % str(data0)[:160]
    try:
        sec0 = float(data0.get("seconds") or 0)
    except (TypeError, ValueError):
        sec0 = 0.0
    rc, out = hdc("shell", "aa", "start", "-b", TTS_BUNDLE, "-a", TTS_ABILITY,
                  "--ps", "url", url0, timeout=30)
    if rc != 0 or "successfully" not in out.lower():
        return False, ("拉起 TtsAbility 失败: rc=%s %s（确认 HAP 已安装: "
                       "hdc install -r harmony/entry/build/default/outputs/default/entry-default-signed.hap）"
                       % (rc, out.strip()[:160]))
    _t2 = time.perf_counter()
    total_seconds = sec0
    seg_reports = [(segs[0], sec0, data0.get("cached"))]
    # 播 seg1 期间后台预取 seg2（合成等待藏进播放窗口）
    prefetch = _Prefetch(segs[1]) if n > 1 else None
    if sec0 > 0:
        time.sleep(sec0 + 0.5)
    _t3 = time.perf_counter()
    # ② seg2~segN：取预取结果（预取失败/超时降级主线程当场合成）→ aa start → 预取下一段
    for i in range(1, n):
        if prefetch is not None and prefetch.wait():
            ok, data = prefetch.result
        else:
            ok, data = _tts_synth(segs[i])
        if not ok:
            return False, "第%d/%d段: %s%s" % (i + 1, n, data, _seg_tail(i + 1, n))
        url = (data.get("url") or "").strip()
        if not url:
            return False, "第%d/%d段: TTS 服务未返回音频 url: %s%s" % (
                i + 1, n, str(data)[:160], _seg_tail(i + 1, n))
        try:
            sec = float(data.get("seconds") or 0)
        except (TypeError, ValueError):
            sec = 0.0
        rc, out = hdc("shell", "aa", "start", "-b", TTS_BUNDLE, "-a", TTS_ABILITY,
                      "--ps", "url", url, timeout=30)
        if rc != 0 or "successfully" not in out.lower():
            return False, ("第%d/%d段拉起 TtsAbility 失败: rc=%s %s%s"
                           % (i + 1, n, rc, out.strip()[:120], _seg_tail(i + 1, n)))
        total_seconds += sec
        seg_reports.append((segs[i], sec, data.get("cached")))
        prefetch = _Prefetch(segs[i + 1]) if i + 1 < n else None
        if sec > 0:
            time.sleep(sec + 0.5)
    # ③ 全部播完自动翻一页
    _t_turn_start = time.perf_counter()
    turn_note = _sound_page_turn(screen)
    _t_end = time.perf_counter()
    # ④ 耗时统计行（随"已执行:"一起打印）
    audio_rest = sum(sec for _, sec, _ in seg_reports[1:])
    if n == 1:
        timing_str = (" | 耗时: 合成%.1fs 起播%.1fs 翻页%.1fs 总%.1fs(音频%.1fs)"
                      % (_t1 - T0, _t2 - _t1, _t_end - _t_turn_start,
                         _t_end - T0, total_seconds))
    else:
        timing_str = (" | 耗时: seg1合成%.1fs 起播%.1fs | 逐段播放%.1fs(%d段,预取)"
                      " | 翻页%.1fs 总%.1fs(音频%.1fs/开销%.1fs)"
                      % (_t1 - T0, _t2 - _t1, max(_t_end - _t3 - audio_rest, 0.0), n - 1,
                         _t_end - _t_turn_start, _t_end - T0, total_seconds,
                         _t_end - T0 - total_seconds))
    # ⑤ 返回
    if n == 1:
        preview = segs[0] if len(segs[0]) <= 40 else segs[0][:37] + "..."
        return True, "已播放 %.1fs 音频 %r（%s）%s%s" % (
            sec0, preview, "缓存" if data0.get("cached") else "本次合成", turn_note, timing_str)
    descs = " + ".join("'%s'" % (s[:15] + "…" if len(s) > 15 else s) for s, _, _ in seg_reports)
    cached_all = data0.get("cached") and all(c for _, _, c in seg_reports[1:])
    return True, ("本地拆 %d 句连播（共 %.1fs）%s（%s）%s%s" % (
        n, total_seconds, descs,
        "全缓存" if cached_all else "部分本次合成", turn_note, timing_str))


def exec_sound(a, args, screen=None):
    """执行 sound 动作：把 text 字段读出来（屏幕完全不变，下一轮截图与本轮相同）。
    流程（09-11 23:5x，流式为主）：
    主路径（len(text)≤STREAM_MAX_CHARS 且 tts_server 有流式端点）：
      POST /tts_stream（服务器立即起 edge-tts 流式合成，或缓存命中直接回缓存 url）
      → aa start（AVPlayer 渐进播放，~2-3s 出声；同一页一条连续流，零拆段零段间沉默）
      → 轮询 /tts_status 拿实测总时长 → sleep 到音频结束 → 自动左滑翻一页
    Fallback（tts_server 旧版/不可达，或超长）：本地拆句（100 字窗）+ 逐段 aa start（单 url），
      预取把合成等待藏进播放窗口；每段靠 aa start 显式触发（不依赖后台 'completed' 事件）。
    返回 (ok, info)：ok=False = 这句没读完（缺文本/合成失败/服务不可达/aa 失败）——
    是"这句失败"不是任务失败，失败原因回喂（含已读进度），模型可补讲或先做别的，脚本不停止。"""
    t = (a.get("text") or "").strip()
    if not t:
        return False, "sound 缺 text（要朗读的句子；文本放 text 字段）"
    res = _exec_sound_stream(t, screen)
    if res is not None:
        return res
    return _exec_sound_segments(t, screen)


# ----------------------------- 主循环 -----------------------------

def describe(a, screen):
    if a["action"] == "click" and a.get("x") is not None and a.get("y") is not None:
        return "click(%d,%d)" % (px(a["x"], screen.w), px(a["y"], screen.h))
    if a["action"] in ("double_click", "long_press") and a.get("x") is not None and a.get("y") is not None:
        return "%s(%d,%d)" % (a["action"], px(a["x"], screen.w), px(a["y"], screen.h))
    if a["action"] == "key":
        return "key %s" % (a.get("key") or "?")
    if a["action"] == "type":
        return "type %r" % (a.get("text") or "")
    if a["action"] == "scroll":
        return "scroll dy=%s" % (a.get("dy") if a.get("dy") is not None else 1)
    if a["action"] == "drag" and a.get("x") is not None and a.get("x2") is not None:
        return "drag(%d,%d)->(%d,%d) v=%s" % (px(a["x"], screen.w), px(a["y"], screen.h),
                                              px(a["x2"], screen.w), px(a["y2"], screen.h),
                                              a.get("velocity") or 300)
    if a["action"] == "wait":
        return "wait %ss" % (a.get("seconds") or "?")
    if a["action"] == "sound":
        t = (a.get("text") or "")
        return "sound '%s'" % (t if len(t) <= 40 else t[:37] + "...")
    if a["action"] == "request":
        m = (a.get("method") or "GET").upper()
        u = (a.get("url") or "")
        if len(u) > 80:
            u = u[:77] + "..."
        return "request %s %s%s" % (m, u, " [save=%s]" % a.get("save") if a.get("save") else "")
    return a["action"]


def is_same_action(prev, cur):
    """对 click/type/key 做重复判定：
    - key: 同一键名即同一动作
    - type: 文本相同（空白文本统一视为"空"）且坐标 ±2% 屏（±20）内
    - click: 坐标 ±20 内视为同一位置
    - wait: 连续等待视为同一动作（等了屏幕还是没推进 = 卡住，别再光等）
    - request: 同一 URL（忽略大小写/首尾空格）视为同一动作（同一条死链重复请求 = 卡住）
    - drag: 起点和终点都在 ±20 内视为同一段（同一条线反复拖 = 卡住，尤其画笔画不上时）
    - double_click / long_press: 同 click，坐标 ±20 内视为同一动作
    - sound: 同一句话（text 相同）视为同一动作（同句复读 = 卡住；不同句是正常连续讲解，不算重复）
    其余动作不触发防护。"""
    if prev.get("action") != cur.get("action") or cur.get("action") not in ("click", "double_click", "long_press",
                                                                            "type", "key", "wait", "request", "drag",
                                                                            "sound"):
        return False
    if cur["action"] == "wait":
        return True
    if cur["action"] == "request":
        return (prev.get("url") or "").strip().lower() == (cur.get("url") or "").strip().lower()
    if cur["action"] == "sound":
        return (prev.get("text") or "").strip() == (cur.get("text") or "").strip()
    if cur["action"] == "drag":
        if any(prev.get(k) is None or cur.get(k) is None for k in ("x", "y", "x2", "y2")):
            return True
        return (abs(prev["x"] - cur["x"]) <= 20 and abs(prev["y"] - cur["y"]) <= 20 and
                abs(prev["x2"] - cur["x2"]) <= 20 and abs(prev["y2"] - cur["y2"]) <= 20)
    if cur["action"] == "key":
        return (prev.get("key") or "").strip().lower() == (cur.get("key") or "").strip().lower()
    if cur["action"] == "type" and (prev.get("text") or "").strip() != (cur.get("text") or "").strip():
        return False
    if prev.get("x") is None or cur.get("x") is None or prev.get("y") is None or cur.get("y") is None:
        return prev.get("x") == cur.get("x") and prev.get("y") == cur.get("y")
    return abs(prev["x"] - cur["x"]) <= 20 and abs(prev["y"] - cur["y"]) <= 20


def is_same_band(prev, cur):
    """同一带反复点击：同一水平条带（x±300 且 y±30，约 30%x3% 屏，覆盖任务栏/被遮挡窗口
    标签栏这类一行多按钮区域）内的连续点击——识别"换着位置戳同一区域"的死循环。仅提醒不强停。
    覆盖 click / double_click / long_press 三类点击式动作（任务栏一行多图标，Phase 1 加入后同步扩展）。"""
    if prev["action"] not in ("click", "double_click", "long_press") or \
            cur["action"] not in ("click", "double_click", "long_press"):
        return False
    if prev.get("x") is None or cur.get("x") is None or prev.get("y") is None or cur.get("y") is None:
        return False
    return abs(prev["x"] - cur["x"]) <= 300 and abs(prev["y"] - cur["y"]) <= 30


def _is_edge_click(a):
    """是否点在屏幕最外圈 ~2% 的边缘（归一化坐标）。
    右下角最外像素是系统"显示桌面"热区：一点=所有窗口最小化（微信消息任务实测：模型点
    (1000,1000) 后微信消失→重开→再点角落→无限循环）。这类点击模型自己认不出后果，脚本必须主动喂回。"""
    x, y = a.get("x"), a.get("y")
    if x is None or y is None:
        return False
    return x <= 25 or x >= 975 or y <= 10 or y >= 990


def format_history(history, max_thought=100):
    """把历史动作压成紧凑文本发给大模型，让它自己复盘、判断是否该换新动作（不做图像比对）。"""
    lines = []
    for (step, desc, thought, ok) in history:
        t = (thought or "").strip().replace("\n", " ").replace("\r", " ")
        if len(t) > max_thought:
            t = t[:max_thought] + "…"
        lines.append("  step %d: %s | 思路: %s | 结果: %s" % (step, desc, t or "-", "成功" if ok else "失败"))
    return "\n".join(lines)


# JSON 解析失败重试时附加的纠正式提示：temperature=0 + 同输入 → 盲重发必同型失败（2026-09-08
# step14 实测：同输入 3 次连续产出同型坏 JSON），必须改输入。实测失败形态=thought 里出现
# 未转义双引号（模型引用界面文字时），故提示同时点出这一点。
JSON_FIX_NOTE = ("[CORRECTION: Your previous response was NOT valid JSON and could not be parsed. "
                 "Output ONLY a single valid JSON object, no prose. If the 'thought' field contains "
                 "double quotes, escape them as \\\" or use single quotes for quoted UI text instead.]")


def _vlm_call(vlm, shot, task, feedback, history_text, args, extra_images=None, view_note=""):
    """VLM 调用 + 自动重试（--vlm-retries）。全部失败返回 None（由调用方退出码 1）。
    两类失败分开处理：
    - 网络/服务端错误（超时/RST/5xx/空 content）→ 同输入原样重发；
    - JSON 解析失败（ActionParseError）→ 纠正式重试：附加 JSON_FIX_NOTE 改变输入再发
      （fix_note 一旦置上保持 sticky——"上一条输出是坏 JSON"这一事实不随中间的网络错误消失）。"""
    fix_note = ""
    for attempt in range(1, args.vlm_retries + 2):   # 首次 + 重试
        try:
            return vlm.next_action(shot, task, feedback, history_text,
                                   extra_images=extra_images, view_note=view_note, fix_note=fix_note)
        except ActionParseError as e:
            if attempt > args.vlm_retries:
                print("[错误] VLM 输出解析失败（共试 %d 次）: %s" % (attempt, e))
                return None
            print("[警告] VLM 输出解析失败（第 %d/%d 次）: %s —— 附加 JSON 纠正式提示，3s 后重试"
                  % (attempt, args.vlm_retries + 1, e))
            fix_note = JSON_FIX_NOTE
            time.sleep(3)
        except Exception as e:
            if attempt > args.vlm_retries:
                print("[错误] VLM 调用失败（共试 %d 次）: %s" % (attempt, e))
                return None
            print("[警告] VLM 调用失败（第 %d/%d 次）: %s —— 3s 后重试" % (attempt, args.vlm_retries + 1, e))
            time.sleep(3)


def _notfound_feedback(n):
    """模型试图输出 not_found（认输）时的拦截反馈：not_found 已禁用，任务必须完成，强制重新规划。
    连续拦截次数越多，措辞越硬。"""
    if n == 1:
        return ("🚫 not_found 已被禁用，本任务没有'放弃'选项——禁止再输出 not_found。"
                "目标不在当前画面就换路径继续找：scroll 翻页找、key=back 回上一页、key=home 回桌面"
                "重新进入相关应用、换一个搜索词，或从另一个应用/入口进入。")
    return ("🚫 你已经连续 %d 次试图输出 not_found，全部无效——任务必须完成。现在必须输出一个具体动作："
            "先 key=home 回桌面，然后重新规划一条完全不同的路径（换应用、换入口、换搜索词、"
            "或把任务拆成更小的子目标逐个推进）。" % n)


def _make_feedback(sa, sdesc, exact, band):
    """按重复情况给下一轮模型的 feedback：精确重复=硬指令（分动作类型），同带重复=提醒。"""
    if exact >= 1:
        if sa["action"] == "type" and (sa.get("text") or "").strip() == "":
            return ("上一步 %s 已执行。⚠️ 你已连续 %d 次用空文本/换行表达'按回车'——空文本不是按键。"
                    "要确认地址栏/搜索框里已输入的内容（跳转/提交），请输出 action=key 且 key=enter。"
                    % (sdesc, exact + 1))
        if sa["action"] == "type":
            return ("上一步 %s 已执行。⚠️ 你已连续 %d 次输入同一文本，若输入框仍为空说明文本没进去。"
                    "请换一个输入框再试：浏览器改点顶部那条'搜索或输入网址'地址栏（在里面输入即触发搜索）；"
                    "其它应用换另一个搜索/输入框。最多再试一次；若仍为空，别再点这个输入框——scroll 翻出其它"
                    "输入框，或换一个应用搜同样的内容（not_found 已禁用，任务必须完成）。" % (sdesc, exact + 1))
        if sa["action"] == "key":
            return ("🛑 上一步 %s 已执行，但你已连续 %d 次按同一个键、屏幕没变化——这是【同一个失败动作】。"
                    "必须换一个【不同类别】的动作：点一个明显不同的元素、key=back/key=home、scroll；"
                    "若这条路径确实走不通，换一条【完全不同】的路径（key=home 回桌面重新进入、换应用、"
                    "换入口）——not_found 已禁用，任务必须完成。" % (sdesc, exact + 1))
        if sa["action"] == "wait":
            return ("🛑 上一步 %s 已执行，但你已连续 %d 次等待、屏幕仍未推进——继续空等没有意义，"
                    "这是【同一个失败动作】。必须换一个【不同类别】的动作：点一个明显不同的元素、"
                    "key=back/key=home、scroll、或 action=type 带坐标输入；若目标确实不可达，"
                    "换一条完全不同的路径继续寻找（not_found 已禁用，任务必须完成）。" % (sdesc, exact + 1))
        if sa["action"] == "request":
            return ("🛑 上一步 %s 已执行，但你已连续 %d 次请求同一个 URL、都没拿到结果——同一条死链不会"
                    "自己变活，这是【同一个失败动作】。必须换 URL：回页面找【另一条】下载链接（资源页通常"
                    "有多条候选）、换搜索词、或换站点/换入口；not_found 已禁用，任务必须完成。" % (sdesc, exact + 1))
        if sa["action"] == "sound":
            return ("🛑 上一步 %s 已执行，但你已连续 %d 次朗读同一句话——复读同一句不推进任务。"
                    "按顺序讲【下一句】（text 不同的新 sound）；若已是最后一句，继续后续的屏幕操作或 done，"
                    "不要再读这一句。" % (sdesc, exact + 1))
        if sa["action"] == "drag":
            return ("🛑 上一步 %s 已执行，但你已连续 %d 次拖同一段、画面/画布没变化——这是【同一个失败动作】。"
                    "若在作画：先核对截图里上一笔是否真留下了痕迹——没痕迹说明不在画布上/工具没选中（先 click "
                    "画布中央聚焦、或点开笔刷/画笔工具选一支笔），再拖；速度降到 velocity=200 笔画更实；"
                    "确认痕迹在变就继续画下一段，别重复同一段。若在拖物体：换物体上的另一个位置当起点"
                    "（点物体中心而不是边缘），或先 click 选中再拖。not_found 已禁用，任务必须完成。"
                    % (sdesc, exact + 1))
        nx, ny = sa.get("x"), sa.get("y")
        return ("🛑 上一步 %s 已执行。你在归一化坐标 (%s,%s) 已经点击 %d 次、屏幕都没变化——这是"
                "【同一个失败动作】；把坐标挪几像素（如 y 在 2013/2016/2020 之间）不算换动作，禁止再点这里"
                "或它附近。按情况三选一：(a) 你本意是点中输入框/地址栏准备输入——那多半已聚焦，下一步直接 "
                "action=type 并带该框 x,y 输入（会原子地再点一次并输入），别再光点不输；(b) 点击没生效"
                "（目标窗口被遮挡 / 目标打不开）——换一个【完全不同】的动作：key=back 关遮挡、key=home 回"
                "桌面、scroll 露出目标、或点另一个明显不同的元素；(c) 你已试过 2-3 种不同办法都没用——"
                "key=home 回桌面重新规划，换一个应用/入口/搜索词再来（not_found 已禁用，任务必须完成）。"
                % (sdesc, nx, ny, exact + 1))
    if band >= 2:
        return ("上一步 %s 已执行。⚠️ 你已在同一区域连续点击 %d 次，点击可能一直没生效。若目标"
                "窗口被遮挡，请改点底部 Dock/任务栏里该应用的图标把它调出来；若目标确实不可见，"
                "别再点这个区域：key=home 回桌面重新进入，或换搜索词/换应用去找（not_found 已禁用，"
                "任务必须完成）。" % (sdesc, band + 1))
    return "上一步 %s 已执行。" % sdesc


# ----------------------------- 讲解任务 done 硬拦截（代码层）-----------------------------
# prompt 层双保险（done 的 EXCEPTION + Rules 第 5 条）对 27B 不够（2026-09-11 三连实测：
# 模型都"在 thought 里读完内容"就 done，把"讲解"理解成"我识别了内容"），故加代码层确定性拦截：
# 任务属讲解类（含关键词）且动作历史里从未成功播过 sound → 拒绝 done，回喂"先讲出来"。
# 这是"讲 PPT/替我上课"场景的兜底，不依赖模型自觉；非讲解任务完全不受影响。
SOUND_TASK_KEYWORDS = ("讲解", "朗读", "讲一下", "讲给我", "介绍一下", "读一下", "读出来",
                       "念一下", "念出来", "播报", "explain", "narrate", "read aloud", "lecture")


def _task_needs_sound(task):
    """任务是否要求"出声"（讲解/朗读/播报类）。"""
    t = (task or "").lower()
    return any(k in t for k in SOUND_TASK_KEYWORDS)


def _has_spoken(history):
    """动作历史里是否已成功播过 sound（history 里 sound 的 desc 形如 "sound '...'"，且 ok=True）。"""
    return any(d.startswith("sound") and ok for (_s, d, _th, ok) in history)


def _sound_done_block_feedback():
    return ("[done 被拦截] 任务要求讲解/朗读/播报，但你还没有用 sound 动作把内容读出来——"
            "用户听不见你的 thought，只在 thought 里把内容读一遍不等于讲过。"
            "现在请先用 sound 讲解内容（每次 1~3 句），讲完再输出 done。")


def run(task, args):
    tts_ok, tts_msg = _ensure_tts_server()
    if tts_ok and "自动拉起" in tts_msg:
        print("[信息] %s" % tts_msg)
    elif not tts_ok:
        print("[警告] %s" % tts_msg)
    screen = Screen(args.out_dir)
    vlm = Vlm(args.base_url, args.model, args.api_key,
              enable_thinking=not getattr(args, "no_thinking", False),
              reasoning_effort=getattr(args, "reasoning_effort", DEFAULT_REASONING_EFFORT))
    feedback = ""
    prev = None      # 上一个已执行动作，用于重复提示
    exact = 0        # 连续精确重复（±20）计数
    band = 0         # 连续同带重复（±300x±30）计数
    history = []     # 历史动作记录（发给大模型让它自己复盘/决定是否换新动作）
    budget = args.max_steps   # 步数预算：每个执行的动作各扣 1（批量动作里每个都算）
    empty_rounds = 0          # 连续"没执行到任何动作"的轮数（防反复请求 view 空转）
    nf_count = 0              # 连续输出 not_found（被拦截）的次数；不导致停止，只升级 feedback
    edge_count = 0            # 点屏幕最外圈边缘的次数（右下角=显示桌面热区）；只升级警示 feedback
    sound_done_blocks = 0     # 讲解任务里"没出声就要 done"被拦截的次数；done 不扣预算，须封顶防死循环
    step = 0
    while budget > 0:
        step += 1
        t0 = time.time()
        try:
            shot = screen.shot(step)
        except Exception as e:
            print("[错误] 截图失败: %s" % e)
            return 1
        print("[step %d] 截图 %s (%dx%d)" % (step, os.path.basename(shot), screen.w, screen.h))
        history_text = format_history(history) if history else ""
        if getattr(args, "no_thinking", False):
            think_tag = "，thinking 关"
        else:
            think_tag = "，thinking %s" % getattr(args, "reasoning_effort", DEFAULT_REASONING_EFFORT)
        print("[step %d] 决策中（VLM 调用%s，读超时 %ds，失败自动重试 %d 次）..."
              % (step, think_tag, VLM_TIMEOUT, args.vlm_retries))
        res = _vlm_call(vlm, shot, task, feedback, history_text, args)
        if res is None:
            return 1
        a, ms = res[0], res[1]
        think_chars = res[2] if len(res) > 2 else 0
        # —— 回溯历史截图：模型请求看旧图 → 旧图+当前图一起附上再问一次 ——
        if a["action"] == "__view__":
            want = (a.get("view") or [])[:args.max_view]
            valid = []
            for n in want:
                p = os.path.join(args.out_dir, "shot_step%d.png" % n)
                if 1 <= n < step and os.path.exists(p):
                    valid.append((n, p))
            if not valid:
                print("[step %d] 模型请求查看历史截图 %s —— 不存在或超出范围（只能看 step 1..%d），忽略请求"
                      % (step, want, step - 1))
                feedback = ("你请求的历史截图 %s 不存在（只能请求 step 1..%d 且已截图的），"
                            "请直接基于当前截图输出动作。" % (want, step - 1))
                empty_rounds += 1
                if empty_rounds >= 3:
                    print("结束: 连续 %d 轮没有执行到任何动作（反复请求无效的历史截图），停止（退出码 1）" % empty_rounds)
                    return 1
                continue
            labels = ["图%d=历史 step %d 截图" % (i + 1, n) for i, (n, _p) in enumerate(valid)]
            print("[step %d] 模型请求查看历史截图 %s（%s）→ 附上当前截图再决策..."
                  % (step, [n for n, _ in valid], "；".join(labels)))
            res2 = _vlm_call(vlm, shot, task, feedback, history_text, args,
                             extra_images=[(lab, p) for lab, (_n, p) in zip(labels, valid)],
                             view_note="[你请求的历史截图已按标注附上，最后一张是当前截图(最新)。"
                                       "对比后请直接输出动作 JSON，不要再请求 view。]")
            if res2 is None:
                return 1
            b, ms2 = res2[0], res2[1]
            if len(res2) > 2:
                think_chars += res2[2]
            if b["action"] == "__view__":
                print("[step %d] 模型再次请求 view 而没有给动作 —— 跳过本轮" % step)
                feedback = "你已经看过这些历史截图了，请基于当前截图直接输出动作 JSON（不要再输出 view）。"
                empty_rounds += 1
                if empty_rounds >= 3:
                    print("结束: 连续 %d 轮没有执行到任何动作，停止（退出码 1）" % empty_rounds)
                    return 1
                continue
            a, ms = b, ms + ms2
            history.append((step, "查看历史截图 step %s" % [n for n, _ in valid], a["thought"], True))
        empty_rounds = 0
        desc = ("批量 %d 个动作: %s" % (len(a["actions"]),
                                        " → ".join(describe(s, screen) for s in a["actions"][:args.max_batch]))
                if a.get("actions") else describe(a, screen))
        think_note = (" | 思考 %d 字" % think_chars) if think_chars else ""
        print("[step %d] %ds | VLM %dms%s | %s | %s"
              % (step, int(time.time() - t0), ms, think_note, desc, a["thought"]))
        if a["action"] == "done":
            if _task_needs_sound(task) and not _has_spoken(history):
                sound_done_blocks += 1
                if sound_done_blocks >= 3:
                    print("结束: 模型连续 %d 次被拦下仍未出声讲解，判定其拒绝执行讲解，停止（退出码 1）"
                          % sound_done_blocks)
                    return 1
                print("[拦截] 模型要 done，但任务要求讲解且还没播过 sound（第 %d 次）——拒绝 done，强制先讲"
                      % sound_done_blocks)
                feedback = _sound_done_block_feedback()
                if args.dry_run:
                    print("(dry-run 不执行，只演示这一轮决策)")
                    return 0
                continue
            print("结束: done")
            return 0
        if a["action"] == "not_found":
            nf_count += 1
            print("[拦截] 模型试图输出 not_found（第 %d 次）——not_found 已禁用，强制它重新规划换路径"
                  % nf_count)
            feedback = _notfound_feedback(nf_count)
            if args.dry_run:
                print("(dry-run 不执行，只演示这一轮决策)")
                return 0
            continue   # 不停止：下一轮截图 + 带拦截 feedback 重新决策
        if args.dry_run:
            if a.get("actions"):
                for i, sa in enumerate(a["actions"][:args.max_batch], 1):
                    print("  (dry-run 批量计划 %d: %s)" % (i, describe(sa, screen)))
            print("(dry-run 不执行，只演示这一轮决策)")
            return 0
        # —— 展开成动作序列（单动作 = 长度为 1 的序列），逐个执行、共享重复防护 ——
        if a.get("actions"):
            actions = a["actions"]
            if len(actions) > args.max_batch:
                print("[提示] 批量 %d 个动作超过上限 --max-batch=%d，只执行前 %d 个"
                      % (len(actions), args.max_batch, args.max_batch))
                actions = actions[:args.max_batch]
            is_batch = True
        else:
            actions, is_batch = [a], False
        batch_aborted = False
        for idx, sa in enumerate(actions, 1):
            if sa["action"] == "done":
                if _task_needs_sound(task) and not _has_spoken(history):
                    sound_done_blocks += 1
                    if sound_done_blocks >= 3:
                        print("结束: 模型连续 %d 次被拦下仍未出声讲解，判定其拒绝执行讲解，停止（退出码 1）"
                              % sound_done_blocks)
                        return 1
                    print("[拦截] 批量里的 done，但任务要求讲解且还没播过 sound（第 %d 次）——中止批量，强制先讲"
                          % sound_done_blocks)
                    feedback = _sound_done_block_feedback()
                    batch_aborted = True
                    break
                print("结束: done")
                return 0
            if sa["action"] == "not_found":
                nf_count += 1
                print("[拦截] 批量第 %d/%d 个动作是 not_found —— 已禁用，中止剩余 %d 个动作，强制重新规划"
                      % (idx, len(actions), len(actions) - idx))
                feedback = _notfound_feedback(nf_count)
                batch_aborted = True
                break
            if budget <= 0:
                print("结束: 达到最大步数 %d" % args.max_steps)
                return 1
            budget -= 1
            sdesc = describe(sa, screen)
            rep_exact = (prev is not None) and is_same_action(prev, sa)
            rep_band = (prev is not None) and is_same_band(prev, sa)
            exact = exact + 1 if rep_exact else 0
            band = band + 1 if rep_band else 0
            if rep_exact and args.max_repeat > 0 and exact >= args.max_repeat:
                print("结束: 连续 %d 次重复同一动作 %s，模型未采纳自我纠错，判定死循环，停止（退出码 4）" % (exact + 1, sdesc))
                return 4
            ok, info = exec_action(sa, screen, args)
            if not ok:
                print("[错误] 执行失败: %s" % info)
                if is_batch:
                    print("      批量中止: 剩余 %d 个动作跳过（下一轮重新决策）" % (len(actions) - idx))
                    feedback = ("批量执行到第 %d/%d 个动作 %s 时失败: %s。剩余动作已跳过——请基于下一张截图"
                                "重新决策，别盲目重试同一动作。" % (idx, len(actions), sdesc, info))
                    prev = sa
                    batch_aborted = True
                    break
                # 单动作失败 ≠ 任务失败（死链/未知键/缺坐标/设备报错 = "这种做法不行"）：
                # 把失败原因喂回，模型下一轮换办法；同一动作重复仍有 --max-repeat rc4 死循环兜底。
                # 之前 click 等动作失败直接 return 1，一次模型输出瑕疵（如缺坐标）就杀掉整个任务
                print("      （单动作失败不判任务失败，下一轮带该原因继续）")
                if sa["action"] == "request":
                    feedback = "[request 结果] %s" % info
                elif sa["action"] == "sound":
                    feedback = ("[sound 结果] %s。发音失败不影响屏幕操作——先继续其它操作，稍后回来补讲；"
                                "重试时原句重发即可（超长由系统自动拆句，无需手动缩短）。")
                elif sa["action"] == "key":
                    feedback = ("[key 执行失败] %s。手机 App 通常不响应 PC 快捷键（Ctrl+X 式）——"
                                "排版/操作请直接点屏幕上的工具栏按钮（给坐标）。" % info)
                else:
                    feedback = ("[执行失败] %s。不要重复同样的做法：先核对坐标/元素位置，或改用其他动作。" % info)
                prev = sa
                continue
            if is_batch:
                print("      [批量 %d/%d] 已执行: %s" % (idx, len(actions), info))
            else:
                print("      已执行: %s" % info)
            history.append((step, sdesc, sa.get("thought") or a["thought"], ok))
            if sa["action"] == "request":
                # request 不改变屏幕：下一轮截图与本轮相同，必须把结果（预览/保存路径）喂回，
                # 模型才能读页面内容或确认下载完成
                feedback = "[request 结果] %s" % info
                if exact > 0:
                    feedback += " " + _make_feedback(sa, sdesc, exact, band)
            elif sa["action"] == "sound":
                # sound 不改变屏幕：下一轮截图与本轮相同，必须显式回喂"这句已播完"，
                # 模型才知道按顺序讲下一句（否则会以为"屏幕没变化=失败"而复读同一句）
                feedback = ("[sound 结果] %s。这句已播完——下一句请按顺序讲（text 不同的新 sound），"
                            "不要重读这句。" % info)
                if exact > 0:
                    feedback += " " + _make_feedback(sa, sdesc, exact, band)
            else:
                feedback = _make_feedback(sa, sdesc, exact, band)
            if sa["action"] in ("click", "double_click", "long_press", "drag") and _is_edge_click(sa):
                # 边缘点击/拖拽起点主动警示（模型认不出自己点了系统热区；微信消息任务实测死循环元凶）
                edge_count += 1
                hint = ("⚠️ 你点到了屏幕最外圈像素——屏幕右下角是系统'显示桌面'热区，点它会【最小化所有窗口】"
                        "（'窗口不在前台/画面变成桌面'的元凶）。输入框请点它的中左部（x=300~600），按钮点中心，"
                        "禁止再点屏幕最外圈。")
                if edge_count >= 2:
                    hint += " 这是你第 %d 次点屏幕边缘了——窗口反复消失就是因为这个点击，这次必须把坐标挪到屏幕内部。" % edge_count
                feedback = hint + " " + feedback
            if sa["action"] == "key" and "ctrl" in (sa.get("key") or "").lower():
                # PC 快捷键在手机上经常无响应（WPS 任务实测：模型想用 Ctrl+E 居中，手机 App 根本不吃）
                feedback = ("提醒：手机 App 常不响应 PC 快捷键（Ctrl+X 式）——若下张截图屏幕没变化，"
                            "直接点屏幕上对应的工具栏按钮，不要重试同一快捷键。 ") + feedback
            prev = sa
            if is_batch and idx < len(actions) and args.batch_gap > 0 and sa["action"] != "wait":
                time.sleep(args.batch_gap)
        if is_batch and not batch_aborted:
            feedback = ("批量 %d 个动作已全部执行完成（%s）。%s"
                        % (len(actions), " → ".join(describe(s, screen) for s in actions), feedback))
        time.sleep(1.0)   # 等界面稳定再下一轮
    print("结束: 达到最大步数 %d（动作预算耗尽）" % args.max_steps)
    return 1


def main():
    ap = argparse.ArgumentParser(description="UIAgent 本地直连模式（终端里直接跑）")
    ap.add_argument("task", nargs="?", help="指令，如: 点击任务栏的微信图标")
    ap.add_argument("--dry-run", action="store_true", help="只截屏+决策，不碰屏幕")
    ap.add_argument("--max-steps", type=int, default=10,
                    help="最多执行动作数（默认 10；批量动作 actions 数组里每个动作各占 1 步）")
    ap.add_argument("--max-batch", type=int, default=4,
                    help="一次批量（actions 数组）最多执行几个动作（默认 4；设 1 = 关闭批量，回到单动作）")
    ap.add_argument("--batch-gap", type=float, default=0.5,
                    help="批量动作之间的间隔秒数（默认 0.5s 等界面跟上；wait 动作后不加；设 0 关闭）")
    ap.add_argument("--max-view", type=int, default=2,
                    help="一次最多回溯几张历史截图（默认 2）")
    ap.add_argument("--max-repeat", type=int, default=5,
                    help="同一动作连续重复达到该次数、且模型仍未换动作时才判定死循环停止（退出码 4）；默认 5。"
                         "设为 0 = 不做重复硬停，完全交给大模型自我纠错（仅受 --max-steps 限制）")
    ap.add_argument("--vlm-retries", type=int, default=2,
                    help="VLM 调用失败（超时/断连/5xx）时的自动重试次数（默认 2，每次间隔 3s）。"
                         "100 步长任务防一次网络/服务端抖动就杀掉整个任务。设为 0 = 不重试")
    ap.add_argument("--no-thinking", action="store_true",
                    help="关闭模型 thinking 模式（默认开：先思考后输出 JSON，更稳但每步更慢）。"
                         "思考 token 计入 max_tokens=%d" % MAX_TOKENS)
    ap.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT,
                    choices=["xhigh", "high", "medium", "low"],
                    help="thinking 强度（默认 xhigh；仅 thinking 开时生效，随请求下发 reasoning_effort）")
    ap.add_argument("--req-timeout", type=int, default=60,
                    help="request 动作的 HTTP 读超时秒数（默认 60；慢站点/大文件按需调大）")
    ap.add_argument("--max-download-mb", type=int, default=2048,
                    help="单次 request 下载的大小上限 MB（默认 2048；超限中断并丢弃部分文件，防假链接/超大文件空烧时间）")
    ap.add_argument("--display-id", type=int, default=None,
                    help="多屏：给所有 uitest uiInput 命令追加尾参 displayId，把动作注入指定屏（默认不追加=主屏）")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--api-key", default=DEFAULT_API_KEY)
    ap.add_argument("--out-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "uiagent_shots"),
                    help="截图保存目录")
    ap.add_argument("--check", action="store_true", help="自检：截一张图打印分辨率后退出")
    ap.add_argument("--target", default=None,
                    help="hdc target 标识符（hdc list targets 的输出）；多设备时必填，否则自动锁定唯一设备")
    ap.add_argument("--reconnect-target", default=DEFAULT_RECONNECT_TARGET,
                    help="无设备时自动 hdc tconn 的目标（默认 127.0.0.1:33897，本机本地 hdc）")
    ap.add_argument("--no-auto-reconnect", action="store_true", help="禁用掉线自动重连")
    args = ap.parse_args()

    global DISPLAY_ID
    if args.display_id is not None:
        DISPLAY_ID = args.display_id

    resolve_target(args.target, args.reconnect_target, not args.no_auto_reconnect)   # 可能 SystemExit(1)

    if args.check:
        s = Screen(args.out_dir)
        p = s.shot(0)
        print("自检通过: %s  分辨率 %dx%d  截图方式: %s" % (p, s.w, s.h, s.last_method))
        return 0
    if not args.task:
        ap.error("缺少指令（或先用 --check 自检）")

    print("任务: %s   模式: %s   最多 %d 步   thinking: %s"
          % (args.task, "dry-run" if args.dry_run else "真实执行", args.max_steps,
             "off" if args.no_thinking else "on (%s)" % args.reasoning_effort))
    if DISPLAY_ID is not None:
        print("display-id: %d（所有 uiInput 命令注入 %d 号屏）" % (DISPLAY_ID, DISPLAY_ID))
    print("（记得先把终端和 UIAgent App 窗口最小化，它们会出现在截图里）")
    return run(args.task, args)


if __name__ == "__main__":
    sys.exit(main())
