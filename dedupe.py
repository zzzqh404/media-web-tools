# -*- coding: utf-8 -*-
"""重复文件/相似照片清理：目录扫描、内容哈希、感知哈希、分组与删除。

- 完全重复：先按文件大小分组，同大小再做快速哈希（前 256KB）预筛，
  预筛相同才计算全量 MD5，避免大文件无谓的全盘读取
- 相似照片：dHash 感知哈希（64 位，缩放到 9x8 灰度比较相邻像素），
  汉明距离 ≤ 阈值视为相似；JPEG 先 draft 降采样解码，大图代价很小
- 分组：并查集（MD5 相同或 dHash 相似即合并），每组给出建议保留项
  （分辨率最高 → 体积最大 → 修改时间最早）
- 删除：只允许删除出现在扫描结果中的路径；默认移入回收站（send2trash）
"""
import hashlib
import io
import logging
import threading
import time
from pathlib import Path

from flask import Response, jsonify, request
from PIL import Image, ImageOps

import photo
from web_common import clean_path, open_in_explorer

try:
    from send2trash import send2trash
except Exception:
    send2trash = None

log = logging.getLogger("media.dedupe")

QUICK_HASH_BYTES = 256 * 1024      # 快速哈希只读文件头
THUMB_SIZE = 240                   # 结果缩略图最长边
THUMB_CACHE_MAX = 400

# 扫描结果（进程内保留最近一次），{"root", "files": {path: rec}, "groups": [...],
# "stats": {...}, "threshold", "finished_at"}
LAST_SCAN = None
_scan_lock = threading.Lock()

# 缩略图缓存：{(path, mtime): jpeg bytes}
_thumb_cache = {}


# ----------------------------------------------------------------- 哈希工具
def file_md5(path, quick=False):
    h = hashlib.md5()
    with open(path, "rb") as f:
        if quick:
            h.update(f.read(QUICK_HASH_BYTES))
        else:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    return h.hexdigest()


def dhash(im):
    """9x8 灰度比较相邻像素，输出 64 位感知哈希（对亮度整体平移不敏感）。"""
    small = im.convert("L").resize((9, 8), Image.Resampling.BOX)
    px = small.tobytes()
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return bits


def phash_file(path):
    """返回 (dhash, 真实宽, 真实高)；JPEG 用 draft 降采样加速解码。"""
    with Image.open(path) as im:
        w, h = im.size                      # draft 之前取真实分辨率
        try:
            im.draft("L", (128, 128))
        except Exception:
            pass
        im.load()
        im = ImageOps.exif_transpose(im)
        return dhash(im), w, h


def hamming(a, b):
    return (a ^ b).bit_count()


# ------------------------------------------------------------------- 分组
def build_groups(recs, threshold):
    """对文件记录（dict，path 为键）做并查集分组，返回组列表。

    合并依据：MD5 完全相同，或（都算出了 dHash 时）汉明距离 ≤ 阈值。
    """
    recs = list(recs.values())
    n = len(recs)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_md5 = {}
    for i, r in enumerate(recs):
        if r.get("md5"):
            by_md5.setdefault(r["md5"], []).append(i)
    for lst in by_md5.values():
        for x in lst[1:]:
            union(lst[0], x)

    ph = [(i, r["phash"]) for i, r in enumerate(recs) if r.get("phash") is not None]
    for a in range(len(ph)):
        ia, ha = ph[a]
        for b in range(a + 1, len(ph)):
            ib, hb = ph[b]
            if find(ia) == find(ib):
                continue
            if hamming(ha, hb) <= threshold:
                union(ia, ib)

    comps = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(i)

    groups = []
    for members in comps.values():
        if len(members) < 2:
            continue
        files = [recs[i] for i in members]
        md5s = {r.get("md5") for r in files}
        kind = "exact" if len(md5s) == 1 and None not in md5s else "similar"
        # 建议保留：分辨率最高 → 体积最大 → 修改时间最早 → 路径稳定排序
        files.sort(key=lambda r: (-(r.get("w") or 0) * (r.get("h") or 0),
                                  -r["size"], r.get("mtime") or 0, r["path"]))
        for j, r in enumerate(files):
            r["suggest_delete"] = j > 0
        groups.append({
            "kind": kind,
            "files": files,
            "wasted": sum(r["size"] for r in files[1:]),
        })
    groups.sort(key=lambda g: (-g["wasted"], g["kind"]))
    for gid, g in enumerate(groups):
        g["id"] = gid
    return groups


def scan_stats(groups, total_files, skipped_images):
    return {
        "files": total_files,
        "skipped_images": skipped_images,
        "groups": len(groups),
        "exact_groups": sum(1 for g in groups if g["kind"] == "exact"),
        "similar_groups": sum(1 for g in groups if g["kind"] == "similar"),
        "wasted_bytes": sum(g["wasted"] for g in groups),
    }


# ------------------------------------------------------------------- 扫描任务
def _cancelled(t):
    return t.get("status") == "cancelling"


def _set_phase(t, phase, scanned=None, total=None, progress=None):
    t["phase"] = phase
    if scanned is not None:
        t["scanned"] = scanned
    if total is not None:
        t["total"] = total
    if progress is not None:
        t["progress"] = round(min(100.0, max(0.0, progress)), 2)


def run_task(mgr, t):
    global LAST_SCAN
    params = t["params"]
    root = Path(params["path"])
    do_exact = bool(params["exact"])
    do_similar = bool(params["similar"])
    threshold = params["threshold"]
    min_size = params["min_size"]

    # ---- 阶段 1：遍历文件
    _set_phase(t, "遍历文件", 0, 0, 0)
    records = {}
    for p in root.rglob("*"):
        if _cancelled(t):
            return
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size < min_size:
            continue
        records[str(p)] = {
            "path": str(p),
            "name": p.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "is_image": p.suffix.lower() in photo.IMAGE_EXT,
            "md5": None,
            "phash": None,
            "w": 0,
            "h": 0,
        }
        if len(records) % 100 == 0:
            _set_phase(t, "遍历文件", len(records), None, min(5.0, len(records) * 0.05))

    skipped_images = 0

    # ---- 阶段 2：完全重复（内容哈希）
    if do_exact:
        by_size = {}
        for r in records.values():
            by_size.setdefault(r["size"], []).append(r)
        candidates = [g for g in by_size.values() if len(g) > 1]
        total_hash = sum(len(g) for g in candidates)
        done = 0
        for g in candidates:
            by_quick = {}
            for r in g:
                if _cancelled(t):
                    return
                try:
                    by_quick.setdefault(file_md5(r["path"], quick=True), []).append(r)
                except OSError:
                    done += 1
                    continue
                done += 1
                if done % 10 == 0:
                    _set_phase(t, "校验相同文件", done, total_hash, 5 + done / max(1, total_hash) * 55)
            for sub in by_quick.values():
                if len(sub) < 2:
                    continue
                for r in sub:
                    if _cancelled(t):
                        return
                    try:
                        r["md5"] = file_md5(r["path"])
                    except OSError:
                        pass

    # ---- 阶段 3：相似照片（dHash）
    if do_similar:
        img_recs = [r for r in records.values() if r["is_image"]]
        done = 0
        for r in img_recs:
            if _cancelled(t):
                return
            try:
                ph, w, h = phash_file(r["path"])
                r["phash"], r["w"], r["h"] = ph, w, h
            except Exception:
                skipped_images += 1
            done += 1
            if done % 5 == 0 or done == len(img_recs):
                _set_phase(t, "分析相似照片", done, len(img_recs),
                           60 + done / max(1, len(img_recs)) * 30)

        _set_phase(t, "相似分组", len(img_recs), len(img_recs), 92)
        # build_groups 里的两两比较是纯内存位运算，这里不再单独报进度

    # ---- 阶段 4：分组与落账
    groups = build_groups(records, threshold)
    stats = scan_stats(groups, len(records), skipped_images)

    with mgr.lock:
        if t["status"] == "cancelling":
            return
        with _scan_lock:
            LAST_SCAN = {
                "root": str(root),
                "files": records,
                "groups": groups,
                "stats": stats,
                "threshold": threshold,
                "finished_at": time.time(),
            }
        t["status"] = "completed"
        t["progress"] = 100.0
        t["finished"] = time.time()
        t["phase"] = "完成"
        t["scanned"] = len(records)
        t["total"] = len(records)


def drop_paths(paths):
    """从最近一次扫描结果中移除已删除的文件并重建分组；返回更新后的结果。

    注意：不能在持有 _scan_lock 时调用 result_payload()（它也会加同一把锁）。
    """
    global LAST_SCAN
    with _scan_lock:
        scan = LAST_SCAN
        if scan is None:
            return None
        for p in paths:
            scan["files"].pop(p, None)
        scan["groups"] = build_groups(scan["files"], scan["threshold"])
        scan["stats"] = scan_stats(scan["groups"], len(scan["files"]), 0)
    return result_payload()


def result_payload():
    with _scan_lock:
        scan = LAST_SCAN
        if scan is None:
            return {"ready": False}
        return {
            "ready": True,
            "root": scan["root"],
            "finished_at": scan["finished_at"],
            "stats": scan["stats"],
            "threshold": scan["threshold"],
            "groups": scan["groups"],
        }


def clear_result():
    """新扫描开始时使旧结果失效。"""
    global LAST_SCAN
    with _scan_lock:
        LAST_SCAN = None
        _thumb_cache.clear()


# ------------------------------------------------------------------- 缩略图
def thumb_response(path):
    with _scan_lock:
        scan = LAST_SCAN
        rec = scan["files"].get(path) if scan else None
    if rec is None or not rec.get("is_image"):
        return None
    key = (path, rec["mtime"])
    data = _thumb_cache.get(key)
    if data is None:
        with Image.open(path) as im:
            try:
                im.draft("RGB", (THUMB_SIZE * 2, THUMB_SIZE * 2))
            except Exception:
                pass
            im.load()
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
        data = buf.getvalue()
        if len(_thumb_cache) >= THUMB_CACHE_MAX:
            _thumb_cache.pop(next(iter(_thumb_cache)))
        _thumb_cache[key] = data
    return Response(data, mimetype="image/jpeg")


# ------------------------------------------------------------------- 删除
def delete_files(paths, mode):
    """删除扫描结果中的文件；mode: recycle（默认，send2trash）或 permanent。"""
    with _scan_lock:
        scan = LAST_SCAN
    if scan is None:
        return None, "请先扫描"
    if mode not in ("recycle", "permanent"):
        return None, "无效的删除方式"
    if mode == "recycle" and send2trash is None:
        return None, "缺少 send2trash 依赖，无法移入回收站（pip install send2trash）"

    results = []
    ok_paths = []
    for p in paths:
        p = str(p)
        if p not in scan["files"]:
            results.append({"path": p, "ok": False, "error": "不在扫描结果中"})
            continue
        f = Path(p)
        if not f.exists():
            results.append({"path": p, "ok": False, "error": "文件不存在"})
            continue
        try:
            if mode == "recycle":
                send2trash(str(f))
            else:
                f.unlink()
            results.append({"path": p, "ok": True})
            ok_paths.append(p)
        except Exception as e:
            log.warning("删除失败 %s: %s", p, e)
            results.append({"path": p, "ok": False, "error": str(e)})

    if ok_paths:
        drop_paths(ok_paths)
    return results, None


# ------------------------------------------------------------------- 参数
def params_from_request(req):
    path = clean_path(req.form.get("path"))
    if not path or not Path(path).is_dir():
        return None, f"文件夹不存在: {path or '(空)'}"

    def flag(key, default):
        return (req.form.get(key, default) or default) in ("1", "true", "on")

    exact = flag("exact", "1")
    similar = flag("similar", "0")
    if not exact and not similar:
        return None, "至少选择一种查重方式"

    try:
        threshold = int(req.form.get("threshold") or 8)
    except ValueError:
        return None, "相似阈值必须是整数"
    threshold = max(0, min(24, threshold))

    try:
        min_size_kb = int(req.form.get("min_size_kb") or 64)
    except ValueError:
        return None, "最小文件大小必须是整数"
    min_size_kb = max(0, min(1024 * 1024, min_size_kb))

    return {
        "path": path,
        "exact": exact,
        "similar": similar,
        "threshold": threshold,
        "min_size": min_size_kb * 1024,
    }, None


def reveal(path):
    with _scan_lock:
        scan = LAST_SCAN
    if scan is None or path not in scan["files"]:
        return False
    p = Path(path)
    if not p.exists():
        return False
    open_in_explorer(p)
    return True
