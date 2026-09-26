@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo   QUANT KALSHI - Modo 24/7 (PAPER)
echo ==========================================
echo.
echo Arrancando el supervisor en segundo plano.
echo Se reiniciara solo si el bot falla o deja de responder.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File','%~dp0run_forever.ps1' -WindowStyle Hidden"

timeout /t 3 /nobreak >nul

echo Dashboard:  http://localhost:8000/dashboard
echo Estado:     http://localhost:8000/api/health
echo Logs:       %~dp0logs
echo.
echo Para detenerlo: stop_bot.bat
echo.

start "" http://localhost:8000/dashboard
