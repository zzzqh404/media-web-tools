# -*- coding: utf-8 -*-
"""本地媒体工具箱：视频转码（ffmpeg）+ 照片压缩（Pillow）+ 重复清理 的本机网页工具。

- `/`       视频转码页（原 ffmpeg-web-encoder）
- `/photo`  照片压缩页（原 photo-web-compressor）
- `/dedupe` 重复文件/相似照片清理页
- 各工具的 API 挂在 /api/video、/api/photo、/api/dedupe 命名空间下，
  任务队列/取消/重试等通用逻辑由 task_manager.TaskManager 提供。
- 仅处理本机文件，仅允许本机访问。
"""
import logging
import os
import subprocess
import sys
import threading
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import dedupe
import photo
import video
from task_manager import TaskManager, register_task_routes
from web_common import (BASE_DIR, CREATE_NO_WINDOW, IS_WINDOWS, clean_path,
                        local_guard, pick_dialog)

HOST = os.environ.get("MEDIA_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEDIA_PORT", "5000"))

VIDEO_WORKERS = int(os.environ.get("FFMPEG_WORKERS", "2"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(),
              RotatingFileHandler(BASE_DIR / "media_tools.log",
                                  maxBytes=5 * 1024 * 1024, backupCount=2,
                                  encoding="utf-8")])
log = logging.getLogger("media")
# 轮询请求的访问日志噪音太大，只记录 WARNING 以上
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)

# ------------------------------------------------------------------ 双任务管理器
video_mgr = TaskManager(
    name="video", workers=VIDEO_WORKERS, run_task=video.run_task,
    candidate=video.output_candidate,
    serialize_keys=("speed", "cmd", "duration"),
    on_cancel_running=video.cancel_running)

photo_mgr = TaskManager(
    name="photo", workers=photo.default_workers(), run_task=photo.run_task,
    candidate=photo.output_candidate)

# 扫描类任务没有输出文件；单 worker，同一时间只跑一次扫描
dedupe_mgr = TaskManager(
    name="dedupe", workers=1, run_task=dedupe.run_task,
    candidate=None,
    serialize_keys=("phase", "scanned", "total"))

register_task_routes(app, video_mgr, "/api/video")
register_task_routes(app, photo_mgr, "/api/photo")
register_task_routes(app, dedupe_mgr, "/api/dedupe")


# ------------------------------------------------------------------ 中间件
@app.before_request
def guard():
    return local_guard()


@app.after_request
def no_store_api(resp):
    """API 响应禁止缓存：参数更新或服务重启后，页面轮询不得拿到旧数据。"""
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ 页面
@app.route("/")
def index():
    return render_template("video.html")


@app.route("/photo")
def photo_page():
    return render_template("photo.html")


@app.route("/dedupe")
def dedupe_page():
    return render_template("dedupe.html")


# ------------------------------------------------------------------ 视频转码 API
@app.route("/api/video/info")
def video_info():
    return jsonify(video.info_payload())


@app.route("/api/video/preview")
def video_preview():
    payload, code = video.preview_payload(video_mgr)
    return jsonify(payload), code


@app.route("/api/video/probe")
def video_probe():
    p = clean_path(request.args.get("path"))
    if not p or not Path(p).is_file():
        return jsonify({"error": "路径不存在或不是文件"}), 400
    try:
        return jsonify(video.probe_payload(p))
    except subprocess.TimeoutExpired:
        return jsonify({"error": "读取视频信息超时"}), 500
    except Exception as e:
        return jsonify({"error": f"读取视频信息失败: {e}"}), 500


@app.route("/api/video/tasks", methods=["POST"])
def video_submit():
    params, err = video.params_from_request(request)
    if err:
        return jsonify({"error": err}), 400
    sources, err = video.collect_sources(request.form.getlist("path"))
    if err:
        return jsonify({"error": err}), 400
    if not sources:
        return jsonify({"error": "未选择任何文件"}), 400
    created = [video_mgr.serialize(video_mgr.new_task(src, name, dict(params)))
               for src, name in sources]
    log.info("提交 %d 个视频任务 预设=%s", len(created), params.get("preset"))
    return jsonify({"tasks": created})


# ------------------------------------------------------------------ 照片压缩 API
@app.route("/api/photo/caps")
def photo_caps():
    return jsonify(photo.caps_payload())


@app.route("/api/photo/scan")
def photo_scan():
    p = clean_path(request.args.get("path"))
    if not p:
        return jsonify({"error": "缺少路径"}), 400
    payload, code = photo.scan_payload(p)
    return jsonify(payload), code


@app.route("/api/photo/preview")
def photo_preview():
    payload, code = photo.preview_payload(photo_mgr)
    return jsonify(payload), code


@app.route("/api/photo/probe")
def photo_probe():
    p = clean_path(request.args.get("path"))
    if not p or not Path(p).is_file():
        return jsonify({"error": "路径不存在或不是文件"}), 400
    try:
        return jsonify(photo.probe_image(p))
    except Exception as e:
        return jsonify({"error": f"读取照片信息失败: {e}"}), 500


@app.route("/api/photo/tasks", methods=["POST"])
def photo_submit():
    params, err = photo.params_from_request(request)
    if err:
        return jsonify({"error": err}), 400
    sources, err = photo.collect_sources(request.form.getlist("path"),
                                         request.form.getlist("formats"))
    if err:
        return jsonify({"error": err}), 400
    if not sources:
        return jsonify({"error": "未选择任何照片"}), 400
    created = [photo_mgr.serialize(photo_mgr.new_task(src, name, dict(params)))
               for src, name in sources]
    log.info("提交 %d 个照片任务 预设=%s", len(created), params.get("preset"))
    return jsonify({"tasks": created})


# ------------------------------------------------------------------ 重复清理 API
@app.route("/api/dedupe/tasks", methods=["POST"])
def dedupe_submit():
    params, err = dedupe.params_from_request(request)
    if err:
        return jsonify({"error": err}), 400
    # 新扫描开始后旧结果立即失效
    dedupe.clear_result()
    root = Path(params["path"])
    task = dedupe_mgr.new_task(root, root.name, dict(params))
    log.info("开始重复扫描 %s exact=%s similar=%s 阈值=%d",
             root, params["exact"], params["similar"], params["threshold"])
    return jsonify({"task": dedupe_mgr.serialize(task)})


@app.route("/api/dedupe/result")
def dedupe_result():
    return jsonify(dedupe.result_payload())


@app.route("/api/dedupe/thumb")
def dedupe_thumb():
    p = clean_path(request.args.get("path"))
    resp = dedupe.thumb_response(p, request.args.get("size"))
    if resp is None:
        return jsonify({"error": "文件不在扫描结果中或不是图片"}), 404
    return resp


@app.route("/api/dedupe/delete", methods=["POST"])
def dedupe_delete():
    body = request.get_json(silent=True) or {}
    paths = body.get("paths") or []
    if not isinstance(paths, list) or not paths:
        return jsonify({"error": "未选择要删除的文件"}), 400
    results, err = dedupe.delete_files([str(p) for p in paths],
                                       body.get("mode", "recycle"))
    if err:
        return jsonify({"error": err}), 400
    ok = sum(1 for r in results if r["ok"])
    log.info("重复清理删除 %d/%d 个文件（%s）", ok, len(results), body.get("mode"))
    return jsonify({"results": results, "result": dedupe.result_payload()})


@app.route("/api/dedupe/reveal", methods=["POST"])
def dedupe_reveal():
    body = request.get_json(silent=True) or {}
    p = clean_path(body.get("path"))
    if not p or not dedupe.reveal(p):
        return jsonify({"error": "文件不在扫描结果中或已不存在"}), 404
    return jsonify({"ok": True})


# ------------------------------------------------------------------ 共享 API
# 文件对话框：按工具区分标题与扩展名过滤器
PICK_TOOLS = {
    "video": ("视频文件", "选择视频文件", "选择包含视频的文件夹", video.VIDEO_EXT),
    "photo": ("照片文件", "选择照片文件", "选择包含照片的文件夹", photo.IMAGE_EXT),
    "dedupe": ("文件", "选择文件", "选择要查重的文件夹", set()),
}


def _pick_cfg():
    return PICK_TOOLS.get(request.args.get("tool", "video"), PICK_TOOLS["video"])


@app.route("/api/pick", methods=["POST"])
def pick_files():
    label, title_files, title_dir, exts = _pick_cfg()
    return pick_dialog("files", label, title_files, title_dir, exts)


@app.route("/api/pick-dir", methods=["POST"])
def pick_dir():
    _, title_files, title_dir, _ = _pick_cfg()
    return pick_dialog("dir", "", title_files, title_dir, ())


RESTART_HELPER = BASE_DIR / "restart_helper.py"


@app.route("/api/restart", methods=["POST"])
def restart():
    # 先停掉两个工具的所有任务，再整体重启服务
    stops = [mgr.stop_all() for mgr in (video_mgr, photo_mgr)]
    for cancels in stops:
        for t in cancels:
            video.cancel_running(t)
    with video.child_lock:
        ff_pids = [str(p) for p in video.child_pids]
    try:
        kwargs = {"cwd": str(BASE_DIR), "close_fds": True}
        if IS_WINDOWS:
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP
                                       | CREATE_NO_WINDOW)
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, str(RESTART_HELPER),
             str(os.getpid()), str(BASE_DIR), *ff_pids], **kwargs)
        log.info("服务重启中 残留ffmpeg=%s", ff_pids)
        return jsonify({"ok": True, "message": "正在重启服务..."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ------------------------------------------------------------------ 启动
def start_workers():
    for mgr in (video_mgr, photo_mgr, dedupe_mgr):
        for _ in range(mgr.workers):
            threading.Thread(target=mgr.worker, daemon=True).start()


def _open_browser(url):
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    start_workers()
    if os.environ.get("MEDIA_NO_BROWSER") != "1":
        threading.Timer(1.2, _open_browser,
                        args=(f"http://{HOST}:{PORT}",)).start()
    log.info("启动 http://%s:%d video_workers=%d photo_workers=%d ffmpeg=%s "
             "pillow=%s 平台=%s", HOST, PORT, video_mgr.workers,
             photo_mgr.workers, video.FFMPEG, photo.PIL.__version__, sys.platform)
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
