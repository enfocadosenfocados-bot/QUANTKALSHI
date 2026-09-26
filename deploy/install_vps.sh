#!/usr/bin/env bash
# =============================================================================
# QUANT KALSHI - instalador 24/7 para VPS Linux (systemd)
#
# Uso, desde el directorio del repo ya clonado:
#     bash deploy/install_vps.sh
#
# Idempotente: se puede repetir para reparar/reinstalar. Pasos:
#   1. comprobar Python >= 3.11 (el codigo usa datetime.UTC)
#   2. crear venv e instalar requirements.txt
#   3. crear .env desde deploy/env.vps.example si falta
#   4. instalar y arrancar el servicio systemd `quantkalshi` (Restart=always)
#   5. esperar a que /api/health responda y resumir el estado
#
# Variables de escape: SKIP_TESTS=1  (no ejecutar la suite antes de instalar)
#                      PYTHON_BIN=python3.12
# =============================================================================
set -euo pipefail

SERVICE_NAME="quantkalshi"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
APP_PORT=8000                     # main.py sirve uvicorn en 0.0.0.0:8000
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-90}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

log()  { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[aviso]  \033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]  \033[0m %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- privilegios -------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || die "Ejecuta el script como root o instala sudo."
    log "Pidiendo sudo para instalar el servicio systemd..."
    exec sudo -E bash "$0" "$@"
fi

RUN_USER="${SUDO_USER:-}"
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
    RUN_USER="$(stat -c '%U' "$APP_DIR" 2>/dev/null || echo root)"
fi
id -u "$RUN_USER" >/dev/null 2>&1 || die "El usuario '$RUN_USER' no existe."
RUN_GROUP="$(id -gn "$RUN_USER" 2>/dev/null || echo "$RUN_USER")"

# El venv y el estado deben quedar a nombre del usuario real, no de root.
run_as_owner() {
    if [ "$RUN_USER" = "$(id -un)" ]; then "$@"
    elif command -v runuser >/dev/null 2>&1; then runuser -u "$RUN_USER" -- "$@"
    elif command -v sudo >/dev/null 2>&1; then sudo -u "$RUN_USER" -- "$@"
    else "$@"; fi
}

log "Proyecto:  ${APP_DIR}"
log "Servicio:  ${SERVICE_NAME}.service  (usuario ${RUN_USER}:${RUN_GROUP})"

# --- 1) Python ---------------------------------------------------------------
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "No encuentro '$PYTHON_BIN'."
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Se necesita Python 3.11+ (visto: $("$PYTHON_BIN" -V 2>&1)). Ubuntu 22.04: sudo apt install software-properties-common && sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv"
log "[1/5] $("$PYTHON_BIN" -V 2>&1) OK"

# --- 2) venv + dependencias --------------------------------------------------
if [ ! -x "${APP_DIR}/venv/bin/python" ]; then
    log "[2/5] Creando entorno virtual..."
    run_as_owner "$PYTHON_BIN" -m venv "${APP_DIR}/venv" \
        || die "No se pudo crear el venv. En Debian/Ubuntu instala python3-venv."
else
    log "[2/5] El entorno virtual ya existe."
fi
run_as_owner "${APP_DIR}/venv/bin/python" -m pip install --upgrade pip >/dev/null
run_as_owner "${APP_DIR}/venv/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"

# Red de seguridad: la suite es rapida (~1s) y no toca la red.
if [ "${SKIP_TESTS:-0}" = "1" ]; then
    warn "SKIP_TESTS=1: no se ejecutan los tests."
else
    log "Ejecutando la suite de tests antes de arrancar el servicio..."
    if ! (cd "$APP_DIR" && run_as_owner "${APP_DIR}/venv/bin/python" -m unittest discover -p 'test_*.py' >/tmp/quantkalshi_tests.log 2>&1); then
        tail -n 30 /tmp/quantkalshi_tests.log >&2 || true
        die "Los tests fallan: no se instala el servicio. Detalle en /tmp/quantkalshi_tests.log (SKIP_TESTS=1 para ignorarlo conscientemente)."
    fi
fi

# --- 3) .env y logs ----------------------------------------------------------
ENV_CREATED=0
if [ ! -f "${APP_DIR}/.env" ]; then
    cp "${APP_DIR}/deploy/env.vps.example" "${APP_DIR}/.env"
    ENV_CREATED=1
fi
chown "${RUN_USER}:${RUN_GROUP}" "${APP_DIR}/.env"
chmod 600 "${APP_DIR}/.env"
mkdir -p "${APP_DIR}/logs"
chown -R "${RUN_USER}:${RUN_GROUP}" "${APP_DIR}/logs"
log "[3/5] .env listo (permisos 600, propietario ${RUN_USER})"

# --- 4) servicio systemd -----------------------------------------------------
log "[4/5] Instalando ${UNIT_PATH}..."
sed -e "s|__APP_DIR__|${APP_DIR}|g" \
    -e "s|__RUN_USER__|${RUN_USER}|g" \
    -e "s|__RUN_GROUP__|${RUN_GROUP}|g" \
    "${SCRIPT_DIR}/quantkalshi.service" > "${UNIT_PATH}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true
systemctl restart "${SERVICE_NAME}"

# --- 5) healthcheck ----------------------------------------------------------
HEALTH_URL="http://127.0.0.1:${APP_PORT}/api/health"
health_ok() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1
    else
        "${APP_DIR}/venv/bin/python" -c "import urllib.request;urllib.request.urlopen('${HEALTH_URL}', timeout=5)" >/dev/null 2>&1
    fi
}

log "[5/5] Esperando a que responda ${HEALTH_URL} (hasta ${HEALTH_TIMEOUT_SECONDS}s)..."
HEALTHY=0
for _ in $(seq 1 $(( HEALTH_TIMEOUT_SECONDS / 3 ))); do
    if health_ok; then HEALTHY=1; break; fi
    sleep 3
done

echo
echo "============================================================"
if [ "$HEALTHY" = "1" ]; then
    echo " QUANT KALSHI operando 24/7"
else
    echo " El servicio esta instalado pero /api/health aun no responde"
fi
echo "============================================================"
systemctl --no-pager --lines=0 status "${SERVICE_NAME}" || true
echo
echo "Dashboard (solo desde tu PC, por tunel SSH):"
echo "  ssh -N -L ${APP_PORT}:127.0.0.1:${APP_PORT} ${RUN_USER}@TU_VPS"
echo "  luego abre http://localhost:${APP_PORT}/dashboard"
echo
echo "Operacion:"
echo "  journalctl -u ${SERVICE_NAME} -f          # logs en vivo"
echo "  systemctl restart ${SERVICE_NAME}         # reiniciar"
echo "  systemctl stop ${SERVICE_NAME}            # parar (kill-switch: para el proceso)"
echo "  bash deploy/update_vps.sh                 # actualizar codigo y reiniciar"
echo
if [ "$ENV_CREATED" = "1" ]; then
    warn "He creado ${APP_DIR}/.env desde la plantilla. SIN credenciales Kalshi el bot"
    warn "solo consume datos publicos y no puede operar. Edita el archivo:"
    warn "  nano ${APP_DIR}/.env    (KALSHI_PROD_KEY_ID + PEM en ~/.kalshi/, chmod 600)"
fi
if ! grep -qE '^ALLOW_REAL_MONEY=(1|true|yes|on)' "${APP_DIR}/.env" 2>/dev/null; then
    warn "ALLOW_REAL_MONEY no esta activado: el bot NO enviara ordenes con dinero real"
    warn "(en KALSHI_ENV=production el interlock las bloquea). Activalo solo despues"
    warn "de validar el cableado en demo: ALLOW_REAL_MONEY=true"
fi

