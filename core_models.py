"""Modelos base para estrategias modulares de Polymarket.

Estos dataclasses son una capa ligera y estable para que las estrategias
externas puedan trabajar sin depender directamente de ``MarketSnapshot``.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Market:
    condition_id: str
    question: str
    category: str = "general"
    tokens: List[Dict[str, Any]] = field(default_factory=list)
    volume: float = 0.0
    volume_24h: float = 0.0
    liquidity: float = 0.0
    active: bool = True
    end_date_iso: Optional[str] = None
    description: str = ""
    resolution_source: str = ""

    # Contexto de evento Kalshi (necesario para estrategias de canasta y escalera).
    event_id: str = ""
    mutually_exclusive: bool = False
    strike_type: str = ""
    close_time_iso: Optional[str] = None
    settlement_sources: List[Any] = field(default_factory=list)
    event_siblings: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class Opportunity:
    market_id: str
    question: str
    market_price: float
    category: str = "general"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Signal:
    market_id: str
    token_id: str
    side: str              # "buy" o "sell"
    estimated_prob: float  # Probabilidad estimada por el modelo (0.0 a 1.0)
    market_price: float    # Precio actual en Polymarket (0.0 a 1.0)
    confidence: float      # Nivel de confianza (0.0 a 1.0)
    strategy_name: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def edge(self) -> float:
        """Diferencia entre la probabilidad estimada y el precio del mercado."""
        return self.estimated_prob - self.market_price


class BaseStrategy:
    name: str = "base"
    tier: str = "C"
    strategy_id: int = 0
    required_data: List[str] = []

    def scan(self, markets: List[Market]) -> List[Opportunity]:
        raise NotImplementedError

    def analyze(self, opportunity: Opportunity, **kwargs: Any) -> Optional[Signal]:
        raise NotImplementedError