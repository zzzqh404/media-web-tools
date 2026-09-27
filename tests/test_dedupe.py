# -*- coding: utf-8 -*-
"""重复清理工具测试：精确重复、相似照片、删除校验与缩略图。"""
import time
from pathlib import Path

import pytest
from PIL import Image

import app as appmod
import dedupe as dedupemod


def wait_for(cond, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def pattern_a(size=(160, 120)):
    """确定性伪纹理；整体加常量不改变 dHash（无钳位），镜像则明显不同。"""
    im = Image.new("L", size)
    px = [(x * 3 + (x % 7) * 11 + y * 2) % 128
          for y in range(size[1]) for x in range(size[0])]
    im.putdata(px)
    return im


def pattern_b(size=(160, 120)):
    im = Image.new("L", size)
    px = [(x * 5 + (x % 5) * 13 + y * 7) % 128
          for y in range(size[1]) for x in range(size[0])]
    im.putdata(px)
    return im


def make_photo_folder(tmp_path):
    """返回 (base, brighter, other)：base 与 brighter 是相似照片。"""
    base = pattern_a()
    brighter = base.point(lambda v: v + 60)      # 整体提亮，dHash 不变
    other = pattern_b()
    paths = []
    for name, im in (("base.png", base), ("brighter.png", brighter),
                     ("other.png", other)):
        p = tmp_path / name
        im.save(p)
        paths.append(p)
    return paths


def scan_and_wait(client, worker_thread, folder, **extra):
    form = {"path": str(folder), "exact": "1", "similar": "1", "min_size_kb": "0"}
    form.update(extra)
    r = client.post("/api/dedupe/tasks", data=form)
    assert r.status_code == 200, r.get_json()
    tid = r.get_json()["task"]["id"]
    # 扫描任务没有输出文件
    assert r.get_json()["task"]["output"] == ""
    assert wait_for(lambda: appmod.dedupe_mgr.tasks[tid]["status"] == "completed")
    r2 = client.get("/api/dedupe/result")
    assert r2.status_code == 200
    return r2.get_json()


# ---------------------------------------------------------------- 参数校验

def test_result_not_ready(client):
    r = client.get("/api/dedupe/result")
    assert r.status_code == 200
    assert r.get_json() == {"ready": False}


def test_submit_validations(client, tmp_path):
    r = client.post("/api/dedupe/tasks", data={"path": ""})
    assert r.status_code == 400
    r = client.post("/api/dedupe/tasks", data={"path": "Z:/no/such/dir"})
    assert r.status_code == 400
    # 两种方式都没勾
    r = client.post("/api/dedupe/tasks",
                    data={"path": str(tmp_path), "exact": "0", "similar": "0"})
    assert r.status_code == 400
    assert "至少选择一种" in r.get_json()["error"]


# ---------------------------------------------------------------- 精确重复

def test_exact_duplicates_by_content_hash(client, worker_thread, tmp_path):
    """同大小不同内容不算重复；同内容才是完全重复。"""
    payload = b"\xab" * 2048
    (tmp_path / "a.bin").write_bytes(payload)
    (tmp_path / "b.bin").write_bytes(payload)
    (tmp_path / "c.bin").write_bytes(b"\xcd" * 2048)   # 同大小、不同内容
    (tmp_path / "d.bin").write_bytes(b"\xab" * 100)    # 不同大小

    data = scan_and_wait(client, worker_thread, tmp_path)
    assert data["ready"] is True
    assert data["stats"]["groups"] == 1
    g = data["groups"][0]
    assert g["kind"] == "exact"
    assert {Path(f["path"]).name for f in g["files"]} == {"a.bin", "b.bin"}
    assert g["wasted"] == 2048
    # 建议保留项唯一且未被标记删除
    keeps = [f for f in g["files"] if not f["suggest_delete"]]
    assert len(keeps) == 1


def test_min_size_filter(client, worker_thread, tmp_path):
    (tmp_path / "a.bin").write_bytes(b"\x00" * 2048)
    (tmp_path / "b.bin").write_bytes(b"\x00" * 2048)
    data = scan_and_wait(client, worker_thread, tmp_path, min_size_kb="64")
    assert data["stats"]["groups"] == 0
    assert data["stats"]["files"] == 0


# ---------------------------------------------------------------- 相似照片

def test_similar_photos_grouped(client, worker_thread, tmp_path):
    base, brighter, other = make_photo_folder(tmp_path)
    data = scan_and_wait(client, worker_thread, tmp_path)
    assert data["ready"] is True
    assert data["stats"]["similar_groups"] == 1
    g = data["groups"][0]
    assert g["kind"] == "similar"
    names = {Path(f["path"]).name for f in g["files"]}
    assert names == {"base.png", "brighter.png"}
    for f in g["files"]:
        assert f["w"] == 160 and f["h"] == 120


def test_similar_disabled_no_groups(client, worker_thread, tmp_path):
    make_photo_folder(tmp_path)
    data = scan_and_wait(client, worker_thread, tmp_path, similar="0")
    # 相似不查、三张图内容各不相同 → 无组
    assert data["stats"]["groups"] == 0


# ---------------------------------------------------------------- 缩略图

def test_thumb_endpoint(client, worker_thread, tmp_path):
    base, _, _ = make_photo_folder(tmp_path)
    scan_and_wait(client, worker_thread, tmp_path)
    r = client.get(f"/api/dedupe/thumb?path={base}")
    assert r.status_code == 200
    assert r.mimetype == "image/jpeg"

    # 不在扫描结果里的路径拒绝
    r2 = client.get(f"/api/dedupe/thumb?path={tmp_path / 'nope.png'}")
    assert r2.status_code == 404


def test_thumb_size_param(client, worker_thread, tmp_path):
    """size 参数控制预览图尺寸（灯箱用大图），非法值回退默认。"""
    base, _, _ = make_photo_folder(tmp_path)
    scan_and_wait(client, worker_thread, tmp_path)
    for size in ("240", "800", "bogus"):
        r = client.get(f"/api/dedupe/thumb?path={base}&size={size}")
        assert r.status_code == 200, size
        assert r.mimetype == "image/jpeg", size


# ---------------------------------------------------------------- 删除

def test_delete_permanent_and_regroup(client, worker_thread, tmp_path):
    payload = b"\xab" * 2048
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(payload)
    b.write_bytes(payload)
    data = scan_and_wait(client, worker_thread, tmp_path)
    victim = next(f for f in data["groups"][0]["files"] if f["suggest_delete"])

    r = client.post("/api/dedupe/delete", json={"paths": [victim["path"]],
                                                "mode": "permanent"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["results"] == [{"path": victim["path"], "ok": True}]
    assert not Path(victim["path"]).exists()
    # 组内只剩一个文件，组自动消失
    assert body["result"]["stats"]["groups"] == 0
    assert body["result"]["stats"]["wasted_bytes"] == 0


def test_delete_rejects_paths_outside_scan(client, worker_thread, tmp_path):
    (tmp_path / "a.bin").write_bytes(b"\x00" * 2048)
    b = tmp_path / "b.bin"
    b.write_bytes(b"\x00" * 2048)
    data = scan_and_wait(client, worker_thread, tmp_path)

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"\x00")
    r = client.post("/api/dedupe/delete",
                    json={"paths": [str(outside)], "mode": "permanent"})
    assert r.status_code == 200
    assert r.get_json()["results"][0]["ok"] is False
    assert "不在扫描结果中" in r.get_json()["results"][0]["error"]
    assert outside.exists()


def test_delete_recycle_uses_send2trash(client, worker_thread, tmp_path, monkeypatch):
    payload = b"\xab" * 2048
    (tmp_path / "a.bin").write_bytes(payload)
    (tmp_path / "b.bin").write_bytes(payload)
    data = scan_and_wait(client, worker_thread, tmp_path)
    victim = next(f for f in data["groups"][0]["files"] if f["suggest_delete"])

    calls = []
    monkeypatch.setattr(dedupemod, "send2trash",
                        lambda p: calls.append(p))
    r = client.post("/api/dedupe/delete", json={"paths": [victim["path"]],
                                                "mode": "recycle"})
    assert r.status_code == 200
    assert calls == [victim["path"]]
    # 文件本体还在（被 monkeypatch 的假回收站“移走”逻辑没删）
    assert Path(victim["path"]).exists()


def test_delete_without_scan_400(client):
    r = client.post("/api/dedupe/delete", json={"paths": ["C:/x.bin"]})
    assert r.status_code == 400


def test_reveal_validates_path(client, worker_thread, tmp_path, monkeypatch):
    base, _, _ = make_photo_folder(tmp_path)
    scan_and_wait(client, worker_thread, tmp_path)

    opened = []
    monkeypatch.setattr(dedupemod, "open_in_explorer", lambda p: opened.append(p))

    r = client.post("/api/dedupe/reveal", json={"path": str(base)})
    assert r.status_code == 200
    assert opened == [Path(base)]

    r2 = client.post("/api/dedupe/reveal", json={"path": str(tmp_path / "x.png")})
    assert r2.status_code == 404
