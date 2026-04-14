"""
portfolio_cloner.py — Sistema de Clonación Whale Scoop
Replica posiciones de alta convicción de superinversores (Buffett, Dalio, Lynch...)
"""

from __future__ import annotations
import logging
from datetime import date, datetime
from typing import List, Dict, Optional, Tuple

import httpx

from backend.core.models import Form13FHolding, ClonedPortfolioEntry, VWAPAnalysis
from backend.core.constants import ActorCategory

logger = logging.getLogger("whale.cloning")

# ─── GURÚS PRECONFIGURADOS ────────────────────────────────────────────────────

GURU_REGISTRY: Dict[str, Dict] = {
    "berkshire_hathaway": {
        "name":      "Warren Buffett — Berkshire Hathaway",
        "cik":       "0001067983",
        "aum_est":   350_000_000_000,
        "style":     "value_long_term",
        "category":  ActorCategory.BALLENA_JOROBADA,
        "rotation":  "low",     # Baja rotación = alta convicción
        "whale_score": 98,
    },
    "bridgewater": {
        "name":      "Ray Dalio — Bridgewater Associates",
        "cik":       "0001350694",
        "aum_est":   124_000_000_000,
        "style":     "all_weather_macro",
        "category":  ActorCategory.BALLENA_JOROBADA,
        "rotation":  "medium",
        "whale_score": 94,
    },
    "appaloosa": {
        "name":      "David Tepper — Appaloosa Management",
        "cik":       "0000813919",
        "aum_est":   14_000_000_000,
        "style":     "distressed_macro",
        "category":  ActorCategory.BALLENA,
        "rotation":  "high",
        "whale_score": 89,
    },
    "soros_fund": {
        "name":      "George Soros — Soros Fund Management",
        "cik":       "0000884096",
        "aum_est":   25_000_000_000,
        "style":     "global_macro",
        "category":  ActorCategory.BALLENA_JOROBADA,
        "rotation":  "high",
        "whale_score": 91,
    },
    "pershing_square": {
        "name":      "Bill Ackman — Pershing Square",
        "cik":       "0001336528",
        "aum_est":   10_000_000_000,
        "style":     "activist_concentrated",
        "category":  ActorCategory.BALLENA,
        "rotation":  "very_low",
        "whale_score": 87,
    },
    "third_point": {
        "name":      "Dan Loeb — Third Point LLC",
        "cik":       "0001040570",
        "aum_est":   8_000_000_000,
        "style":     "activist_value",
        "category":  ActorCategory.BALLENA,
        "rotation":  "medium",
        "whale_score": 85,
    },
}


class PortfolioCloner:
    """
    Implementa el método "Whale Scoop":
    1. Filtrar gestores de alta concentración y baja rotación
    2. Detectar convergencia (múltiples ballenas en mismo activo)
    3. Validar con VWAP + Volumen para evitar distribución
    """

    def __init__(self, form13f_holdings: List[Form13FHolding]):
        self._holdings = form13f_holdings
        self._by_manager: Dict[str, List[Form13FHolding]] = {}
        self._by_ticker:  Dict[str, List[Form13FHolding]] = {}
        self._index()

    def _index(self):
        """Construye índices por manager y por ticker."""
        for h in self._holdings:
            self._by_manager.setdefault(h.manager_cik, []).append(h)
            self._by_ticker.setdefault(h.ticker, []).append(h)

    # ── PASO 1: FILTRAR GESTORES DE ALTA CONVICCIÓN ────────────────────────

    def filter_high_conviction_managers(
        self,
        min_concentration_pct: float = 0.05,   # Posición > 5% del fondo
        max_rotation_style: str = "medium",     # "very_low", "low", "medium"
        min_whale_score: int = 80,
    ) -> List[str]:
        """
        Retorna CIKs de gestores que cumplen los criterios:
        - Alta concentración (posiciones > 5% del fondo)
        - Baja rotación
        - WhaleScore alto (proxy: registro interno)
        """
        eligible_ciks: List[str] = []
        rotation_map = {"very_low": 1, "low": 2, "medium": 3, "high": 4}
        max_rot_val  = rotation_map.get(max_rotation_style, 3)

        for guru_id, profile in GURU_REGISTRY.items():
            cik = profile["cik"]
            rot = rotation_map.get(profile.get("rotation", "high"), 4)

            if rot > max_rot_val:
                continue
            if profile.get("whale_score", 0) < min_whale_score:
                continue

            # Verificar que hay posiciones con >5% en cartera
            manager_holdings = self._by_manager.get(cik, [])
            high_conc = [
                h for h in manager_holdings
                if h.pct_portfolio >= min_concentration_pct
            ]
            if high_conc:
                eligible_ciks.append(cik)

        logger.info(f"PortfolioCloner: {len(eligible_ciks)} gestores de alta convicción")
        return eligible_ciks

    # ── PASO 2: DETECCIÓN DE CONVERGENCIA ─────────────────────────────────

    def detect_convergence(
        self,
        eligible_ciks: List[str],
        min_managers: int = 2,
    ) -> List[Tuple[str, List[str], float]]:
        """
        Detecta activos donde múltiples gestores coinciden.
        Retorna: [(ticker, [manager_names], convergence_score), ...]
        """
        convergent: List[Tuple[str, List[str], float]] = []

        for ticker, holdings in self._by_ticker.items():
            relevant = [h for h in holdings if h.manager_cik in eligible_ciks]

            if len(relevant) < min_managers:
                continue

            manager_names = [
                next(
                    (p["name"] for p in GURU_REGISTRY.values() if p["cik"] == h.manager_cik),
                    h.manager_name
                )
                for h in relevant
            ]

            # Score de convergencia: combina N gestores + pct_portfolio promedio
            n_managers = len(relevant)
            avg_pct    = sum(h.pct_portfolio for h in relevant) / n_managers
            conv_score = min((n_managers / 5.0) * 0.6 + (avg_pct / 0.10) * 0.4, 1.0)

            convergent.append((ticker, manager_names, round(conv_score, 3)))

        # Ordenar por convergencia descendente
        convergent.sort(key=lambda x: x[2], reverse=True)
        logger.info(f"Convergencia detectada en {len(convergent)} activos")
        return convergent

    # ── PASO 3: VALIDACIÓN CON VWAP ───────────────────────────────────────

    def validate_with_vwap(
        self,
        convergent_tickers: List[Tuple[str, List[str], float]],
        vwap_analyses: Dict[str, VWAPAnalysis],
        current_prices: Dict[str, float],
    ) -> List[ClonedPortfolioEntry]:
        """
        Filtra activos donde la entrada está validada por VWAP:
        Descarta activos en fase de distribución o Window Dressing.
        """
        results: List[ClonedPortfolioEntry] = []

        for ticker, managers, conv_score in convergent_tickers:
            vwap = vwap_analyses.get(ticker)
            current_price = current_prices.get(ticker, 0.0)

            if current_price == 0:
                continue

            # Descartar si el precio está bajo VWAP con volumen elevado
            # (señal de distribución activa)
            if vwap and not vwap.is_accumulation and vwap.price_vs_vwap < 0:
                logger.debug(f"Descartando {ticker}: distribución activa por VWAP")
                continue

            holdings = [
                h for h in self._by_ticker.get(ticker, [])
                if h.manager_cik in [p["cik"] for p in GURU_REGISTRY.values()]
            ]

            avg_entry = sum(h.value_usd / max(h.shares, 1) for h in holdings) / max(len(holdings), 1)
            pnl = ((current_price - avg_entry) / avg_entry) if avg_entry > 0 else 0.0

            max_pct = max((h.pct_portfolio for h in holdings), default=0.0)
            whale_score = max(
                (p.get("whale_score", 0) for p in GURU_REGISTRY.values()
                 if any(h.manager_cik == p["cik"] for h in holdings)),
                default=None
            )

            entry = ClonedPortfolioEntry(
                guru_name=" + ".join(managers[:3]),
                ticker=ticker,
                weight_in_guru=round(max_pct, 4),
                entry_price=round(avg_entry, 2),
                current_price=current_price,
                pnl_pct=round(pnl * 100, 2),
                n_gurus_holding=len(managers),
                convergence_score=conv_score,
                vwap_validated=bool(vwap and vwap.is_accumulation),
                whale_score=whale_score,
            )
            results.append(entry)

        results.sort(key=lambda e: e.convergence_score, reverse=True)
        return results

    # ── MOTOR COMPLETO (Whale Scoop Pipeline) ─────────────────────────────

    def run_whale_scoop(
        self,
        vwap_analyses: Dict[str, VWAPAnalysis],
        current_prices: Dict[str, float],
        min_whale_score: int = 85,
    ) -> List[ClonedPortfolioEntry]:
        """Ejecuta el pipeline completo de clonación Whale Scoop."""
        logger.info("🐳 Iniciando Whale Scoop pipeline...")

        # Paso 1: Filtrar gestores
        eligible = self.filter_high_conviction_managers(
            min_whale_score=min_whale_score
        )

        # Paso 2: Detectar convergencia
        convergent = self.detect_convergence(eligible, min_managers=2)

        # Paso 3: Validar con VWAP
        portfolio = self.validate_with_vwap(convergent, vwap_analyses, current_prices)

        logger.info(f"Whale Scoop completado: {len(portfolio)} posiciones clonadas")
        return portfolio


class DerivativeAnalyzer:
    """
    Analiza posiciones en derivados para distinguir:
    - Hedging: Compra de PUTs sobre posición larga existente (NO bajista)
    - Apuesta Direccional: PUTs sin subyacente previo (bajista)
    """

    def classify_options_activity(
        self,
        ticker: str,
        options_positions: List[Dict],
        equity_positions: List[Form13FHolding],
    ) -> Dict:
        """
        Clasifica la actividad en opciones.
        options_positions: [{"type": "put"|"call", "contracts": N, "strike": f, ...}]
        """
        equity_exposure_usd = sum(
            h.value_usd for h in equity_positions if h.ticker == ticker
        )

        put_positions  = [o for o in options_positions if o.get("type") == "put"]
        call_positions = [o for o in options_positions if o.get("type") == "call"]

        put_notional = sum(
            o.get("contracts", 0) * o.get("strike", 0) * 100
            for o in put_positions
        )

        # Determinar si los PUTs son cobertura o dirección
        if equity_exposure_usd > 0 and put_notional > 0:
            hedge_ratio = put_notional / equity_exposure_usd
            if 0.1 <= hedge_ratio <= 1.0:
                classification = "HEDGING"
                description = (
                    f"Cobertura: {put_notional:,.0f} USD en PUTs protege posición larga "
                    f"de {equity_exposure_usd:,.0f} USD (hedge ratio: {hedge_ratio:.1%})"
                )
            else:
                classification = "DIRECTIONAL_BET"
                description = f"Apuesta bajista: exceso de PUTs sobre exposición larga."
        elif put_notional > 0 and equity_exposure_usd == 0:
            classification = "DIRECTIONAL_BET"
            description = f"Apuesta bajista pura: PUTs sin posición larga en {ticker}."
        else:
            classification = "LONG_ONLY"
            description = f"Solo posición larga, sin estructura de opciones significativa."

        return {
            "ticker":                ticker,
            "classification":        classification,
            "description":           description,
            "equity_exposure_usd":   equity_exposure_usd,
            "put_notional_usd":      put_notional,
            "call_notional_usd": sum(
                o.get("contracts", 0) * o.get("strike", 0) * 100
                for o in call_positions
            ),
            "is_bearish_signal":     classification == "DIRECTIONAL_BET",
        }
