# -*- coding: utf-8 -*-
"""视频转码工具：ffmpeg 预设、命令构建、进度解析与任务执行。"""
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from flask import request

from web_common import CREATE_NO_WINDOW, IS_WINDOWS, clean_path

log = logging.getLogger("media.video")

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts", ".m2ts", ".flv",
             ".wmv", ".m4v", ".mts", ".3gp", ".mpg", ".mpeg"}

PRESETS = {
    "amd_hevc": {
        "label": "AMD HEVC 硬件编码 (hevc_amf)",
        "codec": "hevc_amf",
        "note": "AMD 显卡硬件编码，速度快、功耗低",
        "params": {
            "mode": "crf", "crf": 24,
            "bitrate": "8000k", "maxrate": "10000k", "bufsize": "16000k",
            "amf_quality": "quality",
            "x265_preset": "medium", "nvenc_preset": "p5", "qsv_preset": "medium",
            "resolution": "original", "res_w": "", "res_h": "",
            "fps": "original", "audio": "copy", "keep_all_audio": "1",
            "custom_args": "",
        },
    },
    "x265": {
        "label": "x265 软件编码 (libx265)",
        "codec": "libx265",
        "note": "CPU 软件编码，同码率下画质最好，速度慢",
        "params": {
            "mode": "crf", "crf": 22,
            "bitrate": "8000k", "maxrate": "12000k", "bufsize": "24000k",
            "x265_preset": "medium", "nvenc_preset": "p5", "qsv_preset": "medium",
            "amf_quality": "quality",
            "resolution": "original", "res_w": "", "res_h": "",
            "fps": "original", "audio": "copy", "keep_all_audio": "1",
            "custom_args": "",
        },
    },
    "amd_hevc_2k": {
        "label": "AMD HEVC 2K 压缩 (hevc_amf)",
        "codec": "hevc_amf",
        "note": "2K + 25fps + 128k 音频，小体积",
        "params": {
            "mode": "crf", "crf": 26,
            "bitrate": "8000k", "maxrate": "10000k", "bufsize": "16000k",
            "amf_quality": "quality",
            "x265_preset": "medium", "nvenc_preset": "p5", "qsv_preset": "medium",
            "resolution": "2560:1440", "res_w": "", "res_h": "",
            "fps": "25", "audio": "aac-128k", "keep_all_audio": "1",
            "custom_args": "",
        },
    },
    "x265_2k": {
        "label": "x265 2K 压缩 (libx265)",
        "codec": "libx265",
        "note": "2K + 25fps + 128k 音频，小体积",
        "params": {
            "mode": "crf", "crf": 27,
            "bitrate": "8000k", "maxrate": "12000k", "bufsize": "24000k",
            "x265_preset": "medium", "nvenc_preset": "p5", "qsv_preset": "medium",
            "amf_quality": "quality",
            "resolution": "2560:1440", "res_w": "", "res_h": "",
            "fps": "25", "audio": "aac-128k", "keep_all_audio": "1",
            "custom_args": "",
        },
    },
}

AMF_QUALITY_OPTIONS = ["speed", "balanced", "quality"]
X265_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                "medium", "slow", "slower", "veryslow"]
NVENC_PRESETS = ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]

# 探测到对应硬件编码器后动态注册的预设（进程内一次性，供 N 卡/Intel 机器使用）
HW_PRESET_LABELS = {
    "hevc_nvenc": "NVIDIA HEVC 硬件编码 (hevc_nvenc)",
    "hevc_qsv": "Intel HEVC 硬件编码 (hevc_qsv)",
    "av1_nvenc": "NVIDIA AV1 硬件编码 (av1_nvenc)",
    "av1_qsv": "Intel AV1 硬件编码 (av1_qsv)",
}
DYNAMIC_PRESETS = {}


def make_hw_preset(codec, label):
    vendor = "NVIDIA" if codec.endswith("_nvenc") else "Intel"
    medium = codec.split("_")[0].upper()
    return {
        "label": label,
        "codec": codec,
        "note": f"{vendor} {medium} 硬件编码，速度快、功耗低",
        "params": {
            "mode": "crf", "crf": 24,
            "bitrate": "8000k", "maxrate": "10000k", "bufsize": "16000k",
            "amf_quality": "quality",
            "x265_preset": "medium", "nvenc_preset": "p5", "qsv_preset": "medium",
            "resolution": "original", "res_w": "", "res_h": "",
            "fps": "original", "audio": "copy", "keep_all_audio": "1",
            "custom_args": "",
        },
    }


def get_preset(preset_id):
    if preset_id in PRESETS:
        return PRESETS[preset_id]
    return DYNAMIC_PRESETS.get(preset_id)


# ------------------------------------------------------------- ffmpeg builder
def build_video_args(preset_id, params):
    codec = get_preset(preset_id)["codec"]
    mode = params.get("mode", "crf")
    crf = int(params.get("crf") or 23)
    args = ["-c:v", codec]

    def bitrate_args():
        b = ["-b:v", params.get("bitrate", "8000k")]
        if params.get("maxrate"):
            b += ["-maxrate", params["maxrate"]]
        if params.get("bufsize"):
            b += ["-bufsize", params["bufsize"]]
        return b

    if codec == "libx265":
        args += ["-preset", params.get("x265_preset", "medium")]
        if mode == "crf":
            args += ["-crf", str(crf)]
        else:
            args += bitrate_args()
        args += ["-x265-params", "log-level=error"]
    elif codec == "hevc_amf":
        args += ["-quality", params.get("amf_quality", "balanced")]
        if mode == "crf":
            # AMF 的 CRF 模式实际是 CQP
            args += ["-rc", "1", "-qp_i", str(crf), "-qp_p", str(crf)]
        else:
            args += bitrate_args()
    elif codec.endswith("_nvenc"):
        args += ["-preset", params.get("nvenc_preset", "p5")]
        if mode == "crf":
            args += ["-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
        else:
            args += bitrate_args()
    elif codec.endswith("_qsv"):
        args += ["-preset", params.get("qsv_preset", "medium")]
        if mode == "crf":
            args += ["-global_quality", str(crf)]
        else:
            args += bitrate_args()
    return args


def build_vf(params):
    filters = []
    res = params.get("resolution", "original")
    if res == "custom":
        if params.get("res_w") and params.get("res_h"):
            filters.append(f"scale={params['res_w']}:{params['res_h']}:flags=lanczos")
    elif res and res != "original":
        filters.append(f"scale={res}:flags=lanczos")
    fps = params.get("fps", "original")
    if fps and fps != "original":
        filters.append(f"fps={fps}")
    return filters


def build_audio_args(params):
    a = params.get("audio", "copy")
    if a == "none":
        return ["-an"]
    if a == "copy":
        return ["-c:a", "copy"]
    m = re.match(r"aac-(\d+)k", a)
    if m:
        return ["-c:a", "aac", "-b:a", f"{m.group(1)}k"]
    return ["-c:a", "copy"]


def build_command(src, out, preset_id, params):
    cmd = [FFMPEG, "-y", "-hide_banner", "-i", str(src)]
    audio = params.get("audio", "copy")
    keep_all = str(params.get("keep_all_audio", "1")).lower() not in ("0", "false", "no", "off")
    if keep_all and audio != "none":
        # 显式映射第一条视频流和全部音轨：ffmpeg 默认每类只挑一条"最佳"流，
        # 多音轨视频会被静默丢轨；显式映射同时排除数据流和封面图
        cmd += ["-map", "0:v:0", "-map", "0:a?"]
    cmd += build_video_args(preset_id, params)
    vf = build_vf(params)
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += build_audio_args(params)
    custom = (params.get("custom_args") or "").strip()
    if custom:
        cmd += re.split(r"\s+", custom)
    cmd += ["-progress", "pipe:1", "-nostats", str(out)]
    return cmd


# --------------------------------------------------------------- param 校验
PARAM_KEYS = ["mode", "crf", "bitrate", "maxrate", "bufsize", "resolution",
              "res_w", "res_h", "fps", "audio", "keep_all_audio",
              "x265_preset", "nvenc_preset", "qsv_preset", "amf_quality",
              "custom_args"]

INT_RE = re.compile(r"^\d+$")
RATE_RE = re.compile(r"^\d+(\.\d+)?[kKmM]?$")
FPS_RE = re.compile(r"^\d+(\.\d+)?$")
RES_RE = re.compile(r"^\d+:\d+$")


def validate_params(params):
    """校验会拼进 ffmpeg 命令的参数，返回错误文案或 None。"""
    if params.get("mode") not in ("crf", "bitrate"):
        return f"无效的码控模式: {params.get('mode')}"
    if params["mode"] == "crf":
        crf = str(params.get("crf") or "")
        if not INT_RE.match(crf) or not 0 <= int(crf) <= 51:
            return "CRF/QP 必须是 0-51 的整数"
    for key, label in (("bitrate", "码率"), ("maxrate", "峰值码率"),
                       ("bufsize", "缓冲大小")):
        v = params.get(key)
        if v and not RATE_RE.match(v):
            return f"{label}格式无效（示例 8000k）: {v}"
    res = params.get("resolution", "original")
    if res == "custom":
        for key, label in (("res_w", "宽度"), ("res_h", "高度")):
            v = params.get(key, "")
            if not INT_RE.match(v or "") or not 16 <= int(v) <= 8192:
                return f"自定义分辨率{label}必须是 16-8192 的整数"
    elif res != "original" and not RES_RE.match(res):
        return f"无效的分辨率: {res}"
    fps = params.get("fps", "original")
    if fps != "original" and (not FPS_RE.match(fps) or not 1 <= float(fps) <= 1000):
        return f"无效的帧率: {fps}"
    if params.get("x265_preset") and params["x265_preset"] not in X265_PRESETS:
        return f"无效的 x265 速度档: {params['x265_preset']}"
    if params.get("nvenc_preset") and params["nvenc_preset"] not in NVENC_PRESETS:
        return f"无效的 NVENC 速度档: {params['nvenc_preset']}"
    if params.get("qsv_preset") and params["qsv_preset"] not in X265_PRESETS:
        return f"无效的 QSV 速度档: {params['qsv_preset']}"
    if params.get("amf_quality") and params["amf_quality"] not in AMF_QUALITY_OPTIONS:
        return f"无效的 AMD 质量档: {params['amf_quality']}"
    audio = params.get("audio", "copy")
    if audio not in ("copy", "none"):
        m = re.match(r"aac-(\d+)k$", audio)
        if not m or not 32 <= int(m.group(1)) <= 320:
            return f"无效的音频设置: {audio}"
    if str(params.get("keep_all_audio", "1")) not in ("0", "1"):
        return "无效的多音轨选项"
    return None


def params_from_request(req):
    """从表单/查询串取参数；返回 (params, 错误文案)。"""

    def g(key):
        v = req.form.get(key)
        if v is None:
            v = req.args.get(key)
        return v

    preset = g("preset") or "amd_hevc"
    if preset not in PRESETS and preset not in DYNAMIC_PRESETS:
        return None, "未知预设"
    defaults = get_preset(preset)["params"]
    vals = {k: g(k) for k in PARAM_KEYS}
    params = {k: (vals[k] if vals[k] is not None else defaults.get(k, ""))
              for k in PARAM_KEYS}
    params["preset"] = preset
    err = validate_params(params)
    if err:
        return None, err
    return params, None


def output_candidate(src, name, params):
    """输出到源文件所在目录，命名 {原名}_{预设}.mp4。"""
    return Path(src).parent / f"{Path(name).stem}_{params.get('preset', 'amd_hevc')}.mp4"


# ------------------------------------------------------------------ 探测/执行
def probe_duration(path):
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=120,
            creationflags=CREATE_NO_WINDOW)
        val = out.stdout.strip()
        return float(val) if val else None
    except Exception:
        return None


def probe_payload(path):
    """ffprobe 读取媒体信息；出错时抛异常，由路由统一兜底。"""
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_format", "-show_streams",
         "-of", "json", str(path)],
        capture_output=True, timeout=120,
        creationflags=CREATE_NO_WINDOW)
    stdout = out.stdout.decode("utf-8", errors="replace") if out.stdout else ""
    data = json.loads(stdout)

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    def fps_of(s):
        r = s.get("avg_frame_rate") or s.get("r_frame_rate")
        if r and "/" in r:
            try:
                num, den = r.split("/")
                num, den = float(num), float(den)
                if num and den:
                    return round(num / den, 3)
            except Exception:
                pass
        return None

    info = {
        "path": str(path),
        "name": Path(path).name,
        "container": fmt.get("format_name", ""),
        "size_bytes": int(fmt.get("size", 0) or 0),
        "duration": float(fmt.get("duration", 0) or 0) or None,
        "video": None,
        "audio": None,
    }
    if v:
        info["video"] = {
            "codec": v.get("codec_name", ""),
            "width": v.get("width"),
            "height": v.get("height"),
            "pix_fmt": v.get("pix_fmt", ""),
            "fps": fps_of(v),
            "bitrate": v.get("bit_rate"),
        }
    if a:
        info["audio"] = {
            "codec": a.get("codec_name", ""),
            "channels": a.get("channels"),
            "sample_rate": a.get("sample_rate"),
            "bitrate": a.get("bit_rate"),
        }
    return info


def cleanup_partial(out):
    try:
        if out.exists():
            out.unlink()
    except Exception:
        pass


def graceful_cancel(proc):
    """请求 ffmpeg 干净退出（stdin 发 q，可正确收尾封装），超时后强杀。"""
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.write("q")
            proc.stdin.flush()
            proc.stdin.close()
    except Exception:
        pass

    def _kill():
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    threading.Timer(2.0, _kill).start()


def cancel_running(t):
    proc = t.get("proc")
    if proc:
        graceful_cancel(proc)


# 该服务启动的 ffmpeg 子进程，重启服务时兜底清理
child_pids = set()
child_lock = threading.Lock()


def run_task(mgr, t):
    src = Path(t["source"])
    if not src.exists():
        mgr.fail_task(t, "源文件不存在或已被删除")
        return

    try:
        t["src_size"] = src.stat().st_size
    except OSError:
        t["src_size"] = 0

    # 磁盘空间预检：以源文件大小 1.1 倍为粗略上界（下限 512MB）
    try:
        out_path = Path(t["output"])
        free = shutil.disk_usage(out_path.anchor or ".").free
        need = max(int(t["src_size"] * 1.1), 512 * 1024 * 1024)
        if free < need:
            mgr.fail_task(t, f"磁盘空间不足：输出盘剩余 {free / 2**30:.1f}GB，"
                             f"预估至少需要 {need / 2**30:.1f}GB")
            return
    except Exception:
        pass

    t["duration"] = probe_duration(src)
    preset_id = t["params"].get("preset", "amd_hevc")
    out = Path(t["output"])

    cmd = build_command(src, out, preset_id, t["params"])
    t["cmd"] = " ".join(cmd)

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        mgr.fail_task(t, f"启动 ffmpeg 失败: {e}")
        return
    with child_lock:
        child_pids.add(proc.pid)
    t["proc"] = proc

    last_err = ""
    cancel_sent = False
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        m = re.search(r"out_time_us=(\d+)", line)
        if m and t["duration"]:
            us = int(m.group(1))
            t["progress"] = round(min(100.0, us / 1e6 / t["duration"] * 100), 2)
        ms = re.search(r"\bspeed=\s*([0-9.]+)x", line)
        if ms:
            try:
                t["speed"] = float(ms.group(1))
            except ValueError:
                pass
        if re.search(r"error|failed", line, re.I):
            last_err = line
        if t.get("status") == "cancelling" and not cancel_sent:
            cancel_sent = True
            graceful_cancel(proc)
    proc.wait()
    with child_lock:
        child_pids.discard(proc.pid)
    code = proc.returncode

    with mgr.lock:
        if t["status"] == "cancelling":
            t["status"] = "cancelled"
            t["error"] = "用户取消"
            cleanup_partial(out)
        elif code == 0 and out.exists():
            t["status"] = "completed"
            t["progress"] = 100.0
            try:
                t["out_size"] = out.stat().st_size
            except OSError:
                pass
        else:
            t["status"] = "failed"
            t["error"] = last_err or f"ffmpeg 退出码 {code}"
            cleanup_partial(out)
        t["finished"] = time.time()
        # 任务已结束，释放子进程句柄引用
        t.pop("proc", None)


# ------------------------------------------------------------- 环境信息接口
_ffi_cache = None


def info_payload():
    """ffmpeg 环境与预设表；探测结果进程内只算一次。"""
    global _ffi_cache
    if _ffi_cache is not None:
        return _ffi_cache
    version = "未找到 ffmpeg"
    try:
        out = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True,
                             timeout=15, creationflags=CREATE_NO_WINDOW)
        if out.stdout:
            version = out.stdout.splitlines()[0]
    except Exception as e:
        version = f"error: {e}"
    encoders = {}
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True,
                             text=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        for enc in ["hevc_amf", "libx265", "hevc_nvenc", "hevc_qsv", "h264_amf",
                    "h264_nvenc", "h264_qsv", "libx264", "av1_amf", "av1_nvenc",
                    "av1_qsv", "aac"]:
            encoders[enc] = bool(re.search(rf"\b{re.escape(enc)}\b", out.stdout))
    except Exception:
        pass
    for codec, label in HW_PRESET_LABELS.items():
        if encoders.get(codec) and codec not in DYNAMIC_PRESETS:
            DYNAMIC_PRESETS[codec] = make_hw_preset(codec, label)
    merged = dict(PRESETS)
    merged.update(DYNAMIC_PRESETS)
    _ffi_cache = {
        "ffmpeg": FFMPEG,
        "version": version,
        "encoders": encoders,
        "presets": {pid: {k: p[k] for k in ("label", "codec", "note", "params")}
                    for pid, p in merged.items()},
    }
    return _ffi_cache


def preview_payload(mgr):
    """构建命令预览；mgr 用于避开已预留的输出名。"""
    params, err = params_from_request(request)
    if err:
        return {"error": err}, 400
    preset = params["preset"]
    name = (request.args.get("name") or "输入视频").strip() or "输入视频"
    stem = Path(name).stem
    path = clean_path(request.args.get("path"))
    p = Path(path) if path else None
    if p and p.is_file():
        out_display = str(mgr.preview_output(output_candidate(p, name, params)))
    elif p and p.is_dir():
        out_display = str(p / f"{stem}_{preset}.mp4")
    elif p:
        out_display = str(p.parent / f"{stem}_{preset}.mp4")
    else:
        sep = "\\" if IS_WINDOWS else "/"
        out_display = f"「输入视频所在目录」{sep}{stem}_{preset}.mp4"
    cmd = build_command(path or "输入视频", out_display, preset, params)
    return {"cmd": " ".join(cmd), "output": out_display}, 200


def collect_sources(form_paths):
    """从路径列表收集待转码文件；文件夹只扫描一层（不递归）。"""
    sources = []
    for p in form_paths:
        p = clean_path(p)
        if not p:
            continue
        pp = Path(p)
        if pp.is_dir():
            for v in sorted(pp.iterdir()):
                if v.is_file() and v.suffix.lower() in VIDEO_EXT:
                    sources.append((v, v.stem))
        elif pp.is_file():
            sources.append((pp, pp.stem))
        else:
            return None, f"路径不存在: {p}"
    return sources, None
