@echo off
title Cloud Camera - Edge Client
echo ============================================================
echo   CLOUD CAMERA - Edge Client
echo   Server: https://emergency-response-cloud.onrender.com
echo ============================================================
echo.
cd /d "%~dp0"
python test_camera_edge.py
pause
