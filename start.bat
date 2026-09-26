@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo   QUANT KALSHI - Iniciando
echo ==========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python no esta instalado o no esta en PATH. Descargalo de https://python.org
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo [1/4] Creando entorno virtual...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] No se pudo crear el entorno virtual.
        pause
        exit /b 1
    )
)

echo [2/4] Activando entorno virtual...
call "venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] No se pudo activar el entorno virtual.
    pause
    exit /b 1
)

echo [3/4] Instalando/actualizando dependencias...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] No se pudieron instalar las dependencias.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   Servidor iniciado!
echo   Abre tu navegador en:
echo   http://localhost:8000/dashboard
echo ==========================================
echo.
echo Presiona Ctrl+C para detener
echo.

echo [4/4] Ejecutando backend...
python main.py

pause
