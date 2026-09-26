#!/usr/bin/env bash
# =============================================================================
# QUANT KALSHI - healthcheck contra /api/health
#
#     bash deploy/healthcheck.sh              # 8000, salida humana
#     bash deploy/healthcheck.sh 8000 --json  # JSON crudo
#
# Codigo de salida 0 = el bot responde. Sirve para cron, monitores externos o
# para el HEALTHCHECK de la imagen Docker.
#
# Un `status: degraded` (mercados o websocket viejos) NO devuelve error: dura minutos
# y reiniciar el proceso no lo arregla. Lo que SI devuelve error es un problema del
# camino de dinero real -- modo LIVE sin credenciales, LIVE bloqueado por el interlock
# o una reconciliacion con incidencias -- porque ahi el bot parece operar y no opera,
# o peor: opera sin saber que tiene ordenes abiertas.
# =============================================================================
set -euo pipefail

PORT="${1:-8000}"
MODE="${2:-}"
URL="http://127.0.0.1:${PORT}/api/health"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

PYTHON=""
for candidate in "${APP_DIR}/venv/bin/python" "$(command -v python3 || true)" "$(command -v python || true)"; do
    if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
        PYTHON="${candidate}"
        break
    fi
done

fetch_body() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 8 "${URL}"
    elif [ -n "${PYTHON}" ]; then
        "${PYTHON}" - "${URL}" <<'PY'
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=8) as r:
    sys.stdout.write(r.read().decode("utf-8", "replace"))
PY
    else
        echo "FALLO ${URL}: no hay curl ni python para consultar el endpoint" >&2
        exit 1
    fi
}

if ! BODY="$(fetch_body)"; then
    echo "FALLO ${URL} no responde" >&2
    exit 1
fi

if [ "${MODE}" = "--json" ]; then
    printf '%s\n' "${BODY}"
fi

if [ -z "${PYTHON}" ]; then
    echo "OK   ${URL} (sin interprete python: solo se comprueba que responda)"
    exit 0
fi

set +e
VERDICT="$(HEALTH_BODY="${BODY}" "${PYTHON}" - <<'PY'
import json, os

try:
    data = json.loads(os.environ.get("HEALTH_BODY", ""))
except Exception as exc:
    print(f"FALLO el cuerpo de /api/health no es JSON: {exc}")
    raise SystemExit(1)

live = data.get("trading_mode") or {}
mode = live.get("mode")
open_orders = data.get("open_live_orders")
cap = live.get("max_open_live_trades")

if mode == "LIVE":
    if not live.get("has_credentials"):
        print("FALLO modo LIVE sin credenciales Kalshi: el panel dice LIVE y no puede operar")
        raise SystemExit(1)
    if not live.get("real_money_allowed"):
        print(f"FALLO modo LIVE bloqueado por el interlock: {live.get('real_money_block_reason')}")
        raise SystemExit(1)
    errors = ((live.get("last_reconcile") or {}).get("errors")) or []
    if errors:
        print(f"FALLO reconciliacion con incidencias: {errors[:2]}")
        raise SystemExit(1)
    if cap and open_orders is not None and int(open_orders) >= int(cap):
        print(f"AVISO tope de ordenes vivas alcanzado ({open_orders}/{cap})")

if data.get("status") != "ok":
    print(f"AVISO degradado: problems={data.get('problems')}")

print(
    f"OK status={data.get('status')} modo={mode} entorno={data.get('kalshi_env')} "
    f"auth={data.get('kalshi_auth_verified')} ordenes_vivas={open_orders}"
)
PY
)"
STATUS=$?
set -e

if [ "${STATUS}" -ne 0 ]; then
    printf '%s\n' "${VERDICT}" >&2
    exit 1
fi

if [ "${MODE}" = "--json" ]; then
    # El JSON ya se imprimio en stdout: el veredicto va a stderr para no romper a los
    # monitores que parsean la salida.
    printf '%s\n' "${VERDICT}" >&2
else
    printf '%s\n' "${VERDICT}"
fi


