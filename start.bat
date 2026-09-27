@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0"
title 本地媒体工具箱（视频转码 + 照片压缩）

echo ============================================
echo   本地媒体工具箱  正在启动...
echo   视频转码（ffmpeg） + 照片压缩（Pillow）
echo ============================================
echo.

rem ---- 查找 Python（优先用 python.org 安装的完整版）----
set "PYEXE="
set "CANDIDATES=%LOCALAPPDATA%\Programs\Python\Python314\python.exe;%LOCALAPPDATA%\Programs\Python\Python313\python.exe;%LOCALAPPDATA%\Programs\Python\Python312\python.exe;%LOCALAPPDATA%\Programs\Python\Python311\python.exe;%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
for %%P in (%CANDIDATES%) do (
    if not defined PYEXE (
        if exist "%%P" set "PYEXE=%%P"
    )
)
if not defined PYEXE (
    where py >nul 2>nul
    if not errorlevel 1 set "PYEXE=py"
)
if not defined PYEXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    echo [错误] 未找到 Python，请先安装 Python 3.10 或更高版本。
    echo        下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo 使用 Python: %PYEXE%
echo.

rem ---- 检查 ffmpeg（视频转码需要；照片压缩不依赖，缺失仅警告）----
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [警告] 未在 PATH 中找到 ffmpeg，视频转码功能不可用，照片压缩不受影响。
    echo        如需转码，请安装 ffmpeg: https://www.gyan.dev/ffmpeg/builds/
    timeout /t 5 >nul
    echo.
)

rem ---- 安装依赖（flask + Pillow + pillow-heif + send2trash）----
"%PYEXE%" -c "import flask, PIL, send2trash" >nul 2>nul
if errorlevel 1 (
    echo 首次运行，正在安装依赖...
    "%PYEXE%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        "%PYEXE%" -m pip install -r requirements.txt
    )
    echo.
)

rem ---- 启动服务（app.py 会在服务就绪后自动打开浏览器）----
"%PYEXE%" app.py

echo.
echo 服务已停止。
pause
