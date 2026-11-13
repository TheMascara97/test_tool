@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PAUSE_ON_EXIT=1"
set "RUN_TESTS=1"
:parse_args
if /i "%~1"=="--no-pause" set "PAUSE_ON_EXIT=0"
if /i "%~1"=="--skip-tests" set "RUN_TESTS=0"
shift
if not "%~1"=="" goto parse_args

echo ============================================
echo   设备测试工具箱 - 一键打包
echo ============================================

python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [错误] 未安装 PyInstaller，请先执行: pip install pyinstaller
    goto :fail
)

powershell -NoProfile -Command "if (Get-Process -Name ToolBox -ErrorAction SilentlyContinue) { exit 1 }"
if errorlevel 1 (
    echo [错误] ToolBox.exe 正在运行，请先关闭后重试。
    goto :fail
)

if "%RUN_TESTS%"=="1" (
    echo [1/4] 运行测试...
    python -m unittest discover -s tests
    if errorlevel 1 (
        echo [错误] 测试失败，已取消打包。
        goto :fail
    )
) else (
    echo [1/4] 已跳过测试。
)

echo [2/4] 清理旧的构建产物...
for /d %%d in (__pycache__ build) do (
    if exist "%%d" rmdir /s /q "%%d"
)
for /d %%d in ("dist\_MEI*") do (
    if exist "%%d" rmdir /s /q "%%d"
)

echo [3/4] 打包 ToolBox.exe...
python -m PyInstaller build_exe.spec --noconfirm --clean
if errorlevel 1 (
    echo [错误] 打包失败，请查看上方日志。
    goto :fail
)

echo [4/4] 清理打包中间产物...
for /d %%d in (__pycache__ build) do (
    if exist "%%d" rmdir /s /q "%%d"
)
for /d %%d in ("dist\_MEI*") do (
    if exist "%%d" rmdir /s /q "%%d"
)

echo.
echo 打包完成: dist\ToolBox.exe
echo 配置和日志仍在 dist\gui_config.json 与 dist\logs，不会被覆盖删除。
if "%PAUSE_ON_EXIT%"=="1" pause
exit /b 0

:fail
if "%PAUSE_ON_EXIT%"=="1" pause
exit /b 1
