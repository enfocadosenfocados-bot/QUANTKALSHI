#!/usr/bin/env bash
# =============================================================================
# QUANT KALSHI - actualizar el bot en la VPS y reiniciarlo
#
#     bash deploy/update_vps.sh
#
# Hace: git pull --ff-only -> pip install -r requirements.txt -> reinicio del
# servicio -> healthcheck. Si el healthcheck no responde, muestra las ultimas
# lineas del journal para diagnosticar.
# =============================================================================
set -euo pipefail

SERVICE_NAME="quantkalshi"
APP_PORT=8000
HEALTH_URL="http://127.0.0.1:${APP_PORT}/api/health"

log()  { printf '\033[1;36m[update]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[aviso] \033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error] \033[0m %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$APP_DIR"

command -v git >/dev/null 2>&1 || die "git no esta instalado."
[ -x "${APP_DIR}/venv/bin/python" ] || die "No hay venv: ejecuta antes bash deploy/install_vps.sh"

# El .env y los JSON de estado estan en .gitignore: un pull nunca pisa datos ni
# credenciales. Los cambios locales sin commitear SI bloquean el pull.
if [ -n "$(git status --porcelain)" ]; then
    warn "Hay cambios locales sin commitear en ${APP_DIR}:"
    git --no-pager status --short
    die "Haz commit/stash antes de actualizar (o usa 'git stash' y repite)."
fi

log "Descargando cambios..."
git pull --ff-only

log "Actualizando dependencias..."
"${APP_DIR}/venv/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"

if [ "${SKIP_TESTS:-0}" != "1" ]; then
    log "Ejecutando la suite de tests..."
    "${APP_DIR}/venv/bin/python" -m unittest discover -p 'test_*.py' || die "Tests en rojo: no reinicio el servicio."
fi

log "Reiniciando ${SERVICE_NAME}..."
sudo systemctl restart "${SERVICE_NAME}"

health_ok() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1
    else
        "${APP_DIR}/venv/bin/python" -c "import urllib.request;urllib.request.urlopen('${HEALTH_URL}', timeout=5)" >/dev/null 2>&1
    fi
}

for _ in $(seq 1 30); do
    if health_ok; then log "OK: ${HEALTH_URL} responde. Version desplegada: $(git rev-parse --short HEAD)"; exit 0; fi
    sleep 3
done

warn "El healthcheck no respondio en 90s. Ultimas lineas del journal:"
sudo journalctl -u "${SERVICE_NAME}" -n 40 --no-pager || true
exit 1
