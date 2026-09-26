#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "=========================================="
echo "  QUANT KALSHI - Iniciando"
echo "=========================================="
echo ""

if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] Python3 no esta instalado"
    exit 1
fi

if [ ! -f "venv/bin/python" ]; then
    echo "[1/4] Creando entorno virtual..."
    python3 -m venv venv
fi

echo "[2/4] Activando entorno virtual..."
source venv/bin/activate

echo "[3/4] Instalando/actualizando dependencias..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo ""
echo "=========================================="
echo "  Servidor iniciado!"
echo "  Abre tu navegador en:"
echo "  http://localhost:8000/dashboard"
echo "=========================================="
echo ""
echo "Presiona Ctrl+C para detener"
echo ""

echo "[4/4] Ejecutando backend..."
python main.py
