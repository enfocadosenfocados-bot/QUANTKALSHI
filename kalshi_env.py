"""Resolución dinámica del entorno Kalshi (demo vs producción).

Kalshi no comparte credenciales entre entornos: una API key creada en
producción devuelve 401 contra demo y viceversa. Este módulo permite
`KALSHI_ENV=auto` para detectar automáticamente en qué entorno son válidas
las credenciales configuradas y ajustar REST/WebSocket en consecuencia.

Dos decisiones aprendidas a golpe de bug:

1. La sonda es `/portfolio/positions`, no `/portfolio/balance`. En demo el
   endpoint de balance devuelve HTTP 500 de forma intermitente (~30% de los
   intentos medidos) y un 500 se interpretaba como "este entorno no vale",
   haciendo que `auto` aterrizara en producción aunque la clave demo fuese
   correcta. Ahora un 5xx se reintenta y no es concluyente.

2. Cada entorno puede tener su propio par de credenciales (`KALSHI_DEMO_*` /
   `KALSHI_PROD_*`). Si existen, se usan las del entorno candidato; si no, se
   recurre a las legacy, que no declaran entorno y por eso se prueban contra
   ambos y es la detección la que decide.

Cualquier operación de trading sigue bloqueada por defecto (modo PAPER);
aquí solo se decide contra qué hosts hablar.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

import httpx

from config import (
    KALSHI_ENV,
    KALSHI_ENV_PROBE_ATTEMPTS,
    KALSHI_ENV_PROBE_PATH,
    KALSHI_REST_BASES,
    KALSHI_WS_BASES,
    kalshi_credentials,
    kalshi_credentials_source,
)

VALID_ENVIRONMENTS = ("demo", "production")


def mask_key_id(key_id: str) -> str:
    """Enmascara un key id para poder mostrarlo sin filtrarlo."""
    value = (key_id or "").strip()
    if not value:
        return ""
    if len(value) <= 12:
        return value[:4] + "..."
    return f"{value[:8]}...{value[-4:]}"


class KalshiEnvironment:
    """Contenedor mutable del entorno activo de Kalshi."""

    def __init__(self) -> None:
        self.requested: str = KALSHI_ENV
        initial = "demo" if KALSHI_ENV == "auto" else KALSHI_ENV
        if initial not in VALID_ENVIRONMENTS:
            initial = "demo"
        self.env: str = initial
        self.rest_base: str = KALSHI_REST_BASES[initial]
        self.ws_base: str = KALSHI_WS_BASES[initial]
        self.resolved: bool = KALSHI_ENV != "auto"
        self.detection_note: str = ""
        self.auth_verified: Optional[bool] = None
        # Resultado del último sondeo por entorno: {"demo": {"status": 200, ...}}
        self.probe_results: Dict[str, Dict[str, Any]] = {}
        self.active_credentials: Dict[str, str] = kalshi_credentials(initial)

    @property
    def is_demo(self) -> bool:
        return self.env == "demo"

    def apply(self, env: str) -> None:
        """Fijar el entorno activo (sin validar credenciales)."""
        if env not in VALID_ENVIRONMENTS:
            env = "demo"
        self.env = env
        self.rest_base = KALSHI_REST_BASES[env]
        self.ws_base = KALSHI_WS_BASES[env]
        self.active_credentials = kalshi_credentials(env)

    def auth_for(self, env: str):
        """Construye un KalshiAuth con las credenciales de un entorno concreto."""
        from kalshi_auth import KalshiAuth

        creds = kalshi_credentials(env)
        return KalshiAuth(
            creds["key_id"],
            creds["private_key_path"],
            creds["private_key_pem"],
        )

    def _probe_once(self, auth, env: str) -> Tuple[Optional[int], str]:
        base = KALSHI_REST_BASES[env]
        headers = auth.headers("GET", "/trade-api/v2" + KALSHI_ENV_PROBE_PATH)
        if not headers:
            return None, "no se pudo firmar la petición"
        resp = httpx.get(f"{base}{KALSHI_ENV_PROBE_PATH}", headers=headers, timeout=10.0)
        return resp.status_code, (resp.text or "")[:180]

    def _probe(self, auth, env: str) -> Tuple[Optional[int], str]:
        """Sondea un entorno. Un 5xx no es concluyente, así que se reintenta."""
        attempts = max(1, KALSHI_ENV_PROBE_ATTEMPTS)
        last_status: Optional[int] = None
        last_detail = ""
        for attempt in range(attempts):
            try:
                status, detail = self._probe_once(auth, env)
            except Exception as exc:
                last_status, last_detail = None, f"{type(exc).__name__}: {exc}"
                time.sleep(0.4 * (attempt + 1))
                continue
            last_status, last_detail = status, detail
            if status is not None and status < 500:
                return status, detail
            if attempt < attempts - 1:
                time.sleep(0.4 * (attempt + 1))
        return last_status, last_detail

    def _resolve_candidate_auth(self, candidate: str, fallback_auth=None):
        """Auth y origen de credenciales para un entorno candidato."""
        source = kalshi_credentials_source(candidate)
        creds = kalshi_credentials(candidate)
        if source == "specific":
            return self.auth_for(candidate), source, creds
        if fallback_auth is not None and getattr(fallback_auth, "configured", False):
            return fallback_auth, source, {
                "key_id": getattr(fallback_auth, "key_id", ""),
                "private_key_path": getattr(fallback_auth, "private_key_path", ""),
                "private_key_pem": "",
            }
        return self.auth_for(candidate), source, creds

    def resolve(self, auth=None) -> str:
        """Detectar el entorno donde las credenciales son válidas.

        Solo se ejecuta cuando `KALSHI_ENV=auto`. Si no hay credenciales, se
        mantiene demo (los datos públicos REST funcionan sin autenticación).
        """
        if self.resolved and self.auth_verified is not None:
            return self.env

        is_auto = KALSHI_ENV == "auto"
        candidates = VALID_ENVIRONMENTS if is_auto else (KALSHI_ENV,)
        probed_any = False
        conclusive = False

        for candidate in candidates:
            probe_auth, source, creds = self._resolve_candidate_auth(candidate, auth)
            if not getattr(probe_auth, "configured", False):
                self.probe_results[candidate] = {
                    "status": None,
                    "detail": "sin credenciales configuradas",
                    "credentials_source": source,
                    "key_id": "",
                }
                continue

            probed_any = True
            status, detail = self._probe(probe_auth, candidate)
            self.probe_results[candidate] = {
                "status": status,
                "detail": detail,
                "credentials_source": source,
                "key_id": mask_key_id(creds.get("key_id", "")),
            }

            if status is None:
                # Error de red: no concluyente, se deja sin resolver para reintentar.
                continue

            conclusive = True

            if status == 200:
                self.apply(candidate)
                self.auth_verified = True
                self.detection_note = f"credenciales válidas en {candidate}"
                self.resolved = True
                return self.env

            if not is_auto:
                # Entorno fijado a mano: se respeta aunque las credenciales fallen,
                # pero se reporta en el arranque para no descubrirlo horas después.
                self.apply(candidate)
                self.auth_verified = False
                self.detection_note = (
                    f"entorno fijado a {candidate} (KALSHI_ENV={KALSHI_ENV}) pero las "
                    f"credenciales devolvieron HTTP {status}: solo datos públicos"
                )
                self.resolved = True
                return self.env

        self.apply("demo" if is_auto else KALSHI_ENV)

        if probed_any and not conclusive:
            self.detection_note = (
                "no se pudo verificar el entorno (error de red); se reintentará en el próximo ciclo"
            )
            return self.env

        self.resolved = True
        self.auth_verified = False
        if probed_any:
            self.detection_note = (
                f"credenciales no válidas en ningún entorno; datos públicos en {self.env}"
            )
        else:
            self.detection_note = f"sin credenciales: usando {self.env} para datos públicos"
        return self.env

    def switch(self, env: str) -> Dict[str, Any]:
        """Cambiar de entorno en runtime validando las credenciales del destino.

        No lanza si las credenciales fallan: deja `auth_verified=False` para que
        el llamador decida (los datos públicos siguen funcionando sin auth).
        """
        target = (env or "").strip().lower()
        if target == "prod":
            target = "production"
        if target not in VALID_ENVIRONMENTS and target != "auto":
            raise ValueError(f"entorno inválido: {env!r} (usa 'demo', 'production' o 'auto')")

        if target == "auto":
            self.resolved = False
            self.auth_verified = None
            self.probe_results = {}
            return self.resolve()

        probe_auth = self.auth_for(target)
        status: Optional[int] = None
        detail = "sin credenciales configuradas"
        if getattr(probe_auth, "configured", False):
            status, detail = self._probe(probe_auth, target)
        creds = kalshi_credentials(target)
        self.probe_results[target] = {
            "status": status,
            "detail": detail,
            "credentials_source": kalshi_credentials_source(target),
            "key_id": mask_key_id(creds.get("key_id", "")),
        }

        self.apply(target)
        self.resolved = True
        self.auth_verified = (status == 200)
        if status == 200:
            self.detection_note = f"cambio manual a {target}: credenciales válidas"
        elif status is None:
            self.detection_note = f"cambio manual a {target}: sin credenciales (solo datos públicos)"
        else:
            self.detection_note = f"cambio manual a {target}: HTTP {status} (solo datos públicos)"
        return self.status()

    def credentials_report(self) -> Dict[str, Any]:
        """Diagnóstico de credenciales por entorno, sin exponer secretos."""
        report: Dict[str, Any] = {"requested": self.requested, "active": self.env}
        for env in VALID_ENVIRONMENTS:
            creds = kalshi_credentials(env)
            report[env] = {
                "credentials_source": kalshi_credentials_source(env),
                "key_id": mask_key_id(creds.get("key_id", "")),
                "has_private_key": bool(
                    creds.get("private_key_path") or creds.get("private_key_pem")
                ),
                "private_key_path": creds.get("private_key_path", ""),
                "probe": self.probe_results.get(env),
                "rest_base": KALSHI_REST_BASES[env],
                "ws_base": KALSHI_WS_BASES[env],
            }
        return report

    def status(self) -> dict:
        return {
            "requested": self.requested,
            "environment": self.env,
            "rest_base": self.rest_base,
            "ws_base": self.ws_base,
            "resolved": self.resolved,
            "auth_verified": self.auth_verified,
            "detection_note": self.detection_note,
            "probe_path": KALSHI_ENV_PROBE_PATH,
            "probe_results": self.probe_results,
        }


kalshi_env = KalshiEnvironment()
