"""
signal_engine.py — Motor de Detección de Señales de Alta Convicción
Módulos: Cluster Buy, I-Ratio, Industry Window Dressing, Conviction Scorer
"""

from __future__ import annotations
import uuid
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional, Tuple

import numpy as np

from backend.core.models import (
    InsiderTransaction, OnChainTransaction,
    ClusterBuy, MarketSignal, VWAPAnalysis
)
from backend.core.constants import (
    CLUSTER_CONFIG, BLACKOUT_MONTHS, BLACKOUT_SENSITIVITY_FACTOR,
    CONVICTION_WEIGHTS, SignalType, SignalStrength
)
from backend.core.models import TransactionCode

logger = logging.getLogger("whale.signals")


# ─── MÓDULO: CLUSTER BUY ─────────────────────────────────────────────────────

class ClusterBuyDetector:
    """
    Implementa la puerta lógica de "Compra Agrupada":
    N ≥ 3 participantes, ventana ≤ 7 días, mismo ticker.
    Solo transacciones con código "P" (open market).
    """

    def detect(
        self,
        transactions: List[InsiderTransaction],
        ref_date: Optional[date] = None,
    ) -> List[ClusterBuy]:
        """Detecta clusters de compra en el conjunto de transacciones."""
        ref_date = ref_date or date.today()
        min_n    = CLUSTER_CONFIG["min_participants"]
        window   = CLUSTER_CONFIG["time_window_days"]
        valid_codes = CLUSTER_CONFIG["valid_transaction_codes"]

        # ── Filtrar solo compras Open Market (código P) y no 10b5-1
        eligible = [
            t for t in transactions
            if t.transaction_code.value in valid_codes
            and not t.is_rule_10b51
        ]

        # ── Agrupar por ticker
        by_ticker: Dict[str, List[InsiderTransaction]] = defaultdict(list)
        for t in eligible:
            by_ticker[t.ticker].append(t)

        clusters: List[ClusterBuy] = []

        for ticker, txns in by_ticker.items():
            # Ordenar por fecha
            txns_sorted = sorted(txns, key=lambda x: x.transaction_date)

            # Ventana deslizante
            for i, anchor in enumerate(txns_sorted):
                window_end = anchor.transaction_date + timedelta(days=window)
                window_txns = [
                    t for t in txns_sorted[i:]
                    if t.transaction_date <= window_end
                ]

                # Participantes únicos (no contar mismo insider dos veces)
                participants = list({t.insider_name for t in window_txns})

                if len(participants) < min_n:
                    continue

                # Evitar duplicados de cluster (mismo ticker, mismo inicio)
                existing_ids = {
                    c.window_start.isoformat() + c.ticker
                    for c in clusters
                }
                key = anchor.transaction_date.isoformat() + ticker
                if key in existing_ids:
                    continue

                total_value = sum(t.value_usd for t in window_txns)
                avg_price   = sum(t.price_per_share * t.shares for t in window_txns) / \
                              sum(t.shares for t in window_txns)

                conviction = self._compute_cluster_conviction(
                    window_txns, ticker, ref_date
                )

                cluster = ClusterBuy(
                    cluster_id=str(uuid.uuid4())[:12],
                    ticker=ticker,
                    detection_date=ref_date,
                    window_start=anchor.transaction_date,
                    window_end=window_end,
                    n_participants=len(participants),
                    participants=participants,
                    total_value_usd=total_value,
                    avg_price=avg_price,
                    transactions=[t.filing_id for t in window_txns],
                    conviction_score=conviction,
                )
                clusters.append(cluster)

        logger.info(f"ClusterBuyDetector: {len(clusters)} clusters detectados")
        return clusters

    def _compute_cluster_conviction(
        self,
        txns: List[InsiderTransaction],
        ticker: str,
        ref_date: date,
    ) -> float:
        """Calcula el score de convicción del cluster (0.0 → 1.0)."""
        score = 0.0

        # Base: número de participantes (más participantes = más convicción)
        n = len({t.insider_name for t in txns})
        score += min(n / 10.0, 0.4)  # Máx 0.4 por N participantes

        # Conviction Ratios individuales (si están disponibles)
        ratios = [t.conviction_ratio for t in txns if t.conviction_ratio is not None]
        if ratios:
            avg_ratio = np.mean(ratios)
            score += min(avg_ratio / 20.0, 0.3)  # Máx 0.3 por ratio promedio

        # Tamaño total de la compra
        total_val = sum(t.value_usd for t in txns)
        if total_val > 50_000_000:
            score += 0.2
        elif total_val > 10_000_000:
            score += 0.1

        # Ajuste estacional (blackout periods)
        if ref_date.month in BLACKOUT_MONTHS:
            score *= BLACKOUT_SENSITIVITY_FACTOR

        return min(score, 1.0)


# ─── MÓDULO: I-RATIO ─────────────────────────────────────────────────────────

class IratioCalculator:
    """
    Indicador I-Ratio: Insider Buy/Sell Ratio con Normalización Estacional.
    Ajusta la sensibilidad en meses de blackout tras earnings.
    """

    def compute(
        self,
        transactions: List[InsiderTransaction],
        lookback_days: int = 30,
        ref_date: Optional[date] = None,
    ) -> Dict[str, float]:
        ref_date = ref_date or date.today()
        cutoff   = ref_date - timedelta(days=lookback_days)

        recent = [
            t for t in transactions
            if t.transaction_date >= cutoff
        ]

        buy_value  = sum(
            t.value_usd for t in recent
            if t.transaction_code == TransactionCode.P and not t.is_rule_10b51
        )
        sell_value = sum(
            t.value_usd for t in recent
            if t.transaction_code == TransactionCode.S
        )

        iratio_raw = buy_value / max(sell_value, 1)

        # Ajuste estacional
        is_blackout = ref_date.month in BLACKOUT_MONTHS
        iratio_adj  = iratio_raw * (BLACKOUT_SENSITIVITY_FACTOR if is_blackout else 1.0)

        return {
            "iratio_raw":          round(iratio_raw, 3),
            "iratio_seasonal_adj": round(iratio_adj, 3),
            "buy_value_usd":       buy_value,
            "sell_value_usd":      sell_value,
            "n_buys":              sum(1 for t in recent if t.transaction_code == TransactionCode.P),
            "n_sells":             sum(1 for t in recent if t.transaction_code == TransactionCode.S),
            "is_blackout_month":   is_blackout,
            "period_days":         lookback_days,
        }


# ─── MÓDULO: WINDOW DRESSING ─────────────────────────────────────────────────

class WindowDressingDetector:
    """
    Detecta "Industry Window Dressing" basado en métricas de la LSE:
    - Migración de sector SIC/NAICS justo sobre el umbral del 50% de ventas
    - Caída de márgenes + crecimiento de inventario > 2SD
    - Cruce con historial de restatements
    """

    def analyze(
        self,
        company_data: Dict,
        restatement_history: List[Dict],
    ) -> Optional[MarketSignal]:
        """Analiza si una empresa muestra señales de Window Dressing."""
        ticker   = company_data.get("ticker", "UNK")
        is_toxic = False
        evidence = []

        # Check 1: Migración de sector
        sector_migrated = company_data.get("sector_changed_recently", False)
        sales_pct_new   = company_data.get("new_segment_sales_pct", 0.0)

        if sector_migrated and 0.50 <= sales_pct_new <= 0.65:
            evidence.append(
                f"Migración de sector detectada: nuevo segmento = {sales_pct_new:.0%} de ventas"
            )

        # Check 2: Caída de márgenes del segmento líder
        margin_change = company_data.get("lead_segment_margin_change", 0.0)
        if margin_change < -0.05:
            evidence.append(
                f"Caída de margen segmento líder: {margin_change:.1%}"
            )

        # Check 3: Inventario
        inventory_zscore = company_data.get("inventory_zscore", 0.0)
        if inventory_zscore > 2.0:
            evidence.append(
                f"Crecimiento inventario > 2SD (z={inventory_zscore:.2f})"
            )

        if len(evidence) < 2:
            return None  # No hay suficientes evidencias

        # Check 4: Historial de restatements → señal TÓXICA
        if restatement_history:
            is_toxic = True
            evidence.append(
                f"⚠️ Historial de {len(restatement_history)} restatements previos"
            )

        strength = SignalStrength.CRITICAL if is_toxic else SignalStrength.STRONG

        return MarketSignal(
            signal_id=str(uuid.uuid4())[:12],
            signal_type=SignalType.WINDOW_DRESSING.value,
            strength=strength.value,
            ticker=ticker,
            description=f"Window Dressing detectado en {ticker}: "
                        f"{'SEÑAL TÓXICA/MANIPULATIVA' if is_toxic else 'Alerta de manipulación'}",
            evidence=evidence,
            conviction_score=0.85 if is_toxic else 0.60,
            is_toxic=is_toxic,
            tags=["window_dressing", "sector_migration", "toxic" if is_toxic else "warning"],
        )


# ─── MÓDULO: MURO DE LIQUIDEZ ─────────────────────────────────────────────────

class LiquidityWallDetector:
    """
    Identifica "Muros de Liquidez" (Inyecciones de Intencionalidad):
    Muros de venta masivos que inducen pánico minorista.
    """

    def analyze_orderbook(
        self,
        orderbook: Dict,
        ticker: str,
        current_price: float,
    ) -> Optional[MarketSignal]:
        """
        Analiza el libro de órdenes buscando muros anómalos.
        orderbook = {"bids": [[price, size], ...], "asks": [[price, size], ...]}
        """
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])

        if not bids or not asks:
            return None

        # Calcular volumen promedio por nivel
        avg_bid_size = np.mean([b[1] for b in bids])
        avg_ask_size = np.mean([a[1] for a in asks])

        # Detectar muros: órdenes > 5x el promedio
        bid_walls = [(p, s) for p, s in bids if s > avg_bid_size * 5]
        ask_walls = [(p, s) for p, s in asks if s > avg_ask_size * 5]

        signals = []

        for price, size in ask_walls:
            distance_pct = (price - current_price) / current_price
            if 0.02 <= distance_pct <= 0.10:   # Muro de venta a 2-10% de distancia
                signals.append(
                    f"Muro de VENTA: {size:,.0f} @ ${price:,.2f} "
                    f"({distance_pct:.1%} sobre precio actual) — "
                    f"= {size/avg_ask_size:.0f}x volumen promedio"
                )

        for price, size in bid_walls:
            distance_pct = (current_price - price) / current_price
            if 0.02 <= distance_pct <= 0.10:
                signals.append(
                    f"Muro de COMPRA: {size:,.0f} @ ${price:,.2f} "
                    f"({distance_pct:.1%} bajo precio actual)"
                )

        if not signals:
            return None

        return MarketSignal(
            signal_id=str(uuid.uuid4())[:12],
            signal_type=SignalType.WALL_INJECTION.value,
            strength=SignalStrength.STRONG.value,
            ticker=ticker,
            description=f"Inyección de Intencionalidad detectada en {ticker}: "
                        f"{len(ask_walls)} muro(s) de venta artificial(es)",
            evidence=signals,
            conviction_score=0.72,
            is_toxic=False,
            tags=["liquidity_wall", "manipulation", "institutional"],
        )


# ─── SCORER DE CONVICCIÓN MAESTRO ────────────────────────────────────────────

class ConvictionScorer:
    """
    Consolida todas las señales para producir un Conviction Score maestro (0-1).
    Weights: Form 4 (35%) + On-Chain (30%) + Cluster (20%) + VWAP (10%) + DIX (5%)
    """

    def score(
        self,
        ticker: str,
        form4_signals: List[InsiderTransaction],
        onchain_signals: List[OnChainTransaction],
        clusters: List[ClusterBuy],
        vwap: Optional[VWAPAnalysis] = None,
        dix_value: Optional[float] = None,
    ) -> Tuple[float, Dict[str, float]]:
        """
        Retorna (conviction_score, score_breakdown).
        """
        breakdown: Dict[str, float] = {}
        total = 0.0

        # ── Form 4 Open Market (35%)
        relevant_f4 = [
            t for t in form4_signals
            if t.ticker == ticker
            and t.transaction_code == TransactionCode.P
            and not t.is_rule_10b51
        ]
        if relevant_f4:
            f4_score = min(len(relevant_f4) / 5.0, 1.0)
            weighted = f4_score * CONVICTION_WEIGHTS["form_4_open_market"]
            breakdown["form_4_open_market"] = weighted
            total += weighted

        # ── On-Chain Acumulación (30%)
        accum_flows = [
            t for t in onchain_signals
            if t.is_exchange_outflow
            and t.blockchain in {"bitcoin", "ethereum"}
        ]
        if accum_flows:
            oc_score = min(len(accum_flows) / 3.0, 1.0)
            weighted = oc_score * CONVICTION_WEIGHTS["onchain_accumulation"]
            breakdown["onchain_accumulation"] = weighted
            total += weighted

        # ── Cluster Buy (20%)
        ticker_clusters = [c for c in clusters if c.ticker == ticker]
        if ticker_clusters:
            clust_score = max(c.conviction_score for c in ticker_clusters)
            weighted    = clust_score * CONVICTION_WEIGHTS["cluster_buy"]
            breakdown["cluster_buy"] = weighted
            total += weighted

        # ── VWAP + Volumen (10%)
        if vwap and vwap.ticker == ticker and vwap.is_accumulation:
            weighted = CONVICTION_WEIGHTS["vwap_above_2sd_vol"]
            breakdown["vwap_accumulation"] = weighted
            total += weighted

        # ── DIX (5%)
        if dix_value and dix_value > 45:
            weighted = CONVICTION_WEIGHTS["dix_elevated"]
            breakdown["dix_elevated"] = weighted
            total += weighted

        return round(min(total, 1.0), 3), breakdown
