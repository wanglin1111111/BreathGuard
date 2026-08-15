@echo off
chcp 65001 >nul
title 护院鹅 · 信号测试播放器
cd /d "%~dp0"

:menu
cls
echo.
echo  ╔══════════════════════════════════════════════════╗
echo  ║     护院鹅 · 信号测试播放器                      ║
echo  ║     以下信号均基于硬件实测数据模拟                ║
echo  ╚══════════════════════════════════════════════════╝
echo.
echo  [1] 静止状态（静坐/睡眠，活动强度≈0，呼吸平稳）
echo  [2] 正常运动（日常走动，活动强度0.3-0.65）
echo  [3] 剧烈运动（跌倒瞬间，活动强度0.9-1.0）
echo  [4] 呼吸均匀（静息，呼吸14-17次/分）
echo  [5] 呼吸异常（呼吸加快+不整，22-30次/分）
echo  [6] 跌倒+呼吸正常（完整Zone 2语音确认流程）
echo  [7] 跌倒+呼吸异常（完整Zone 3立即告警流程）
echo  [8] 心梗老人跌倒（病历个性化，跳过语音直接告警）
echo  [9] 呼吸消失（Zone 4紧急救援）
echo  [0] 退出
echo.
set /p choice=请选择要播放的信号编号:

if "%choice%"=="1" set scenario=test_still
if "%choice%"=="2" set scenario=test_normal_motion
if "%choice%"=="3" set scenario=test_intense_motion
if "%choice%"=="4" set scenario=test_breathing_normal
if "%choice%"=="5" set scenario=test_breathing_abnormal
if "%choice%"=="6" set scenario=day_fall
if "%choice%"=="7" set scenario=day_fall_abnormal
if "%choice%"=="8" set scenario=day_heart_fall
if "%choice%"=="9" set scenario=day_breathing_lost
if "%choice%"=="0" exit

if not defined scenario (
    echo 无效选择，请重新输入
    timeout /t 2 >nul
    goto menu
)

echo.
echo  ▶ 正在播放：%scenario%
echo  ▶ 请确保边缘服务已启动（http://localhost:8000）
echo.
python -m replay.player --scenario %scenario% --speed 10

echo.
echo  ✔ 播放完毕
set scenario=
pause
goto menu
