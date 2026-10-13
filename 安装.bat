@echo off
chcp 65001 >nul
title see-vision 安装器
echo ==========================================
echo   see-vision 安装器 — 给 DeepSeek 装识图眼睛
echo ==========================================
echo.
echo 正在检测 Python...
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python 3 并勾选 Add to PATH
    pause
    exit /b 1
)
echo 找到 Python，开始安装...
python "%~dp0install.py"
echo.
echo 安装流程结束。按任意键关闭窗口。
pause >nul
