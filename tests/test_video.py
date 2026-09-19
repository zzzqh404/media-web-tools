# -*- coding: utf-8 -*-
"""视频转码工具测试（从原 ffmpeg-web-encoder 迁移，路由改为 /api/video 前缀）。"""
import time
from pathlib import Path

import pytest

import app as appmod
import video as videomod


def wait_for(cond, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture()
def video_file(tmp_path):
    p = tmp_path / "sample.mp4"
    p.write_bytes(b"\x00" * 1024)
    return p


@pytest.fixture()
def dynamic_presets():
    """单测直接注册动态硬件预设（正常由 /api/video/info 探测后注册）。"""
    for codec, label in videomod.HW_PRESET_LABELS.items():
        videomod.DYNAMIC_PRESETS.setdefault(codec, videomod.make_hw_preset(codec, label))


def submit_one(client, path, **extra):
    form = {"preset": "x265", "path": str(path)}
    form.update(extra)
    r = client.post("/api/video/tasks", data=form)
    assert r.status_code == 200
    return r.get_json()["tasks"][0]


# ----------------------------------------------------------- 命令构建单测

def test_build_command_keeps_all_audio_by_default():
    params = dict(videomod.PRESETS["x265"]["params"], preset="x265")
    cmd = videomod.build_command("in.mp4", "out.mp4", "x265", params)
    i = cmd.index("-map")
    assert cmd[i:i + 4] == ["-map", "0:v:0", "-map", "0:a?"]


def test_build_command_single_audio_when_disabled():
    params = dict(videomod.PRESETS["x265"]["params"], preset="x265",
                  keep_all_audio="0")
    cmd = videomod.build_command("in.mp4", "out.mp4", "x265", params)
    assert "-map" not in cmd


def test_build_command_no_map_when_audio_removed():
    params = dict(videomod.PRESETS["x265"]["params"], preset="x265", audio="none")
    cmd = videomod.build_command("in.mp4", "out.mp4", "x265", params)
    assert "-map" not in cmd
    assert "-an" in cmd


def test_build_video_args_amf_qp_mode():
    params = dict(videomod.PRESETS["amd_hevc"]["params"], preset="amd_hevc")
    args = videomod.build_video_args("amd_hevc", params)
    assert args[:2] == ["-c:v", "hevc_amf"]
    assert "-rc" in args and "-qp_i" in args


def test_build_video_args_nvenc_cq_mode(dynamic_presets):
    params = {"mode": "crf", "crf": "26", "nvenc_preset": "p5",
              "bitrate": "8000k", "maxrate": "", "bufsize": ""}
    args = videomod.build_video_args("hevc_nvenc", params)
    assert args[:2] == ["-c:v", "hevc_nvenc"]
    assert "-cq" in args and "26" in args


def test_build_video_args_qsv_global_quality(dynamic_presets):
    params = {"mode": "crf", "crf": "24", "qsv_preset": "medium",
              "bitrate": "8000k", "maxrate": "", "bufsize": ""}
    args = videomod.build_video_args("hevc_qsv", params)
    assert args[:2] == ["-c:v", "hevc_qsv"]
    assert "-global_quality" in args and "24" in args


def test_unique_output_avoids_reserved(tmp_path):
    cand = videomod.output_candidate(tmp_path / "v.mp4", "v", {"preset": "x265"})
    a = appmod.video_mgr.preview_output(cand)
    with appmod.video_mgr.lock:
        appmod.video_mgr.reserved_outputs.add(str(a))
    b = appmod.video_mgr.preview_output(cand)
    assert a != b


# ------------------------------------------------------------- 参数校验

def test_validate_params_rejects_bad_crf():
    params = dict(videomod.PRESETS["x265"]["params"], preset="x265", crf="99")
    assert videomod.validate_params(params)


def test_validate_params_rejects_bad_custom_resolution():
    params = dict(videomod.PRESETS["x265"]["params"], preset="x265",
                  resolution="custom", res_w="abc", res_h="1080")
    assert videomod.validate_params(params)


def test_validate_params_rejects_bad_audio():
    params = dict(videomod.PRESETS["x265"]["params"], preset="x265",
                  audio="aac-999k")
    assert videomod.validate_params(params)


def test_validate_params_accepts_defaults():
    params = dict(videomod.PRESETS["amd_hevc"]["params"], preset="amd_hevc")
    assert videomod.validate_params(params) is None


# --------------------------------------------------------------- API 接口

def test_preview_bad_preset_400(client):
    r = client.get("/api/video/preview?preset=nope")
    assert r.status_code == 400


def test_preview_bad_crf_400(client):
    r = client.get("/api/video/preview?preset=x265&crf=99")
    assert r.status_code == 400


def test_preview_ok(client):
    r = client.get("/api/video/preview?preset=x265&crf=22")
    assert r.status_code == 200
    assert "libx265" in r.get_json()["cmd"]


def test_ffmpeg_info_uses_cache(client, monkeypatch):
    r = client.get("/api/video/info")
    assert r.status_code == 200
    presets = r.get_json()["presets"]
    assert "hevc_nvenc" in presets and "hevc_qsv" in presets
    assert presets["hevc_nvenc"]["codec"] == "hevc_nvenc"
    # 探测结果有缓存：把 ffmpeg 指向不存在的命令，二次请求仍返回同样内容
    monkeypatch.setattr(videomod, "FFMPEG", "definitely-not-ffmpeg")
    r2 = client.get("/api/video/info")
    assert r2.get_json() == r.get_json()


def test_probe(client, video_file):
    r = client.get(f"/api/video/probe?path={video_file}")
    assert r.status_code == 200
    data = r.get_json()
    assert data["duration"] == 60.0
    assert data["video"]["fps"] == 25.0


# ------------------------------------------------------------ 任务生命周期

def test_task_completes(client, worker_thread, video_file):
    t = submit_one(client, video_file)
    assert wait_for(lambda: appmod.video_mgr.tasks[t["id"]]["status"] == "completed")
    task = appmod.video_mgr.tasks[t["id"]]
    assert task["progress"] == 100.0
    assert Path(task["output"]).exists()
    assert task["out_size"] > 0
    assert task["src_size"] == 1024
    assert task["output"] not in appmod.video_mgr.reserved_outputs


def test_task_failure_cleans_partial_output(client, worker_thread, video_file,
                                            monkeypatch):
    monkeypatch.setenv("STUB_FAIL", "1")
    t = submit_one(client, video_file)
    assert wait_for(lambda: appmod.video_mgr.tasks[t["id"]]["status"] == "failed")
    task = appmod.video_mgr.tasks[t["id"]]
    assert task["error"]
    assert not Path(task["output"]).exists()
    assert task["output"] not in appmod.video_mgr.reserved_outputs


def test_cancel_running_task(client, worker_thread, video_file, monkeypatch):
    monkeypatch.setenv("STUB_STEP", "0.5")   # 拖慢桩，确保取消落在运行期
    t = submit_one(client, video_file)
    assert wait_for(lambda: appmod.video_mgr.tasks[t["id"]]["status"] == "running")
    r = client.post(f"/api/video/tasks/{t['id']}/cancel")
    assert r.status_code == 200
    assert wait_for(lambda: appmod.video_mgr.tasks[t["id"]]["status"] == "cancelled")
    assert t["output"] not in appmod.video_mgr.reserved_outputs


def test_retry_after_cancel(client, worker_thread, video_file, monkeypatch):
    monkeypatch.setenv("STUB_STEP", "0.3")
    t = submit_one(client, video_file)
    assert wait_for(lambda: appmod.video_mgr.tasks[t["id"]]["status"] == "running")
    client.post(f"/api/video/tasks/{t['id']}/cancel")
    assert wait_for(lambda: appmod.video_mgr.tasks[t["id"]]["status"] == "cancelled")
    r = client.post(f"/api/video/tasks/{t['id']}/retry")
    assert r.status_code == 200
    nt = r.get_json()
    assert nt["id"] != t["id"]
    assert wait_for(lambda: appmod.video_mgr.tasks[nt["id"]]["status"] == "completed")


def test_parallel_same_source_gets_distinct_outputs(client, video_file):
    t1 = submit_one(client, video_file)
    t2 = submit_one(client, video_file)
    assert t1["output"] != t2["output"]


def test_stop_all_cancels_queued(client, worker_thread, video_file, monkeypatch):
    monkeypatch.setenv("STUB_STEP", "0.3")
    t1 = submit_one(client, video_file)
    t2 = submit_one(client, video_file)
    assert wait_for(lambda: appmod.video_mgr.tasks[t1["id"]]["status"] == "running")
    r = client.post("/api/video/stop-all")
    assert r.status_code == 200
    assert wait_for(lambda: appmod.video_mgr.tasks[t2["id"]]["status"] == "cancelled")
    assert wait_for(lambda: appmod.video_mgr.tasks[t1["id"]]["status"] == "cancelled")
