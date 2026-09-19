# -*- coding: utf-8 -*-
"""假 ffmpeg/ffprobe 桩：覆盖 -version / -encoders / 时长探测 / 编码模拟。

通过 bat/sh 包装脚本以 sys.executable 运行（见根目录 conftest.py），
环境变量 STUB_FAIL=1 时模拟编码失败。
"""
import os
import pathlib
import sys
import time

args = sys.argv[1:]

if "-version" in args:
    print("ffmpeg version 7.1-stub")
    sys.exit(0)

if "-encoders" in args:
    for name in ["av1_amf", "av1_nvenc", "av1_qsv", "aac", "h264_amf",
                 "h264_nvenc", "h264_qsv", "hevc_amf", "hevc_nvenc",
                 "hevc_qsv", "libx264", "libx265"]:
        print(f" A....D {name}            stub encoder")
    sys.exit(0)

if "-show_entries" in args:          # ffprobe format=duration
    print("60.0")
    sys.exit(0)

if "-show_format" in args:           # ffprobe 完整媒体信息
    print('{"format": {"format_name": "mov,mp4", "duration": "60.0", "size": "1000000"},'
          ' "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920,'
          ' "height": 1080, "pix_fmt": "yuv420p", "avg_frame_rate": "25/1"}]}')
    sys.exit(0)

if os.environ.get("STUB_FAIL"):
    print("Error initializing output stream, stub failure")
    sys.exit(1)

# 编码模拟：最后一个位置参数是输出文件
out = args[-1]
step = float(os.environ.get("STUB_STEP", "0.02"))
for i in (1, 2, 3):
    print(f"out_time_us={i * 20_000_000}")
    print("speed=9.9x")
    sys.stdout.flush()
    time.sleep(step)
pathlib.Path(out).write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64)
print("progress=end")
sys.exit(0)
