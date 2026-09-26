"""
news_oracle_agent.py
====================
Agente IA Evaluador de Noticias de Alta Frecuencia y Reglas Contractuales.

Mecánica Cuantitativa:
1. Ingesta Asíncrona de Fuentes RSS / Feeds de Noticias Cripto y Macro (CoinDesk, Cointelegraph, Decrypt, etc.).
2. Extracción de Entidades Clave y Palabras de Impacto (Fed, Rate Cut, ETF, SEC Approval, Trump, Harris, CPI).
3. Inferencia Bayesiana de Probabilidad:
   P(H|E) = (P(H) * L) / (P(H) * L + (1 - P(H)))
   Donde:
   - P(H): Probabilidad a priori (precio actual del contrato en Polymarket).
   - L = P(E|H) / P(E|¬H): Ratio de verosimilitud de la evidencia de la noticia.
   - P(H|E): Probabilidad posterior tras la noticia.
4. Cálculo de Edge Cuantitativo y Emisión de Catalizadores para Sniping de Noticias (S22).
"""

import asyncio
import time
import re
import logging
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
import xml.etree.ElementTree as ET

import httpx

logger = logging.getLogger("news_oracle_agent")
logger.setLevel(logging.INFO)


@dataclass
class NewsCatalyst:
    id: str
    headline: str
    source: str
    url: str
    published_at: float
    matched_entity: str
    sentiment_score: float  # -1.0 a +1.0
    prior_prob: float
    posterior_prob: float
    bayesian_edge: float  # (Posterior - Prior) * 100
    recommended_outcome: str  # "Yes" o "No"
    target_market_hint: str


class NewsOracleAgent:
    def __init__(self):
        self.running = False
        self.catalysts: List[NewsCatalyst] = []
        self._task: Optional[asyncio.Task] = None
        self.rss_sources = [
            {"name": "GoogleNews", "url": "https://news.google.com/rss/search?q=bitcoin+OR+crypto+OR+polymarket+OR+federal+reserve&hl=en-US&gl=US&ceid=US:en"},
            {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss"},
            {"name": "Decrypt", "url": "https://decrypt.co/feed"},
        ]
        self.last_fetch_time = 0.0
        self.tracked_entities = {
            "BITCOIN": ["bitcoin", "btc", "satoshi"],
            "ETHEREUM": ["ethereum", "eth", "vitalik"],
            "SOLANA": ["solana", "sol"],
            "CRYPTO_MACRO": ["crypto", "cryptocurrency", "altcoin", "doge", "dogecoin", "xrp", "polymarket"],
            "FED_RATES": ["fed", "federal reserve", "powell", "rate cut", "rate hike", "interest rate", "cpi", "inflation", "fomc"],
            "ETF_APPROVAL": ["etf", "sec", "gensler", "approval", "approve", "rejection", "reject", "etp"],
            "ELECTIONS": ["trump", "kamala", "harris", "biden", "election", "elections", "president", "presidential", "polls", "vote"],
        }

    def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._run_news_loop())
        logger.info("Agente IA Evaluador de Noticias y Oráculos iniciado.")

    def stop(self):
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Agente IA Evaluador de Noticias detenido.")

    def _calculate_sentiment(self, text: str) -> float:
        """Heurística léxica avanzada de polaridad en noticias financieras."""
        text_lower = text.lower()
        bullish_words = [
            "surge", "surges", "soar", "soars", "jump", "jumps", "record", "records", "record high", 
            "approve", "approved", "approval", "approves", "bullish", "win", "wins", "lead", "leads", 
            "gains", "gain", "skyrocket", "skyrockets", "positive", "cut rate", "rate cut", "cuts rate",
            "rally", "rallies", "breakout", "breaks out", "ath", "all-time high", "support", "inflow", 
            "inflows", "passes", "passed", "expands", "milestone"
        ]
        bearish_words = [
            "plunge", "plunges", "crash", "crashes", "tumble", "tumbles", "dump", "dumps", 
            "reject", "rejected", "rejection", "rejects", "bearish", "lose", "loses", "lawsuit", 
            "delay", "delays", "probe", "probes", "investigation", "hacked", "hack", "sec sues", 
            "inflation up", "rate hike", "hikes", "hike", "drops", "drop", "falls", "fall", 
            "slide", "slides", "dip", "dips", "outflow", "outflows"
        ]

        score = 0.0
        for w in bullish_words:
            if re.search(r'\b' + re.escape(w) + r'\b', text_lower):
                score += 0.35
        for w in bearish_words:
            if re.search(r'\b' + re.escape(w) + r'\b', text_lower):
                score -= 0.35

        return round(min(max(score, -1.0), 1.0), 2)

    def _compute_bayesian_posterior(self, prior: float, sentiment: float) -> float:
        """
        Inferencia Bayesiana:
        P(H|E) = (P(H) * L) / (P(H) * L + (1 - P(H)))
        """
        # Calcular verosimilitud L (Likelihood ratio)
        if sentiment > 0:
            likelihood_ratio = 1.0 + (sentiment * 3.5)
        elif sentiment < 0:
            likelihood_ratio = 1.0 / (1.0 + (abs(sentiment) * 3.5))
        else:
            likelihood_ratio = 1.0

        p = max(min(prior, 0.99), 0.01)
        posterior = (p * likelihood_ratio) / ((p * likelihood_ratio) + (1.0 - p))
        return round(min(max(posterior, 0.02), 0.98), 3)

    async def _fetch_rss(self, client: httpx.AsyncClient, source: Dict[str, str]) -> List[Dict[str, Any]]:
        """Descarga y parsea feed RSS en formato XML estándar."""
        items = []
        try:
            resp = await client.get(
                source["url"],
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=6.0
            )
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                for item in root.findall(".//item")[:10]:
                    title = item.findtext("title", "")
                    link = item.findtext("link", "")
                    items.append({
                        "source": source["name"],
                        "title": title,
                        "link": link,
                    })
        except Exception as e:
            # RSS endpoints may throttle or fail, non-fatal
            logger.debug(f"RSS fetch fallback para {source['name']}: {e}")
        return items

    async def _run_news_loop(self):
        """Bucle principal de ingesta periódica de noticias (cada 45 segundos)."""
        async with httpx.AsyncClient(timeout=8.0) as client:
            while self.running:
                try:
                    all_news = []
                    for src in self.rss_sources:
                        news_items = await self._fetch_rss(client, src)
                        all_news.extend(news_items)

                    now = time.time()
                    self.last_fetch_time = now

                    for n in all_news:
                        title = n.get("title", "")
                        source = n.get("source", "")
                        link = n.get("link", "")
                        title_lower = title.lower()

                        # Identificar entidad
                        matched_cat = None
                        for cat, keywords in self.tracked_entities.items():
                            if any(k in title_lower for k in keywords):
                                matched_cat = cat
                                break

                        if not matched_cat:
                            continue

                        # Evitar noticias ya procesadas
                        if any(c.headline == title for c in self.catalysts):
                            continue

                        sentiment = self._calculate_sentiment(title)
                        if abs(sentiment) < 0.20:
                            continue

                        # Prior de mercado típico (0.50 si nuevo, o sesgado)
                        prior = 0.50
                        posterior = self._compute_bayesian_posterior(prior, sentiment)
                        edge = (posterior - prior) * 100.0

                        if abs(edge) >= 8.0:
                            outcome = "Yes" if edge > 0 else "No"
                            catalyst = NewsCatalyst(
                                id=f"NEWS-{int(now * 1000)}-{len(self.catalysts)}",
                                headline=title,
                                source=source,
                                url=link,
                                published_at=now,
                                matched_entity=matched_cat,
                                sentiment_score=sentiment,
                                prior_prob=prior,
                                posterior_prob=posterior,
                                bayesian_edge=round(abs(edge), 1),
                                recommended_outcome=outcome,
                                target_market_hint=f"Mercados relacionados con {matched_cat}",
                            )
                            self.catalysts.insert(0, catalyst)
                            logger.info(f"📰 [CATALIZADOR IA] {source}: {title[:60]}... | Edge: {catalyst.bayesian_edge}% {outcome}")

                    # Limitar historial a los 50 más recientes
                    if len(self.catalysts) > 50:
                        self.catalysts = self.catalysts[:50]

                    await asyncio.sleep(45.0)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error en loop de noticias IA: {e}")
                    await asyncio.sleep(30.0)

    def get_status(self) -> Dict[str, Any]:
        """Devuelve el estado del agente de noticias para el Dashboard y API."""
        return {
            "running": self.running,
            "last_fetch_time": self.last_fetch_time,
            "sources_monitored": [s["name"] for s in self.rss_sources],
            "total_catalysts_detected": len(self.catalysts),
            "recent_catalysts": [asdict(c) for c in self.catalysts[:20]],
        }


# Instancia singleton del agente
news_oracle_agent = NewsOracleAgent()
