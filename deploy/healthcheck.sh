#!/usr/bin/env bash
# =============================================================================
# QUANT KALSHI - healthcheck contra /api/health
#
#     bash deploy/healthcheck.sh              # 8000, salida humana
#     bash deploy/healthcheck.sh 8000 --json  # JSON crudo
#
# Codigo de salida 0 = el bot responde. Sirve para cron, monitores externos o
# para el HEALTHCHECK de la imagen Docker.
# =============================================================================
set -euo pipefail

PORT="${1:-8000}"
MODE="${2:-}"
URL="http://127.0.0.1:${PORT}/api/health"

if command -v curl >/dev/null 2>&1; then
    if [ "$MODE" = "--json" ]; then
        curl -fsS --max-time 8 "$URL"
    else
        if BODY="$(curl -fsS --max-time 8 "$URL")"; then
            echo "OK   ${URL}"
            echo "${BODY}"
        else
            echo "FALLO ${URL} no responde" >&2
            exit 1
        fi
    fi
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
    "${APP_DIR}/venv/bin/python" - "$URL" <<'PY'
import sys, urllib.request
url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=8) as r:
        body = r.read().decode("utf-8", "replace")
except Exception as exc:
    print(f"FALLO {url}: {exc}", file=sys.stderr)
    raise SystemExit(1)
print(f"OK   {url}")
print(body)
PY
fi
