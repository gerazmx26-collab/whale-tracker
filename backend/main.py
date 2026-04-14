"""
main.py — FastAPI Application: Sistema Unificado de Rastreo de Ballenas Institucionales
Endpoints: /dashboard, /signals, /clusters, /onchain, /cloning, /vwap/{ticker}
"""

from __future__ import annotations
import asyncio
import logging
import os
from datetime import date, datetime
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.core.models import (
    MarketSignal, ClusterBuy, OnChainTransaction,
    VWAPAnalysis, ClonedPortfolioEntry, SystemDashboard,
    InsiderTransaction, Form13FHolding
)
from backend.core.constants import SignalType
from backend.modules.cloning.portfolio_cloner import GURU_REGISTRY
from backend.modules.sec.sec_scraper import Form4Parser, Form13FParser
from backend.modules.onchain.onchain_monitor import WhaleAlertClient, NetFlowCalculator
from backend.modules.signals.signal_engine import (
    ClusterBuyDetector, IratioCalculator, ConvictionScorer,
    WindowDressingDetector, LiquidityWallDetector
)
from backend.modules.microstructure.vwap_analyzer import VWAPEngine, PolygonDataClient
from backend.modules.cloning.portfolio_cloner import PortfolioCloner, DerivativeAnalyzer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("whale.api")

# ─── ENV VARS ────────────────────────────────────────────────────────────────

WHALE_ALERT_KEY = os.getenv("WHALE_ALERT_API_KEY", "demo_mode")
POLYGON_KEY     = os.getenv("POLYGON_API_KEY",     "demo_mode")

# ─── ESTADO GLOBAL (cache en memoria) ────────────────────────────────────────

class AppState:
    form4_transactions: List[InsiderTransaction] = []
    form13f_holdings:   List[Form13FHolding]     = []
    onchain_txns:       List[OnChainTransaction] = []
    clusters:           List[ClusterBuy]          = []
    signals:            List[MarketSignal]        = []
    vwap_cache:         dict                      = {}
    last_refresh:       Optional[datetime]        = None

app_state = AppState()


# ─── LIFESPAN ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicialización: carga datos iniciales al arrancar."""
    logger.info("🐳 Iniciando Sistema de Rastreo de Ballenas...")
    await refresh_all_data()
    logger.info("✅ Sistema listo")
    yield
    logger.info("Cerrando sistema...")


# ─── APP ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="🐳 Whale Tracker — Sistema Institucional de Rastreo",
    description=(
        "Sistema Unificado de Rastreo de Flujos Institucionales y Ballenas. "
        "Integra SEC/EDGAR, On-Chain flows, VWAP institucional y Clonación de carteras."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── LÓGICA DE ACTUALIZACIÓN ─────────────────────────────────────────────────

async def refresh_all_data():
    """Pipeline maestro de actualización de datos."""
    logger.info("🔄 Actualizando todos los datos...")

    # ── Form 4
    form4_parser = Form4Parser()
    try:
        app_state.form4_transactions = await form4_parser.get_recent_form4s(days_back=7)
    finally:
        await form4_parser.close()

    # ── On-Chain
    whale_client = WhaleAlertClient(api_key=WHALE_ALERT_KEY)
    try:
        app_state.onchain_txns = await whale_client.get_transactions(
            min_value_usd=500_000
        )
    finally:
        await whale_client.close()

    # ── Cluster Buy Detection
    detector = ClusterBuyDetector()
    app_state.clusters = detector.detect(app_state.form4_transactions)

    # ── VWAP para tickers activos
    polygon = PolygonDataClient(api_key=POLYGON_KEY)
    engine  = VWAPEngine()
    active_tickers = list({t.ticker for t in app_state.form4_transactions})[:15]

    for ticker in active_tickers:
        try:
            bars = await polygon.get_daily_bars(ticker, days=25)
            if bars:
                volumes   = [b["volume"] for b in bars]
                close     = bars[-1]["close"]
                intraday  = await polygon.get_intraday_bars(ticker)
                analysis  = engine.analyze(
                    ticker=ticker,
                    close_price=close,
                    daily_volume=volumes[-1],
                    volume_history_20d=volumes[:-1],
                    intraday_bars=intraday if intraday else None,
                )
                app_state.vwap_cache[ticker] = analysis
        except Exception as e:
            logger.debug(f"VWAP error {ticker}: {e}")

    await polygon.close()

    # ── Actualizar señales
    scorer = ConvictionScorer()
    for cluster in app_state.clusters:
        score, breakdown = scorer.score(
            ticker=cluster.ticker,
            form4_signals=app_state.form4_transactions,
            onchain_signals=app_state.onchain_txns,
            clusters=app_state.clusters,
            vwap=app_state.vwap_cache.get(cluster.ticker),
        )
        signal = MarketSignal(
            signal_id=f"SIG_{cluster.cluster_id}",
            signal_type=SignalType.CLUSTER_BUY.value,
            strength=min(3, round(score * 4)),
            ticker=cluster.ticker,
            description=(
                f"Cluster Buy detectado: {cluster.n_participants} insiders "
                f"compraron ${cluster.total_value_usd:,.0f} en {cluster.ticker} "
                f"(ventana: {cluster.window_start} → {cluster.window_end})"
            ),
            evidence=[
                f"Participantes: {', '.join(cluster.participants)}",
                f"Valor total: ${cluster.total_value_usd:,.0f}",
                f"Score de convicción: {score:.2%}",
                f"Breakdown: {breakdown}",
            ],
            conviction_score=score,
            tags=["cluster_buy", "insider", cluster.ticker],
        )
        app_state.signals.append(signal)

    app_state.last_refresh = datetime.utcnow()
    logger.info(f"✅ Datos actualizados: {len(app_state.form4_transactions)} Form4 | "
                f"{len(app_state.onchain_txns)} On-Chain | "
                f"{len(app_state.clusters)} Clusters | "
                f"{len(app_state.signals)} Signals")


# ─── ENDPOINTS ────────────────────────────────────────────────────────────────

@app.get("/", tags=["System"])
async def root():
    return {
        "system":  "🐳 Whale Tracker v1.0",
        "status":  "operational",
        "modules": [
            "SEC/EDGAR Form 4 + 13F",
            "On-Chain Monitor (Whale Alert)",
            "Cluster Buy Detection",
            "VWAP Institucional",
            "Portfolio Cloning (Whale Scoop)",
            "I-Ratio + Window Dressing",
        ],
        "last_refresh": app_state.last_refresh.isoformat() if app_state.last_refresh else None,
    }


@app.get("/dashboard", response_model=SystemDashboard, tags=["Dashboard"])
async def get_dashboard():
    """Estado consolidado del sistema para el panel principal."""
    iratio_calc = IratioCalculator()
    iratio_data = iratio_calc.compute(app_state.form4_transactions)

    onchain_agg = NetFlowCalculator().aggregate_exchange_flows(app_state.onchain_txns)
    btc_netflow = onchain_agg.get("bitcoin", {}).get("net_flow_usd", 0)
    eth_netflow = onchain_agg.get("ethereum", {}).get("net_flow_usd", 0)

    accumulation = [
        ticker for ticker, v in app_state.vwap_cache.items()
        if v.is_accumulation
    ]
    distribution = [
        ticker for ticker, v in app_state.vwap_cache.items()
        if not v.is_accumulation and v.price_vs_vwap < 0
    ]

    iratio_adj = iratio_data["iratio_seasonal_adj"]
    market_regime = (
        "RISK_ON"  if iratio_adj > 1.5 else
        "RISK_OFF" if iratio_adj < 0.5 else
        "NEUTRAL"
    )

    return SystemDashboard(
        timestamp=datetime.utcnow(),
        total_signals_today=len(app_state.signals),
        critical_signals=sum(1 for s in app_state.signals if s.strength >= 3),
        active_clusters=app_state.clusters[:5],
        top_whales=[],
        top_onchain_flows=app_state.onchain_txns[:10],
        accumulation_assets=accumulation,
        distribution_assets=distribution,
        cloned_positions=[],
        iratio_current=iratio_data["iratio_raw"],
        iratio_seasonal_adj=iratio_adj,
        market_regime=market_regime,
    )


@app.get("/signals", response_model=List[MarketSignal], tags=["Signals"])
async def get_signals(
    min_strength: int = Query(0, ge=0, le=4),
    signal_type: Optional[str] = Query(None),
    toxic_only: bool = Query(False),
    limit: int = Query(50, le=200),
):
    """Lista de señales filtradas por fuerza, tipo y toxicidad."""
    filtered = app_state.signals

    if min_strength > 0:
        filtered = [s for s in filtered if s.strength >= min_strength]
    if signal_type:
        filtered = [s for s in filtered if s.signal_type == signal_type]
    if toxic_only:
        filtered = [s for s in filtered if s.is_toxic]

    return sorted(
        filtered[:limit],
        key=lambda s: (s.strength, s.conviction_score),
        reverse=True
    )


@app.get("/clusters", response_model=List[ClusterBuy], tags=["Insider"])
async def get_clusters(
    min_participants: int = Query(3, ge=3),
    ticker: Optional[str] = Query(None),
    min_conviction: float = Query(0.0, ge=0.0, le=1.0),
):
    """Clusters de compra de insiders detectados."""
    result = app_state.clusters

    if ticker:
        result = [c for c in result if c.ticker == ticker.upper()]
    result = [c for c in result if c.n_participants >= min_participants]
    result = [c for c in result if c.conviction_score >= min_conviction]

    return sorted(result, key=lambda c: c.conviction_score, reverse=True)


@app.get("/form4", response_model=List[InsiderTransaction], tags=["Insider"])
async def get_form4_transactions(
    ticker: Optional[str] = Query(None),
    open_market_only: bool = Query(True),
    exclude_10b51: bool = Query(True),
    limit: int = Query(100, le=500),
):
    """Transacciones de insiders del Form 4."""
    result = app_state.form4_transactions

    if ticker:
        result = [t for t in result if t.ticker == ticker.upper()]
    if open_market_only:
        result = [t for t in result if t.transaction_code.value == "P"]
    if exclude_10b51:
        result = [t for t in result if not t.is_rule_10b51]

    return sorted(result, key=lambda t: t.value_usd, reverse=True)[:limit]


@app.get("/iratio", tags=["Insider"])
async def get_iratio(lookback_days: int = Query(30, ge=7, le=90)):
    """I-Ratio con ajuste estacional (blackout months)."""
    calc = IratioCalculator()
    return calc.compute(app_state.form4_transactions, lookback_days=lookback_days)


@app.get("/onchain", response_model=List[OnChainTransaction], tags=["Crypto"])
async def get_onchain_flows(
    blockchain: Optional[str] = Query(None),
    accumulation_only: bool = Query(False),
    distribution_only: bool = Query(False),
    min_usd: float = Query(0.0),
    limit: int = Query(50, le=200),
):
    """Flujos on-chain detectados."""
    result = app_state.onchain_txns

    if blockchain:
        result = [t for t in result if t.blockchain == blockchain.lower()]
    if accumulation_only:
        result = [t for t in result if t.is_exchange_outflow]
    if distribution_only:
        result = [t for t in result if t.is_exchange_inflow]
    if min_usd > 0:
        result = [t for t in result if t.amount_usd >= min_usd]

    return sorted(result, key=lambda t: t.amount_usd, reverse=True)[:limit]


@app.get("/onchain/netflow", tags=["Crypto"])
async def get_netflow_summary():
    """Flujo Neto Agregado por blockchain (acumulación vs distribución)."""
    calc = NetFlowCalculator()
    return calc.aggregate_exchange_flows(app_state.onchain_txns)


@app.get("/vwap/{ticker}", response_model=VWAPAnalysis, tags=["Microstructure"])
async def get_vwap(ticker: str):
    """Análisis VWAP institucional para un ticker específico."""
    ticker = ticker.upper()
    if ticker not in app_state.vwap_cache:
        # Intentar calcular en tiempo real
        polygon = PolygonDataClient(api_key=POLYGON_KEY)
        try:
            bars = await polygon.get_daily_bars(ticker, days=25)
            if not bars:
                raise HTTPException(status_code=404, detail=f"No hay datos para {ticker}")
            intraday = await polygon.get_intraday_bars(ticker)
            engine   = VWAPEngine()
            analysis = engine.analyze(
                ticker=ticker,
                close_price=bars[-1]["close"],
                daily_volume=bars[-1]["volume"],
                volume_history_20d=[b["volume"] for b in bars[:-1]],
                intraday_bars=intraday if intraday else None,
            )
            app_state.vwap_cache[ticker] = analysis
        finally:
            await polygon.close()

    return app_state.vwap_cache[ticker]


@app.get("/cloning/whale-scoop", response_model=List[ClonedPortfolioEntry], tags=["Cloning"])
async def get_whale_scoop(
    min_whale_score: int = Query(80, ge=0, le=100),
    min_convergence: float = Query(0.0, ge=0.0, le=1.0),
):
    """Pipeline Whale Scoop: clonación de carteras de superinversores."""
    if not app_state.form13f_holdings:
        # Cargar 13F demo
        f13_parser = Form13FParser()
        try:
            for guru_id, profile in list(GURU_REGISTRY.items())[:3]:
                holdings = await f13_parser.get_latest_13f(profile["cik"])
                app_state.form13f_holdings.extend(holdings)
        finally:
            await f13_parser.close()

    cloner = PortfolioCloner(app_state.form13f_holdings)

    # Precios actuales (demo si no hay API)
    current_prices = {
        t.ticker: app_state.vwap_cache[t.ticker].close_price
        for t in app_state.form13f_holdings
        if t.ticker in app_state.vwap_cache
    }

    portfolio = cloner.run_whale_scoop(
        vwap_analyses=app_state.vwap_cache,
        current_prices=current_prices,
        min_whale_score=min_whale_score,
    )

    return [p for p in portfolio if p.convergence_score >= min_convergence]


@app.get("/gurus", tags=["Cloning"])
async def get_guru_registry():
    """Registro de superinversores monitoreados."""
    return [
        {
            "id":         guru_id,
            "name":       profile["name"],
            "aum_est_b":  profile["aum_est"] / 1e9,
            "style":      profile["style"],
            "rotation":   profile["rotation"],
            "whale_score": profile["whale_score"],
        }
        for guru_id, profile in GURU_REGISTRY.items()
    ]


@app.post("/refresh", tags=["System"])
async def trigger_refresh(background_tasks: BackgroundTasks):
    """Fuerza una actualización de todos los datos en background."""
    background_tasks.add_task(refresh_all_data)
    return {"status": "refresh_triggered", "message": "Actualización iniciada en background"}


@app.get("/health", tags=["System"])
async def health():
    return {
        "status": "healthy",
        "form4_count":    len(app_state.form4_transactions),
        "onchain_count":  len(app_state.onchain_txns),
        "cluster_count":  len(app_state.clusters),
        "signal_count":   len(app_state.signals),
        "vwap_tickers":   list(app_state.vwap_cache.keys()),
        "last_refresh":   app_state.last_refresh.isoformat() if app_state.last_refresh else None,
    }
