"""
polygon_health_checker.py
=========================
Módulo de Auditoría y Diagnóstico de Salud de Billetera Polygon (Chain ID 137).

Funciones:
1. Benchmark y selección automática del nodo RPC de Polygon con menor latencia
   (polygon-rpc.com, rpc.ankr.com/polygon, 1rpc.io/matic, polygon.llamarpc.com).
2. Verificación de saldo de Gas nativo (POL/MATIC) mediante eth_getBalance.
3. Verificación de saldo de USDC.e (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
   y USDC Nativo (0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359) mediante ABI balanceOf.
4. Verificación de Allowance (aprobación) para el contrato de intercambio de Polymarket (CTF Exchange).
5. Semáforo de preparación para Live Trading con recomendaciones accionables.
"""

import os
import json
import time
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict

import httpx

logger = logging.getLogger("polygon_health_checker")
logger.setLevel(logging.INFO)

# Contratos oficiales de Polymarket y tokens en Polygon Mainnet
POLYGON_CHAIN_ID = 137
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Bridged USDC (Polymarket default)
USDC_NATIVE_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # Native USDC
POLYMARKET_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # Main CTF Exchange

DEFAULT_RPCS = [
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
    "https://polygon.llamarpc.com",
]


@dataclass
class RpcBenchmark:
    rpc_url: str
    latency_ms: int
    is_alive: bool
    block_number: int = 0


@dataclass
class WalletHealthReport:
    wallet_address: str
    status: str  # "READY_FOR_LIVE", "LOW_GAS", "LOW_USDC", "MISSING_ALLOWANCE", "NOT_CONFIGURED"
    status_color: str  # "GREEN", "YELLOW", "RED", "GRAY"
    pol_gas_balance: float
    usdc_e_balance: float
    usdc_native_balance: float
    total_usdc_balance: float
    ctf_allowance_active: bool
    best_rpc_url: str
    best_rpc_latency_ms: int
    gas_sufficient_for_trades: bool
    capital_target_ok: bool  # Si se acerca a los $1,000 USD
    recommendations: List[str]
    timestamp: float


class PolygonHealthChecker:
    def __init__(self, rpc_urls: Optional[List[str]] = None):
        self.rpc_urls = rpc_urls or DEFAULT_RPCS
        self.best_rpc = self.rpc_urls[0]
        self.cached_report: Optional[WalletHealthReport] = None
        self.last_check_time = 0.0
        self.cache_ttl_sec = 15.0

    def _pad_address_for_abi(self, address: str) -> str:
        clean = address.lower().replace("0x", "")
        return clean.zfill(64)

    def _encode_balance_of(self, address: str) -> str:
        # 0x70a08231 = keccak256("balanceOf(address)")[:4]
        return "0x70a08231" + self._pad_address_for_abi(address)

    def _encode_allowance(self, owner: str, spender: str) -> str:
        # 0xdd62ed3e = keccak256("allowance(address,address)")[:4]
        return "0xdd62ed3e" + self._pad_address_for_abi(owner) + self._pad_address_for_abi(spender)

    def _resolve_wallet_address(self) -> str:
        """Busca la dirección pública en variables de entorno o archivos locales de config."""
        for env_key in ["POLYMARKET_WALLET_ADDRESS", "POLYGON_WALLET_ADDRESS", "ETH_WALLET_ADDRESS", "WALLET_ADDRESS"]:
            val = os.getenv(env_key)
            if val and val.startswith("0x") and len(val) == 42:
                return val

        # Intentar leer desde settings.json si existe
        if os.path.exists("settings.json"):
            try:
                with open("settings.json", "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k in ["wallet_address", "polygon_address", "address"]:
                        val = data.get(k)
                        if val and val.startswith("0x") and len(val) == 42:
                            return val
            except Exception:
                pass

        return ""

    async def benchmark_rpcs(self) -> List[RpcBenchmark]:
        """Prueba la latencia de todos los RPCs públicos y selecciona el más rápido."""
        benchmarks = []
        async with httpx.AsyncClient(timeout=3.5) as client:
            for rpc in self.rpc_urls:
                t0 = time.time()
                try:
                    payload = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
                    resp = await client.post(rpc, json=payload)
                    latency = int((time.time() - t0) * 1000)
                    if resp.status_code == 200:
                        block_hex = resp.json().get("result", "0x0")
                        block_num = int(block_hex, 16)
                        benchmarks.append(RpcBenchmark(rpc_url=rpc, latency_ms=latency, is_alive=True, block_number=block_num))
                    else:
                        benchmarks.append(RpcBenchmark(rpc_url=rpc, latency_ms=9999, is_alive=False))
                except Exception:
                    benchmarks.append(RpcBenchmark(rpc_url=rpc, latency_ms=9999, is_alive=False))

        benchmarks.sort(key=lambda b: b.latency_ms)
        alive = [b for b in benchmarks if b.is_alive]
        if alive:
            self.best_rpc = alive[0].rpc_url
        return benchmarks

    async def check_health(self, wallet_address: Optional[str] = None, force_refresh: bool = False) -> WalletHealthReport:
        """
        Ejecuta la auditoría on-chain completa de la billetera.
        """
        now = time.time()
        if not force_refresh and self.cached_report and (now - self.last_check_time) < self.cache_ttl_sec:
            return self.cached_report

        addr = wallet_address or self._resolve_wallet_address()
        recommendations = []

        # 1. Benchmark RPC
        benchmarks = await self.benchmark_rpcs()
        best_b = benchmarks[0] if benchmarks else RpcBenchmark(self.best_rpc, 999, False)

        if not addr:
            report = WalletHealthReport(
                wallet_address="0x0000000000000000000000000000000000000000",
                status="NOT_CONFIGURED",
                status_color="GRAY",
                pol_gas_balance=0.0,
                usdc_e_balance=0.0,
                usdc_native_balance=0.0,
                total_usdc_balance=0.0,
                ctf_allowance_active=False,
                best_rpc_url=best_b.rpc_url,
                best_rpc_latency_ms=best_b.latency_ms,
                gas_sufficient_for_trades=False,
                capital_target_ok=False,
                recommendations=[
                    "Billetera Polygon no detectada en .env o configuración.",
                    "Configura POLYMARKET_WALLET_ADDRESS con tu dirección pública 0x...",
                    f"El nodo RPC más rápido ({best_b.rpc_url}) responde en {best_b.latency_ms} ms.",
                ],
                timestamp=now,
            )
            self.cached_report = report
            self.last_check_time = now
            return report

        pol_balance = 0.0
        usdc_e = 0.0
        usdc_native = 0.0
        allowance_active = False

        async with httpx.AsyncClient(timeout=4.0) as client:
            # A. Saldo POL (eth_getBalance)
            try:
                p_gas = {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [addr, "latest"], "id": 1}
                r_gas = await client.post(self.best_rpc, json=p_gas)
                if r_gas.status_code == 200:
                    wei_val = int(r_gas.json().get("result", "0x0"), 16)
                    pol_balance = wei_val / 1e18
            except Exception as e:
                logger.debug(f"Error consultando balance POL: {e}")

            # B. Saldo USDC.e
            try:
                call_data = self._encode_balance_of(addr)
                p_usdc_e = {
                    "jsonrpc": "2.0",
                    "method": "eth_call",
                    "params": [{"to": USDC_E_ADDRESS, "data": call_data}, "latest"],
                    "id": 2,
                }
                r_usdc_e = await client.post(self.best_rpc, json=p_usdc_e)
                if r_usdc_e.status_code == 200:
                    raw_val = int(r_usdc_e.json().get("result", "0x0"), 16)
                    usdc_e = raw_val / 1e6
            except Exception as e:
                logger.debug(f"Error consultando balance USDC.e: {e}")

            # C. Saldo USDC Nativo
            try:
                call_data_nat = self._encode_balance_of(addr)
                p_usdc_nat = {
                    "jsonrpc": "2.0",
                    "method": "eth_call",
                    "params": [{"to": USDC_NATIVE_ADDRESS, "data": call_data_nat}, "latest"],
                    "id": 3,
                }
                r_nat = await client.post(self.best_rpc, json=p_usdc_nat)
                if r_nat.status_code == 200:
                    raw_val = int(r_nat.json().get("result", "0x0"), 16)
                    usdc_native = raw_val / 1e6
            except Exception as e:
                logger.debug(f"Error consultando balance USDC nativo: {e}")

            # D. Allowance CTF Exchange
            try:
                call_allow = self._encode_allowance(addr, POLYMARKET_CTF_EXCHANGE)
                p_allow = {
                    "jsonrpc": "2.0",
                    "method": "eth_call",
                    "params": [{"to": USDC_E_ADDRESS, "data": call_allow}, "latest"],
                    "id": 4,
                }
                r_allow = await client.post(self.best_rpc, json=p_allow)
                if r_allow.status_code == 200:
                    raw_val = int(r_allow.json().get("result", "0x0"), 16)
                    allowance_active = (raw_val / 1e6) >= 100.0
            except Exception as e:
                logger.debug(f"Error consultando allowance CTF: {e}")

        total_usdc = usdc_e + usdc_native
        gas_ok = pol_balance >= 0.20
        capital_ok = total_usdc >= 50.0

        if gas_ok and total_usdc >= 50.0 and allowance_active:
            status = "READY_FOR_LIVE"
            color = "GREEN"
            recommendations.append("✅ Billetera 100% lista para operar en Live Trading.")
        elif not gas_ok:
            status = "LOW_GAS"
            color = "YELLOW"
            recommendations.append(f"⚠️ Saldo POL insuficiente ({pol_balance:.3f} POL). Deposita al menos 0.5 POL para gas.")
        elif total_usdc < 25.0:
            status = "LOW_USDC"
            color = "YELLOW"
            recommendations.append(f"⚠️ Saldo USDC bajo (${total_usdc:.2f} USD). Para optimizar el Kelly de $1,000 USD, deposita fondos.")
        elif not allowance_active:
            status = "MISSING_ALLOWANCE"
            color = "YELLOW"
            recommendations.append("⚠️ Falta aprobar el contrato de Polymarket CTF Exchange para operar.")
        else:
            status = "STANDBY"
            color = "GREEN"

        report = WalletHealthReport(
            wallet_address=addr,
            status=status,
            status_color=color,
            pol_gas_balance=round(pol_balance, 4),
            usdc_e_balance=round(usdc_e, 2),
            usdc_native_balance=round(usdc_native, 2),
            total_usdc_balance=round(total_usdc, 2),
            ctf_allowance_active=allowance_active,
            best_rpc_url=best_b.rpc_url,
            best_rpc_latency_ms=best_b.latency_ms,
            gas_sufficient_for_trades=gas_ok,
            capital_target_ok=capital_ok,
            recommendations=recommendations,
            timestamp=now,
        )
        self.cached_report = report
        self.last_check_time = now
        return report

    def get_status(self) -> Dict[str, Any]:
        """Estado serializable del health check para el Dashboard."""
        if not self.cached_report:
            return {
                "status": "INITIALIZING",
                "status_color": "GRAY",
                "wallet_address": "",
                "best_rpc_url": self.best_rpc,
                "best_rpc_latency_ms": 0,
                "pol_gas_balance": 0.0,
                "total_usdc_balance": 0.0,
                "recommendations": ["Iniciando auditoría de red Polygon..."],
            }
        return asdict(self.cached_report)


# Instancia singleton del health checker de Polygon
polygon_health_checker = PolygonHealthChecker()
