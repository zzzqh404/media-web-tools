# -*- coding: utf-8 -*-
"""照片压缩工具测试（从原 photo-web-compressor 迁移，路由改为 /api/photo 前缀）。"""
import time
from pathlib import Path

import pytest
from PIL import Image, ImageOps

import app as appmod
import photo as photomod


def wait_for(cond, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.1)
    return False


MAKE_TAG, MODEL_TAG, ORIENT_TAG, DATETIME_TAG = 0x010F, 0x0110, 0x0112, 0x0132
ICC_DATA = b"fake-icc-profile-bytes"


def make_photo(path, size=(400, 200), exif=True, orientation=6, icc=True,
               color=(120, 40, 200), fmt="JPEG"):
    img = Image.new("RGB", size, color)
    kw = {}
    if exif:
        ex = Image.Exif()
        ex[MAKE_TAG] = "TestCam"
        ex[MODEL_TAG] = "Model X"
        ex[DATETIME_TAG] = "2026:01:01 10:00:00"
        if orientation:
            ex[ORIENT_TAG] = orientation
        kw["exif"] = ex
    if icc:
        kw["icc_profile"] = ICC_DATA
    img.save(path, fmt, **kw)
    return path


@pytest.fixture()
def jpg_file(tmp_path):
    return make_photo(tmp_path / "photo.jpg")


def submit_one(client, path, **extra):
    form = {"preset": "jpeg", "path": str(path)}
    form.update(extra)
    r = client.post("/api/photo/tasks", data=form)
    assert r.status_code == 200, r.get_json()
    return r.get_json()["tasks"][0]


def run_and_get(client, worker_thread, path, **extra):
    t = submit_one(client, path, **extra)
    assert wait_for(lambda: appmod.photo_mgr.tasks[t["id"]]["status"] == "completed")
    return appmod.photo_mgr.tasks[t["id"]]


# ------------------------------------------------------------- 参数校验

def test_validate_quality_ranges():
    base = {"resize": "original", "resize_px": "", "keep_exif": "1",
            "lossless": "0", "speed": "6"}
    assert photomod.validate_params(dict(base, quality="0"), "jpeg")
    assert photomod.validate_params(dict(base, quality="96"), "jpeg")
    assert photomod.validate_params(dict(base, quality="85"), "jpeg") is None
    assert photomod.validate_params(dict(base, quality="100"), "webp") is None
    assert photomod.validate_params(dict(base, quality="50"), "avif") is None
    # png 没有质量参数
    assert photomod.validate_params(dict(base, quality=""), "png") is None


def test_validate_resize():
    base = {"quality": "85", "lossless": "0", "speed": "6", "keep_exif": "1"}
    assert photomod.validate_params(dict(base, resize="long", resize_px=""), "jpeg")
    assert photomod.validate_params(dict(base, resize="long", resize_px="abc"), "jpeg")
    assert photomod.validate_params(dict(base, resize="diagonal", resize_px="100"), "jpeg")
    assert photomod.validate_params(dict(base, resize="long", resize_px="2048"),
                                    "jpeg") is None


def test_validate_bad_preset():
    assert photomod.validate_params({}, "nope")


def test_validate_del_source():
    base = {"quality": "85", "resize": "original", "resize_px": "",
            "keep_exif": "1", "lossless": "0", "speed": "6"}
    assert photomod.validate_params(dict(base, del_source="2"), "jpeg")
    assert photomod.validate_params(dict(base, del_source="1"), "jpeg") is None
    assert photomod.validate_params(dict(base, del_source="0"), "jpeg") is None


def test_params_from_request_uses_defaults(client):
    from flask import request as flask_request

    # 不带 preset 参数时默认 AVIF
    with client.application.test_request_context("/api/photo/preview"):
        params, err = photomod.params_from_request(flask_request)
    assert err is None
    assert params["preset"] == "avif"
    assert params["quality"] == "60"
    assert params["speed"] == "4"
    assert params["del_source"] == "0"   # 默认不删原图

    with client.application.test_request_context("/api/photo/preview?preset=webp"):
        params, err = photomod.params_from_request(flask_request)
    assert err is None
    assert params["preset"] == "webp"
    assert params["quality"] == "80"
    assert params["keep_exif"] == "1"


def test_params_from_request_rejects_bad(client):
    from flask import request as flask_request

    with client.application.test_request_context(
            "/api/photo/tasks?preset=jpeg&quality=999"):
        params, err = photomod.params_from_request(flask_request)
    assert params is None and err


# ---------------------------------------------------------------- 预览/命名

def test_preview_output_ext_per_preset(client):
    for preset, ext in (("jpeg", ".jpg"), ("webp", ".webp"),
                        ("avif", ".avif"), ("png", ".png")):
        r = client.get(f"/api/photo/preview?preset={preset}&name=abc.jpeg")
        assert r.status_code == 200
        out = r.get_json()["output"]
        assert Path(out).suffix == ext, out


def test_preview_bad_preset_400(client):
    r = client.get("/api/photo/preview?preset=nope")
    assert r.status_code == 400


def test_preview_bad_quality_400(client):
    r = client.get("/api/photo/preview?preset=jpeg&quality=99")
    assert r.status_code == 400


def test_unique_output_avoids_existing_and_reserved(tmp_path):
    cand = photomod.output_candidate(tmp_path / "v.jpg", "v", {"preset": "jpeg"})
    a = appmod.photo_mgr.preview_output(cand)
    (tmp_path / "v.jpg").write_bytes(b"x")
    b = appmod.photo_mgr.preview_output(cand)
    assert a != b


def test_output_name_keeps_source_name(tmp_path):
    """输出文件名不带预设后缀：photo.png -> photo.jpg；同名冲突才追加 _1。"""
    assert photomod.output_candidate(tmp_path / "photo.png", "photo",
                                     {"preset": "webp"}).name == "photo.webp"
    assert photomod.output_candidate(tmp_path / "photo.png", "photo",
                                     {"preset": "jpeg"}).name == "photo.jpg"
    # 同格式重压缩：源 photo.jpg 已存在，输出自动落到 photo_1.jpg，不覆盖原图
    (tmp_path / "photo.jpg").write_bytes(b"x")
    got = appmod.photo_mgr.preview_output(
        photomod.output_candidate(tmp_path / "photo.jpg", "photo", {"preset": "jpeg"}))
    assert got.name == "photo_1.jpg"


# ---------------------------------------------------------------- 能力探测

def test_caps(client):
    r = client.get("/api/photo/caps")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    data = r.get_json()
    assert data["workers"] >= 2
    for pid in ("jpeg", "webp", "avif", "png"):
        assert pid in data["presets"]
    assert data["presets"]["jpeg"]["available"] is True
    assert data["presets"]["webp"]["available"] is True
    assert data["presets"]["avif"]["available"] is True


def test_probe(client, jpg_file):
    r = client.get(f"/api/photo/probe?path={jpg_file}")
    assert r.status_code == 200
    data = r.get_json()
    assert data["width"] == 400 and data["height"] == 200
    assert data["has_exif"] is True
    assert data["orientation"] == 6
    assert data["has_icc"] is True


# ------------------------------------------------------------ 任务生命周期

def test_jpeg_task_preserves_exif_and_rotates(client, worker_thread, jpg_file):
    """核心需求：EXIF 保留、方向转正且标签同步、ICC 保留。"""
    out = run_and_get(client, worker_thread, jpg_file)
    assert Path(out["output"]).suffix == ".jpg"
    assert out["out_size"] > 0

    with Image.open(out["output"]) as im:
        ex = im.getexif()
        assert ex.get(MAKE_TAG) == "TestCam"
        assert ex.get(MODEL_TAG) == "Model X"
        assert ex.get(DATETIME_TAG) == "2026:01:01 10:00:00"
        # 像素已按 Orientation=6 转正：400x200 -> 200x400
        assert im.size == (200, 400)
        # 方向标签已同步（为 1 或删除），不会二次旋转
        assert ex.get(ORIENT_TAG) in (1, None)
        assert im.info.get("icc_profile") == ICC_DATA


def test_keep_exif_handles_upright_photo(client, worker_thread, tmp_path):
    """无方向标签的照片：EXIF 原样保留，尺寸不变。"""
    src = make_photo(tmp_path / "upright.jpg", orientation=None)
    out = run_and_get(client, worker_thread, src)
    with Image.open(out["output"]) as im:
        assert im.size == (400, 200)
        assert im.getexif().get(MAKE_TAG) == "TestCam"


def test_remove_exif_when_disabled(client, worker_thread, jpg_file):
    out = run_and_get(client, worker_thread, jpg_file, keep_exif="0")
    with Image.open(out["output"]) as im:
        assert len(im.getexif()) == 0
        assert not im.info.get("icc_profile")


def test_webp_task(client, worker_thread, jpg_file):
    out = run_and_get(client, worker_thread, jpg_file,
                      preset="webp", quality="80")
    assert Path(out["output"]).suffix == ".webp"
    with Image.open(out["output"]) as im:
        assert im.format == "WEBP"
        assert im.getexif().get(MAKE_TAG) == "TestCam"


def test_webp_lossless(client, worker_thread, jpg_file):
    out = run_and_get(client, worker_thread, jpg_file,
                      preset="webp", lossless="1", quality="80")
    with Image.open(out["output"]) as im:
        assert im.format == "WEBP"
        assert im.size == (200, 400)


def test_avif_task(client, worker_thread, jpg_file):
    out = run_and_get(client, worker_thread, jpg_file,
                      preset="avif", quality="50", speed="8")
    assert Path(out["output"]).suffix == ".avif"
    with Image.open(out["output"]) as im:
        assert im.format == "AVIF"
        assert im.getexif().get(MAKE_TAG) == "TestCam"


def test_png_task_lossless(client, worker_thread, jpg_file):
    src = jpg_file
    out = run_and_get(client, worker_thread, src, preset="png")
    assert Path(out["output"]).suffix == ".png"
    with Image.open(src) as src_im, Image.open(out["output"]) as im:
        assert im.format == "PNG"
        # PNG 无损：像素与转正后的原图完全一致
        expected = ImageOps.exif_transpose(src_im).convert("RGB")
        assert im.convert("RGB").tobytes() == expected.tobytes()
        assert im.getexif().get(MAKE_TAG) == "TestCam"


def test_delete_source_after_success(client, worker_thread, tmp_path):
    """勾选删除原图：压缩成功后源文件被删，输出完好。"""
    src = make_photo(tmp_path / "photo.jpg")
    out = run_and_get(client, worker_thread, src, del_source="1")
    assert Path(out["output"]).exists()
    assert not src.exists()


def test_delete_source_off_by_default(client, worker_thread, jpg_file):
    run_and_get(client, worker_thread, jpg_file)
    assert jpg_file.exists()


def test_failed_task_keeps_source(client, worker_thread, tmp_path, monkeypatch):
    """压缩失败绝不删除原图。"""
    src = make_photo(tmp_path / "photo.jpg")

    def bad_encode(s, d, p, ps):
        raise RuntimeError("x")

    monkeypatch.setattr(photomod, "encode_image", bad_encode)
    t = submit_one(client, src, del_source="1")
    assert wait_for(lambda: appmod.photo_mgr.tasks[t["id"]]["status"] == "failed")
    assert src.exists()


def test_cancelled_task_keeps_source(client, worker_thread, tmp_path, monkeypatch):
    """取消的任务不删除原图。"""
    src = make_photo(tmp_path / "photo.jpg")
    monkeypatch.setattr(photomod, "encode_image", slow_encode)
    t = submit_one(client, src, del_source="1")
    assert wait_for(lambda: appmod.photo_mgr.tasks[t["id"]]["status"] == "running")
    client.post(f"/api/photo/tasks/{t['id']}/cancel")
    assert wait_for(lambda: appmod.photo_mgr.tasks[t["id"]]["status"] == "cancelled")
    assert src.exists()


def test_folder_compression_leaves_videos_alone(client, worker_thread, tmp_path):
    """文件夹混有视频时：只处理照片，视频原样保留（含删除原图场景）。"""
    make_photo(tmp_path / "photo.jpg")
    video = tmp_path / "holiday.mp4"
    video.write_bytes(b"\x00" * 128)

    out = run_and_get(client, worker_thread, tmp_path,
                      del_source="1", preset="jpeg")
    assert Path(out["output"]).exists()          # 照片正常压缩
    assert not (tmp_path / "photo.jpg").exists()  # 勾了删原图：照片被删
    assert video.exists()                        # 视频完好无损
    assert video.read_bytes() == b"\x00" * 128


def test_resize_long_edge_downscales_only(client, worker_thread, jpg_file):
    out = run_and_get(client, worker_thread, jpg_file,
                      resize="long", resize_px="100")
    with Image.open(out["output"]) as im:
        # 源图带 Orientation=6，转正后为竖图 200x400，长边缩到 100
        assert im.size == (50, 100)

    # 小于限制的照片不放大（注意源图会被转正成 200x400）
    out2 = run_and_get(client, worker_thread, jpg_file,
                       resize="long", resize_px="4096")
    with Image.open(out2["output"]) as im:
        assert im.size == (200, 400)


def test_rgba_png_to_jpeg_gets_white_background(client, worker_thread, tmp_path):
    src = tmp_path / "alpha.png"
    Image.new("RGBA", (50, 50), (255, 0, 0, 0)).save(src, "PNG")
    out = run_and_get(client, worker_thread, src)
    with Image.open(out["output"]) as im:
        assert im.mode == "RGB"
        # 完全透明的像素落到白底上
        assert im.getpixel((0, 0)) == (255, 255, 255)


def test_submit_dir_scans_image_ext_only(client, tmp_path):
    make_photo(tmp_path / "a.jpg")
    make_photo(tmp_path / "b.png", fmt="PNG")
    make_photo(tmp_path / "c.webp", fmt="WEBP")
    (tmp_path / "notes.txt").write_text("x")
    r = client.post("/api/photo/tasks", data={"preset": "jpeg", "path": str(tmp_path)})
    assert r.status_code == 200
    assert len(r.get_json()["tasks"]) == 3


def test_submit_dir_recursive_scans_subfolders(client, tmp_path):
    """选中文件夹后自动递归扫描其下所有照片（含子文件夹）。"""
    make_photo(tmp_path / "top.jpg")
    sub = tmp_path / "子文件夹"
    deep = sub / "更深"
    deep.mkdir(parents=True)
    make_photo(sub / "mid.png", fmt="PNG")
    make_photo(deep / "leaf.webp", fmt="WEBP")
    (sub / "ignore.txt").write_text("x")

    r = client.post("/api/photo/tasks", data={"preset": "jpeg", "path": str(tmp_path)})
    assert r.status_code == 200
    tasks = r.get_json()["tasks"]
    assert len(tasks) == 3
    # 每张照片输出到其所在文件夹
    outs = {Path(t["source"]).name: Path(t["output"]).parent for t in tasks}
    assert outs["top.jpg"] == tmp_path
    assert outs["mid.png"] == sub
    assert outs["leaf.webp"] == deep


def test_scan_endpoint(client, tmp_path):
    make_photo(tmp_path / "a.jpg")
    sub = tmp_path / "sub"
    sub.mkdir()
    make_photo(sub / "b.png", fmt="PNG")
    (tmp_path / "x.txt").write_text("x")

    r = client.get(f"/api/photo/scan?path={tmp_path}")
    assert r.status_code == 200
    assert r.get_json() == {"type": "dir", "count": 2, "formats": {"jpg": 1, "png": 1}}

    r = client.get(f"/api/photo/scan?path={tmp_path / 'a.jpg'}")
    assert r.get_json() == {"type": "file", "count": 1}

    r = client.get(f"/api/photo/scan?path={tmp_path / 'x.txt'}")
    assert r.status_code == 400

    r = client.get("/api/photo/scan?path=Z:/no/such/dir")
    assert r.status_code == 400

    # 空文件夹：count 为 0，前端据此提示
    empty = tmp_path / "empty"
    empty.mkdir()
    r = client.get(f"/api/photo/scan?path={empty}")
    assert r.get_json() == {"type": "dir", "count": 0, "formats": {}}


def test_scan_reports_format_breakdown(client, tmp_path):
    make_photo(tmp_path / "a.jpg")
    make_photo(tmp_path / "b.jpeg")
    make_photo(tmp_path / "c.png", fmt="PNG")
    sub = tmp_path / "sub"
    sub.mkdir()
    make_photo(sub / "d.webp", fmt="WEBP")
    (tmp_path / "x.txt").write_text("x")

    r = client.get(f"/api/photo/scan?path={tmp_path}")
    assert r.status_code == 200
    data = r.get_json()
    assert data["type"] == "dir"
    assert data["count"] == 4
    assert data["formats"] == {"jpg": 2, "png": 1, "webp": 1}


def test_submit_dir_format_filter(client, tmp_path):
    """勾选输入格式后，只处理对应格式的照片。"""
    make_photo(tmp_path / "a.jpg")
    make_photo(tmp_path / "b.png", fmt="PNG")
    sub = tmp_path / "sub"
    sub.mkdir()
    make_photo(sub / "c.webp", fmt="WEBP")

    r = client.post("/api/photo/tasks", data={"preset": "jpeg", "path": str(tmp_path),
                                              "formats": "jpg"})
    assert r.status_code == 200
    tasks = r.get_json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["source"].endswith("a.jpg")

    r2 = client.post("/api/photo/tasks", data={"preset": "png", "path": str(tmp_path),
                                               "formats": "png,webp"})
    assert r2.status_code == 200
    assert len(r2.get_json()["tasks"]) == 2


def test_submit_dir_unknown_format_400(client, tmp_path):
    r = client.post("/api/photo/tasks", data={"preset": "jpeg", "path": str(tmp_path),
                                              "formats": "jpg,tiffx"})
    assert r.status_code == 400
    assert "不支持的输入格式" in r.get_json()["error"]


def test_submit_rejects_video_ext(client, tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00")
    r = client.post("/api/photo/tasks", data={"preset": "jpeg", "path": str(p)})
    assert r.status_code == 400
    assert "不支持" in r.get_json()["error"]


def test_submit_missing_path_400(client):
    r = client.post("/api/photo/tasks", data={"preset": "jpeg",
                                              "path": "Z:/definitely/missing.jpg"})
    assert r.status_code == 400


def test_parallel_same_source_distinct_outputs(client, tmp_path):
    f = make_photo(tmp_path / "photo.jpg")
    t1 = submit_one(client, f)
    t2 = submit_one(client, f)
    assert t1["output"] != t2["output"]


def slow_encode(src, dst, preset_id, params):
    time.sleep(0.8)
    Image.new("RGB", (10, 10)).save(dst, "JPEG")


def test_cancel_running_task(client, worker_thread, jpg_file, monkeypatch):
    monkeypatch.setattr(photomod, "encode_image", slow_encode)
    t = submit_one(client, jpg_file)
    assert wait_for(lambda: appmod.photo_mgr.tasks[t["id"]]["status"] == "running")
    r = client.post(f"/api/photo/tasks/{t['id']}/cancel")
    assert r.status_code == 200
    assert wait_for(lambda: appmod.photo_mgr.tasks[t["id"]]["status"] == "cancelled")
    assert not Path(t["output"]).exists()
    assert t["output"] not in appmod.photo_mgr.reserved_outputs


def test_cancel_queued_task(client, worker_thread, jpg_file, monkeypatch):
    monkeypatch.setattr(photomod, "encode_image", slow_encode)
    t1 = submit_one(client, jpg_file)
    t2 = submit_one(client, jpg_file)   # 排在队尾
    assert wait_for(lambda: appmod.photo_mgr.tasks[t1["id"]]["status"] == "running")
    assert appmod.photo_mgr.tasks[t2["id"]]["status"] == "queued"
    client.post(f"/api/photo/tasks/{t2['id']}/cancel")
    assert wait_for(lambda: appmod.photo_mgr.tasks[t2["id"]]["status"] == "cancelled")


def test_retry_after_failure(client, worker_thread, jpg_file, monkeypatch):
    def bad_encode(src, dst, preset_id, params):
        raise RuntimeError("boom")

    monkeypatch.setattr(photomod, "encode_image", bad_encode)
    t = submit_one(client, jpg_file)
    assert wait_for(lambda: appmod.photo_mgr.tasks[t["id"]]["status"] == "failed")
    assert "boom" in appmod.photo_mgr.tasks[t["id"]]["error"]
    assert not Path(t["output"]).exists()

    monkeypatch.setattr(photomod, "encode_image",
                        lambda s, d, p, ps: Image.new("RGB", (10, 10)).save(d, "JPEG"))
    r = client.post(f"/api/photo/tasks/{t['id']}/retry")
    assert r.status_code == 200
    nt = r.get_json()
    assert wait_for(lambda: appmod.photo_mgr.tasks[nt["id"]]["status"] == "completed")


def test_clear_removes_finished(client, worker_thread, jpg_file, monkeypatch):
    monkeypatch.setattr(photomod, "encode_image", slow_encode)
    t1 = submit_one(client, jpg_file)
    t2 = submit_one(client, jpg_file)
    assert wait_for(lambda: appmod.photo_mgr.tasks[t1["id"]]["status"] == "completed")
    client.post(f"/api/photo/tasks/{t2['id']}/cancel")
    assert wait_for(lambda: appmod.photo_mgr.tasks[t2["id"]]["status"] == "cancelled")
    client.post("/api/photo/tasks/clear")
    assert all(tid not in appmod.photo_mgr.tasks for tid in (t1["id"], t2["id"]))
