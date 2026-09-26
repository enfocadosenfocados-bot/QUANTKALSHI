@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo   QUANT KALSHI - Deteniendo
echo ==========================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_bot.ps1"

echo.
pause
