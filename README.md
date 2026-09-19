# 本地媒体工具箱（media-web-tools）

仅限本机使用的网页版媒体处理工具箱，由两个姊妹项目合并而成：

- **🎬 视频转码**（原 [ffmpeg-web-encoder](../ffmpeg-web-encoder)）：浏览器里选视频、配参数、看进度，
  后端调系统 PATH 中的 ffmpeg 批量转码，输出 MP4
- **🖼️ 照片压缩**（原 [photo-web-compressor](../photo-web-compressor)）：Flask + Pillow 批量压缩照片，
  **EXIF 与 ICC 色彩配置完整保留**，竖拍照片按方向标签自动转正

两个工具共用一套界面风格、任务队列与文件对话框，通过顶部页签切换。

## 启动

```
双击 start.bat
```

会自动找到 Python、首次运行时安装依赖，然后启动服务并打开浏览器
（默认 `http://127.0.0.1:5000`，视频转码页；照片压缩在 `/photo`）。

手动启动：

```
pip install -r requirements.txt
python app.py
```

## 功能

### 视频转码（`/`）

- **添加视频**：系统文件/文件夹对话框（tkinter），或粘贴本地路径（支持整目录扫描，扫描一层）
- **编码预设**：AMD `hevc_amf` 硬编 / `libx265` 软编，各含 2K 压缩档；
  探测到 NVIDIA（nvenc）或 Intel（qsv）硬件编码器时自动追加预设卡片
- **参数可调**：CRF/QP 或码率模式、分辨率（含竖屏/自定义）、帧率、音频（复制/移除/AAC）、
  多音轨保留（默认开启，避免 ffmpeg 默认单流选择丢轨）、自定义 ffmpeg 参数
- **实时预览**：完整 ffmpeg 命令行预览（不执行），输出路径提示
- **任务队列**：默认 2 个并发 worker，实时进度/倍速/剩余时间，取消、停止全部、失败重试、
  下载成品、在资源管理器中定位输出文件

### 照片压缩（`/photo`）

- **四种输出格式**：JPEG 重压缩（质量 85）、WebP（质量 80，可无损）、AVIF（质量 60 · 速度 4）、PNG 无损
- **EXIF 完整保留**：原样复制 EXIF 数据块；竖拍照片自动转正像素并**同步方向标签**，不会二次旋转；
  ICC 色彩配置（如 iPhone 的 Display P3）一并保留；HEIC/HEIF 可直接读取（依赖 pillow-heif）
- **批量压缩**：选中文件夹自动**递归扫描**（含子文件夹），可按格式勾选只处理部分照片；
  每张照片的压缩结果输出到它所在的文件夹
- **可选删除原图**：压缩成功后自动删除源文件，**失败或取消的任务绝不删除**
- **可选缩放**：长边/宽/高不超过 N 像素，只缩不放

### 通用

- 三栏任务队列（待处理/正在处理/已完成），完成后显示压缩比、可下载或打开所在文件夹
- 详情弹窗：源/输出文件信息对比
- **安全防护**：仅允许 127.0.0.1/localhost 访问，校验 Origin 防跨站请求
- **输出防覆盖**：与现有文件或并行任务重名时自动追加 `_1`、`_2`，绝不覆盖

## 运行要求

- Windows（Linux 亦可运行，文件对话框依赖 tkinter）
- Python 3.10+
- ffmpeg / ffprobe 在 PATH 中（视频转码需要；照片压缩不依赖，
  没有可从 [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) 下载）
- 依赖：`flask`、`pillow>=11.0`（AVIF 内置）、`pillow-heif`（HEIC 读取）

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEDIA_HOST` | `127.0.0.1` | 监听地址 |
| `MEDIA_PORT` | `5000` | 监听端口 |
| `FFMPEG_WORKERS` | `2` | 视频转码并发线程数 |
| `PHOTO_WORKERS` | CPU 核数一半（2~8） | 照片压缩并发线程数 |
| `MEDIA_NO_BROWSER` | （空） | 设为 `1` 禁止自动打开浏览器 |

## 输出位置

- **视频**：输出到源文件所在目录，命名 `{原名}_{预设}.mp4`，重名自动加 `_1/_2`
- **照片**：输出到照片所在目录，命名 `{原名}{输出扩展名}`（如 `photo.png` → `photo.jpg`）；
  同格式重压缩因与源同名会自动存为 `photo_1.jpg`，**绝不覆盖原图**

## 项目结构

```
app.py               Flask 后端：页面路由、两个工具的 API、服务重启
task_manager.py      通用任务队列：任务模型、worker 循环、取消/重试等任务路由
video.py             视频转码：ffmpeg 预设、命令构建、进度解析
photo.py             照片压缩：Pillow 编码、EXIF/方向处理、目录扫描
web_common.py        共用辅助：本机访问守卫、路径清洗、文件对话框
dialog_helper.py     tkinter 文件对话框子进程
restart_helper.py    服务重启助手（独立进程）
static/common.js     共享前端：请求封装、任务看板（keyed 增量更新 + 轮询）
static/video.js      视频转码页逻辑
static/photo.js      照片压缩页逻辑
static/style.css     暗色主题样式（两页共用）
templates/*.html     两个单页界面
tests/               单元测试（ffmpeg 用桩模拟，照片用真实 Pillow）
```

## 测试

```
pip install pytest
python -m pytest
```
