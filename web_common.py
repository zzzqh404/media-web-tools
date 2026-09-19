# -*- coding: utf-8 -*-
"""两个工具共用的 Web 辅助：本机访问守卫、路径清洗、文件对话框、资源管理器定位。"""
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from flask import jsonify, request

BASE_DIR = Path(__file__).resolve().parent

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DIALOG_HELPER = BASE_DIR / "dialog_helper.py"

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def clean_path(s):
    """去掉前端粘贴路径可能带的引号与首尾空白。"""
    return (s or "").strip().strip('"').strip("'")


def local_guard():
    """仅允许本机访问，并拒绝外站 Origin 的跨站请求。"""
    host = urlparse("http://" + (request.host or "")).hostname or ""
    if host not in LOCAL_HOSTS:
        return jsonify({"error": "仅允许本机访问"}), 403
    origin = request.headers.get("Origin")
    if origin:
        o = urlparse(origin)
        if (o.hostname or "") not in LOCAL_HOSTS:
            return jsonify({"error": "跨站请求被拒绝"}), 403
    return None


def open_in_explorer(path):
    if IS_WINDOWS:
        subprocess.Popen(["explorer", "/select,", str(path)],
                         creationflags=CREATE_NO_WINDOW)
    else:
        subprocess.Popen(["xdg-open", str(path)])


def pick_dialog(kind, label, title_files, title_dir, exts):
    """调起 tkinter 子进程弹出系统文件/文件夹对话框，结果 JSON 原样回传。

    kind: "files"（多选文件，附 exts 过滤器）或 "dir"（选文件夹）。
    """
    try:
        argv = [sys.executable, str(DIALOG_HELPER), kind, label, title_files, title_dir]
        if kind == "files":
            argv += sorted(exts)
        out = subprocess.run(argv, capture_output=True, timeout=600,
                             creationflags=CREATE_NO_WINDOW)
        raw = out.stdout or b""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        return jsonify(json.loads(text))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
