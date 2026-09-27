# -*- coding: utf-8 -*-
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STUB_PY = ROOT / "tests" / "stubs" / "stub_ffmpeg.py"


@pytest.fixture(scope="session")
def stub_ffmpeg(tmp_path_factory):
    """生成假 ffmpeg/ffprobe 命令（bat/sh 包装 Python 桩），返回其路径。"""
    d = tmp_path_factory.mktemp("stubbin")
    if sys.platform == "win32":
        p = d / "ffmpeg-stub.bat"
        p.write_text(f'@"{sys.executable}" "{STUB_PY}" %*\r\n', encoding="ascii")
    else:
        p = d / "ffmpeg-stub.sh"
        p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STUB_PY}" "$@"\n')
        p.chmod(0o755)
    return str(p)


@pytest.fixture()
def client(monkeypatch, stub_ffmpeg):
    import app as appmod
    import dedupe as dedupemod
    import video as videomod

    monkeypatch.setattr(videomod, "FFMPEG", stub_ffmpeg)
    monkeypatch.setattr(videomod, "FFPROBE", stub_ffmpeg)
    videomod._ffi_cache = None
    videomod.DYNAMIC_PRESETS.clear()
    dedupemod.LAST_SCAN = None
    dedupemod._thumb_cache.clear()
    for mgr in (appmod.video_mgr, appmod.photo_mgr, appmod.dedupe_mgr):
        with mgr.lock:
            mgr.tasks.clear()
            mgr.queue.clear()
            mgr.reserved_outputs.clear()
    appmod.app.config["TESTING"] = True
    with appmod.app.test_client() as c:
        yield c


@pytest.fixture(scope="session")
def worker_thread():
    """每个工具各起一个 worker（单 worker 保证取消类测试的排队语义可预期）。"""
    import threading

    import app as appmod
    for mgr in (appmod.video_mgr, appmod.photo_mgr, appmod.dedupe_mgr):
        th = threading.Thread(target=mgr.worker, daemon=True)
        th.start()
