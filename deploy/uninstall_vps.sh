#!/usr/bin/env bash
# =============================================================================
# QUANT KALSHI - desinstalar el servicio systemd (NO borra el checkout)
#
#     bash deploy/uninstall_vps.sh
#
# El bot deja de arrancar y se elimina la unidad. El codigo, el .env, los PEM y
# los JSON de estado se quedan donde estan.
# =============================================================================
set -euo pipefail

SERVICE_NAME="quantkalshi"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || { echo "Ejecuta como root o instala sudo." >&2; exit 1; }
    exec sudo -E bash "$0" "$@"
fi

systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
rm -f "${UNIT_PATH}"
systemctl daemon-reload
systemctl reset-failed "${SERVICE_NAME}" 2>/dev/null || true

echo "Servicio ${SERVICE_NAME} desinstalado. El checkout y el .env siguen intactos."
