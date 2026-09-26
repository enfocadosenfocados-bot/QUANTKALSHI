"""Autenticación Kalshi para REST y WebSocket.

Kalshi usa claves asimétricas: `KALSHI-ACCESS-KEY`, timestamp en ms y una
firma base64 sobre `timestamp + method + path_sin_query`.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlsplit

from config import KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_PRIVATE_KEY_PEM

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except Exception:  # pragma: no cover - se reporta en runtime si falta la dependencia
    hashes = serialization = padding = Ed25519PrivateKey = None  # type: ignore


class KalshiAuth:
    def __init__(self, key_id: str = KALSHI_KEY_ID, private_key_path: str = KALSHI_PRIVATE_KEY_PATH, private_key_pem: str = KALSHI_PRIVATE_KEY_PEM):
        self.key_id = (key_id or "").strip()
        self.private_key_path = (private_key_path or "").strip()
        self.private_key_pem = (private_key_pem or "").strip()
        self._private_key = None
        self._load_error: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self.key_id and (self.private_key_path or self.private_key_pem))

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    def _load_private_key(self):
        if self._private_key is not None:
            return self._private_key
        if not self.configured:
            self._load_error = "KALSHI_KEY_ID y KALSHI_PRIVATE_KEY_PATH/KALSHI_PRIVATE_KEY_PEM no están configurados"
            return None
        if serialization is None:
            self._load_error = "Falta instalar cryptography para firmar requests de Kalshi"
            return None
        try:
            if self.private_key_pem:
                key_bytes = self.private_key_pem.replace("\\n", "\n").encode("utf-8")
            else:
                key_path = Path(self.private_key_path).expanduser()
                key_bytes = key_path.read_bytes()
            self._private_key = serialization.load_pem_private_key(key_bytes, password=None)
            self._load_error = None
            return self._private_key
        except Exception as exc:
            self._load_error = str(exc)
            return None

    @staticmethod
    def unsigned_path(path_or_url: str) -> str:
        """Devuelve el path absoluto sin query, tal como lo exige Kalshi."""
        if path_or_url.startswith("http://") or path_or_url.startswith("https://") or path_or_url.startswith("wss://"):
            parsed = urlsplit(path_or_url)
            return parsed.path
        return path_or_url.split("?", 1)[0]

    def sign_text(self, text: str) -> Optional[str]:
        private_key = self._load_private_key()
        if private_key is None:
            return None
        message = text.encode("utf-8")
        try:
            if Ed25519PrivateKey is not None and isinstance(private_key, Ed25519PrivateKey):
                signature = private_key.sign(message)
            else:
                signature = private_key.sign(
                    message,
                    padding.PSS(
                        mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH,
                    ),
                    hashes.SHA256(),
                )
            return base64.b64encode(signature).decode("utf-8")
        except Exception as exc:
            self._load_error = str(exc)
            return None

    def headers(self, method: str, path_or_url: str) -> Dict[str, str]:
        if not self.configured:
            return {}
        timestamp = str(int(time.time() * 1000))
        method_up = method.upper()
        unsigned = self.unsigned_path(path_or_url)
        signature = self.sign_text(timestamp + method_up + unsigned)
        if not signature:
            return {}
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }
