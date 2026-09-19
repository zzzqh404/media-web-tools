# -*- coding: utf-8 -*-
"""照片压缩工具：Pillow 重编码，EXIF/ICC 保留、方向转正、可选缩放。

- 四种输出：JPEG 重压缩 / WebP / AVIF / PNG 无损
- 只处理本机文件，压缩结果与照片同目录，命名 {原名}{扩展名}
"""
import logging
import os
import re
import time
import uuid
from pathlib import Path

from flask import request
from PIL import Image, ImageOps, features
import PIL

from web_common import IS_WINDOWS, clean_path

try:
    import pillow_heif
    pillow_heif.register_heif_opener()  # 启用 HEIC/HEIF（iPhone 原图）读取
    HEIF_OK = True
except Exception:
    HEIF_OK = False

log = logging.getLogger("media.photo")

# .heic/.heif 的解码依赖 pillow-heif，未安装时相关任务会失败并提示
IMAGE_EXT = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp",
             ".tif", ".tiff", ".avif", ".heic", ".heif"}

# 展示/筛选用的格式组：jpg/jpeg/jfif 归为 jpg，tif/tiff 归为 tif
FORMAT_GROUP = {
    ".jpg": "jpg", ".jpeg": "jpg", ".jfif": "jpg",
    ".png": "png", ".webp": "webp", ".bmp": "bmp",
    ".tif": "tif", ".tiff": "tif",
    ".avif": "avif", ".heic": "heic", ".heif": "heif",
}

PRESETS = {
    "jpeg": {
        "label": "JPEG 重压缩",
        "note": "兼容性最好，通常能压 30%~70%",
        "format": "JPEG",
        "ext": ".jpg",
        "cap": None,
        "params": {"quality": "85", "lossless": "0", "speed": "6",
                   "resize": "original", "resize_px": "", "keep_exif": "1",
                   "del_source": "0"},
    },
    "webp": {
        "label": "WebP",
        "note": "同等画质比 JPEG 再小 25%~35%",
        "format": "WEBP",
        "ext": ".webp",
        "cap": "webp",
        "params": {"quality": "80", "lossless": "0", "speed": "6",
                   "resize": "original", "resize_px": "", "keep_exif": "1",
                   "del_source": "0"},
    },
    "avif": {
        "label": "AVIF",
        "note": "同画质体积最小，编码较慢",
        "format": "AVIF",
        "ext": ".avif",
        "cap": "avif",
        "params": {"quality": "60", "lossless": "0", "speed": "4",
                   "resize": "original", "resize_px": "", "keep_exif": "1",
                   "del_source": "0"},
    },
    "png": {
        "label": "PNG 无损",
        "note": "像素级无损，适合透明图/截图",
        "format": "PNG",
        "ext": ".png",
        "cap": None,
        "params": {"quality": "", "lossless": "0", "speed": "6",
                   "resize": "original", "resize_px": "", "keep_exif": "1",
                   "del_source": "0"},
    },
}

QUALITY_RANGE = {"JPEG": (1, 95), "WEBP": (0, 100), "AVIF": (0, 100)}
RESIZE_MODES = ("original", "long", "width", "height")
PARAM_KEYS = ["quality", "lossless", "speed", "resize", "resize_px", "keep_exif",
              "del_source"]

INT_RE = re.compile(r"^\d+$")

# 照片任务（尤其 JPEG/WebP）基本单核、秒级完成，默认并发取 CPU 核数的一半，
# 上限 8 防止大图并行时内存占用过高；PHOTO_WORKERS 可手动覆盖
_DEFAULT_WORKERS = max(2, min(8, (os.cpu_count() or 4) // 2))


def default_workers():
    return int(os.environ.get("PHOTO_WORKERS") or _DEFAULT_WORKERS)


# ------------------------------------------------------------ Pillow encoder
def _flatten_transparency(im):
    """透明图层合成到白底后转 RGB。"""
    rgba = im.convert("RGBA")
    bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(bg, rgba).convert("RGB")


def normalize_mode(im, fmt):
    """各输出格式只接受确定支持的颜色模式，其余安全转换。"""
    if fmt == "PNG":
        return im
    if fmt == "JPEG":
        if im.mode in ("RGB", "L", "CMYK"):
            return im
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            return _flatten_transparency(im)
        return im.convert("RGB")
    # WEBP / AVIF
    if im.mode in ("RGB", "RGBA", "L", "P"):
        return im
    if im.mode == "LA" or (im.mode == "P" and "transparency" in im.info):
        return im.convert("RGBA")
    return im.convert("RGB")


def resize_dims(w, h, mode, px):
    """按限制计算目标尺寸；只缩不放，返回 (w, h)。"""
    if mode == "long":
        if max(w, h) <= px:
            return w, h
        s = px / max(w, h)
    elif mode == "width":
        if w <= px:
            return w, h
        s = px / w
    elif mode == "height":
        if h <= px:
            return w, h
        s = px / h
    else:
        return w, h
    return max(1, round(w * s)), max(1, round(h * s))


ORIENT_TAG = 0x0112


def _synced_exif(transposed, original_exif):
    """取转正后同步过方向标签的 EXIF 字节流。

    优先用 Pillow 转正副本自带的 EXIF；缺失时从副本重建。若两者都拿不到，
    原 EXIF 带非 1 的方向标签时宁可丢弃——回传旧标签会让查看器把已转正
    的像素再转一次。
    """
    synced = transposed.info.get("exif")
    if not synced:
        ex = transposed.getexif()
        synced = ex.tobytes() if len(ex) else None
    if not synced and original_exif:
        try:
            stale = Image.Exif()
            stale.load(original_exif)
            if stale.get(ORIENT_TAG, 1) in (1, None):
                synced = original_exif
        except Exception:
            pass
    return synced


def encode_image(src, dst, preset_id, params):
    """用 Pillow 重编码单张照片：EXIF/ICC 保留、方向转正、可选缩放。"""
    preset = PRESETS[preset_id]
    fmt = preset["format"]
    keep = str(params.get("keep_exif", "1")).lower() not in ("0", "false", "no", "off")

    with Image.open(src) as im:
        exif = im.info.get("exif") if keep else None
        icc = im.info.get("icc_profile") if keep else None

        # 按方向标签把像素转正，之后一律以同步过标签的 EXIF 为准，
        # 避免查看器按旧标签二次旋转
        im = ImageOps.exif_transpose(im)
        if keep and exif:
            exif = _synced_exif(im, exif)
        im = normalize_mode(im, fmt)

        mode = params.get("resize", "original")
        if mode in ("long", "width", "height"):
            px = int(params.get("resize_px") or 0)
            if px >= 16:
                w, h = resize_dims(im.width, im.height, mode, px)
                if (w, h) != (im.width, im.height):
                    im = im.resize((w, h), Image.Resampling.LANCZOS)

        save_kw = {}
        if exif:
            save_kw["exif"] = exif
        if icc:
            save_kw["icc_profile"] = icc

        if fmt == "JPEG":
            save_kw["quality"] = int(params.get("quality") or 85)
            save_kw["optimize"] = True
            # Progressive JPEG 不支持 CMYK
            save_kw["progressive"] = im.mode != "CMYK"
        elif fmt == "WEBP":
            if str(params.get("lossless", "0")) == "1":
                save_kw["lossless"] = True
            else:
                save_kw["quality"] = int(params.get("quality") or 80)
            save_kw["method"] = 6
        elif fmt == "AVIF":
            save_kw["quality"] = int(params.get("quality") or 60)
            save_kw["speed"] = int(params.get("speed") or 4)
        else:  # PNG
            save_kw["optimize"] = True

        im.save(dst, fmt, **save_kw)


def probe_image(path):
    """用 Pillow 读取照片基本信息，供前端展示。"""
    p = Path(path)
    with Image.open(p) as im:
        im.load()
        try:
            ex = im.getexif()
            orientation = ex.get(0x0112)
            has_exif = bool(im.info.get("exif")) or len(ex) > 0
        except Exception:
            orientation, has_exif = None, False
        return {
            "path": str(p),
            "name": p.name,
            "format": im.format or p.suffix.lstrip(".").upper(),
            "mode": im.mode,
            "width": im.width,
            "height": im.height,
            "size_bytes": p.stat().st_size,
            "has_exif": has_exif,
            "orientation": orientation,
            "has_icc": bool(im.info.get("icc_profile")),
            "animated": bool(getattr(im, "is_animated", False)),
        }


# ------------------------------------------------------------- param builder
def validate_params(params, preset_id):
    """校验用户输入，返回错误文案或 None。"""
    preset = PRESETS.get(preset_id)
    if not preset:
        return f"未知预设: {preset_id}"
    fmt = preset["format"]

    if fmt in QUALITY_RANGE:
        lo, hi = QUALITY_RANGE[fmt]
        q = str(params.get("quality") or "")
        if not INT_RE.match(q) or not lo <= int(q) <= hi:
            return f"质量必须是 {lo}-{hi} 的整数"
    if fmt == "WEBP" and str(params.get("lossless", "0")) not in ("0", "1"):
        return "无效的 WebP 模式"
    if fmt == "AVIF":
        s = str(params.get("speed") or "")
        if not INT_RE.match(s) or not 0 <= int(s) <= 10:
            return "AVIF 速度必须是 0-10 的整数"
    if str(params.get("keep_exif", "1")) not in ("0", "1"):
        return "无效的元数据选项"
    if str(params.get("del_source", "0")) not in ("0", "1"):
        return "无效的删除原图选项"

    resize = params.get("resize", "original")
    if resize not in RESIZE_MODES:
        return f"无效的缩放选项: {resize}"
    if resize != "original":
        px = str(params.get("resize_px") or "")
        if not INT_RE.match(px) or not 16 <= int(px) <= 30000:
            return "缩放尺寸必须是 16-30000 的整数"
    return None


def params_from_request(req):
    """从表单/查询串取参数；返回 (params, 错误文案)。"""

    def g(key):
        v = req.form.get(key)
        if v is None:
            v = req.args.get(key)
        return v

    preset = g("preset") or "avif"
    if preset not in PRESETS:
        return None, "未知预设"
    defaults = PRESETS[preset]["params"]
    params = {k: (g(k) if g(k) is not None else defaults.get(k, ""))
              for k in PARAM_KEYS}
    params["preset"] = preset
    err = validate_params(params, preset)
    if err:
        return None, err
    return params, None


def build_summary(preset_id, params):
    """生成给前端「操作预览」用的一句摘要。"""
    fmt = PRESETS[preset_id]["format"]
    bits = []
    if fmt == "JPEG":
        bits.append(f"质量 {params.get('quality')}/95")
    elif fmt == "WEBP":
        if str(params.get("lossless")) == "1":
            bits.append("无损")
        else:
            bits.append(f"质量 {params.get('quality')}/100")
    elif fmt == "AVIF":
        bits.append(f"质量 {params.get('quality')}/100 · speed {params.get('speed')}")
    else:
        bits.append("无损压缩")
    bits.append("保留EXIF/ICC" if str(params.get("keep_exif", "1")) == "1"
                else "移除元数据")
    resize = params.get("resize", "original")
    px = params.get("resize_px") or "?"
    bits.append({"original": "尺寸不变",
                 "long": f"长边≤{px}px",
                 "width": f"宽≤{px}px",
                 "height": f"高≤{px}px"}[resize])
    return " · ".join(bits)


# ------------------------------------------------------------ output naming
def scan_image_files(root, formats=None):
    """递归收集文件夹下的照片（含子文件夹），按路径排序。

    formats 为格式组白名单（如 {'jpg', 'png'}）；None 表示不过滤。
    """
    root = Path(root)
    files = []
    for p in root.rglob("*"):
        group = FORMAT_GROUP.get(p.suffix.lower())
        if group and (formats is None or group in formats) and p.is_file():
            files.append(p)
    files.sort(key=lambda p: str(p).lower())
    return files


def output_candidate(src, name, params):
    """输出与源文件同名（仅扩展名随预设），冲突时由队列追加 _1、_2。

    同格式重压缩（如 photo.jpg -> JPEG）输出与源文件同名，
    因源文件已存在会自动落到 photo_1.jpg，绝不覆盖原图。
    """
    ext = PRESETS[params.get("preset", "jpeg")]["ext"]
    return Path(src).parent / (Path(name).stem + ext)


def cleanup_file(path):
    try:
        if path and Path(path).exists():
            Path(path).unlink()
    except Exception:
        pass


def run_task(mgr, t):
    src = Path(t["source"])
    out = Path(t["output"])
    if not src.exists():
        mgr.fail_task(t, "源文件不存在或已被删除")
        return
    try:
        t["src_size"] = src.stat().st_size
    except OSError:
        pass

    # 临时文件保留源扩展名，Pillow 据此判断输出格式
    tmp = out.parent / f"{out.name}.{uuid.uuid4().hex[:6]}.tmp{out.suffix}"
    try:
        encode_image(src, tmp, t["params"].get("preset", "jpeg"), t["params"])
        with mgr.lock:
            if t["status"] == "cancelling":
                t["status"] = "cancelled"
                t["error"] = "用户取消"
            else:
                os.replace(tmp, out)
                t["status"] = "completed"
                t["progress"] = 100.0
                t["out_size"] = out.stat().st_size
                t["finished"] = time.time()
        # 压缩成功后可选删除原图；仅在输出有效且与源不同名时执行，
        # 删除失败不影响任务状态
        if (t["status"] == "completed"
                and str(t["params"].get("del_source", "0")) == "1"
                and t["out_size"] > 0):
            try:
                if src.resolve() != out.resolve():
                    src.unlink()
                    log.info("已删除原图 %s", src)
                else:
                    log.warning("输出与源同路径，跳过删除原图 %s", src)
            except OSError as e:
                log.warning("原图删除失败 %s: %s", src, e)
    except Exception as e:
        log.exception("任务异常 %s", t["name"])
        mgr.fail_task(t, f"压缩失败: {e}")
    finally:
        cleanup_file(tmp)


# ------------------------------------------------------------- 接口数据
def caps_payload():
    """Pillow 能力与预设表（进程内不变，前端据此渲染预设卡）。"""
    presets = {}
    for pid, p in PRESETS.items():
        presets[pid] = {
            "label": p["label"],
            "note": p["note"],
            "ext": p["ext"],
            "available": (not p["cap"]) or features.check(p["cap"]),
            "params": p["params"],
        }
    return {
        "pillow": PIL.__version__,
        "heif_input": HEIF_OK,
        "workers": default_workers(),
        "presets": presets,
    }


def scan_payload(path):
    """探测路径类型：文件夹返回照片数量与格式分布（递归）。"""
    pp = Path(path)
    if pp.is_file():
        if pp.suffix.lower() not in IMAGE_EXT:
            return {"error": f"不支持的文件类型: {pp.name}"}, 400
        return {"type": "file", "count": 1}, 200
    if pp.is_dir():
        counts = {}
        for f in pp.rglob("*"):
            group = FORMAT_GROUP.get(f.suffix.lower())
            if group and f.is_file():
                counts[group] = counts.get(group, 0) + 1
        return {"type": "dir", "count": sum(counts.values()),
                "formats": counts}, 200
    return {"error": f"路径不存在: {path}"}, 400


def preview_payload(mgr):
    """操作预览摘要 + 输出路径提示；mgr 用于避开已预留的输出名。"""
    params, err = params_from_request(request)
    if err:
        return {"error": err}, 400
    preset = params["preset"]
    ext = PRESETS[preset]["ext"]
    name = (request.args.get("name") or "照片.jpg").strip() or "照片.jpg"
    stem = Path(name).stem
    path = (request.args.get("path") or "").strip().strip('"').strip("'")
    p = Path(path) if path else None
    if p and p.is_file():
        out_display = str(mgr.preview_output(output_candidate(p, name, params)))
    elif p and p.is_dir():
        sep = "\\" if IS_WINDOWS else "/"
        out_display = (f"{p}{sep}…（含子文件夹，每张照片输出到其所在文件夹，"
                       f"命名 原名{ext}）")
    elif p:
        out_display = str(p.parent / f"{stem}_{preset}{ext}")
    else:
        sep = "\\" if IS_WINDOWS else "/"
        out_display = f"「照片所在目录」{sep}{stem}_{preset}{ext}"
    return {"summary": build_summary(preset, params), "output": out_display}, 200


def collect_sources(form_paths, fmt_filters):
    """从路径列表收集待压缩照片；文件夹递归扫描并按格式组过滤。"""
    sources = []
    for idx, p in enumerate(form_paths):
        p = clean_path(p)
        if not p:
            continue
        fmts = None
        if idx < len(fmt_filters) and fmt_filters[idx].strip():
            groups = {x.strip().lower() for x in fmt_filters[idx].split(",") if x.strip()}
            unknown = groups - set(FORMAT_GROUP.values())
            if unknown:
                return None, f"不支持的输入格式: {', '.join(sorted(unknown))}"
            fmts = groups
        pp = Path(p)
        if pp.is_dir():
            # 文件夹：递归收集其下照片（含子文件夹），按勾选的输入格式过滤
            files = scan_image_files(pp, fmts)
            if not files:
                return None, f"文件夹中未找到照片: {p}"
            for f in files:
                sources.append((f, f.stem))
        elif pp.is_file():
            if pp.suffix.lower() not in IMAGE_EXT:
                return None, f"不支持的文件类型: {pp.name}"
            sources.append((pp, pp.stem))
        else:
            return None, f"路径不存在: {p}"
    return sources, None
