"""
models.py — Modelos de Datos Pydantic para el Sistema de Rastreo
"""

from __future__ import annotations
from datetime import datetime, date
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, validator
from enum import Enum


# ─── ENUMS ────────────────────────────────────────────────────────────────────

class AssetClass(str, Enum):
    EQUITY   = "equity"
    CRYPTO   = "crypto"
    OPTIONS  = "options"
    ETF      = "etf"


class TransactionCode(str, Enum):
    P = "P"   # Open market purchase ✅
    S = "S"   # Open market sale
    A = "A"   # Award/grant (descartar)
    M = "M"   # Exercise of derivative (descartar)
    F = "F"   # Tax payment (descartar)
    G = "G"   # Gift
    D = "D"   # Sale to issuer


class FilingType(str, Enum):
    FORM_4          = "4"
    FORM_13F        = "13F"
    SCHEDULE_13D    = "SC 13D"
    SCHEDULE_13G    = "SC 13G"
    DEF_14A         = "DEF 14A"
    CNMV_IIC        = "CNMV-IIC"
    CNMV_PART_SIG   = "CNMV-PARTICIPACION-SIGNIFICATIVA"


# ─── ENTIDADES BASE ───────────────────────────────────────────────────────────

class WhaleEntity(BaseModel):
    """Entidad identificada como actor institucional o ballena."""
    entity_id:      str
    name:           str
    category:       str                   # ActorCategory value
    aum_usd:        Optional[float]
    btc_holdings:   Optional[float]
    conviction_score: float = Field(0.0, ge=0.0, le=1.0)
    filing_cik:     Optional[str]         # SEC CIK
    wallet_addresses: List[str] = []      # On-chain wallets detectadas
    last_updated:   datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class InsiderTransaction(BaseModel):
    """Transacción de insider extraída del Form 4 (SEC)."""
    filing_id:          str
    issuer_cik:         str
    issuer_name:        str
    ticker:             str
    insider_name:       str
    insider_title:      str
    transaction_date:   date
    transaction_code:   TransactionCode
    shares:             float
    price_per_share:    float
    value_usd:          float
    is_rule_10b51:      bool = False        # Plan automático (excluir de señales)
    footnote_raw:       Optional[str]
    # Enriquecimiento posterior
    annual_compensation: Optional[float]
    conviction_ratio:    Optional[float]    # value_usd / annual_compensation
    cluster_id:         Optional[str]
    filed_at:           datetime = Field(default_factory=datetime.utcnow)

    @validator("value_usd", pre=True, always=True)
    def compute_value(cls, v, values):
        if v is None and "shares" in values and "price_per_share" in values:
            return values["shares"] * values["price_per_share"]
        return v


class Form13FHolding(BaseModel):
    """Posición reportada en Form 13F."""
    filing_id:      str
    manager_cik:    str
    manager_name:   str
    quarter_end:    date
    ticker:         str
    cusip:          str
    value_usd:      float
    shares:         float
    pct_portfolio:  float = 0.0          # % del total del fondo
    prev_shares:    Optional[float]      # Quarter anterior para calcular cambio
    change_pct:     Optional[float]      # (shares - prev_shares) / prev_shares
    is_new_position: bool = False
    filed_at:       datetime = Field(default_factory=datetime.utcnow)


class Schedule13Filing(BaseModel):
    """Schedule 13D/G — Adquisición >5%."""
    filing_id:      str
    filer_name:     str
    filer_cik:      str
    issuer_name:    str
    ticker:         str
    filing_type:    FilingType           # 13D (activista) vs 13G (pasivo)
    pct_acquired:   float
    shares_held:    float
    filing_date:    date
    is_activist:    bool = False         # 13D = intención activista
    purpose_text:   Optional[str]


# ─── ON-CHAIN ─────────────────────────────────────────────────────────────────

class OnChainTransaction(BaseModel):
    """Transacción on-chain detectada (Whale Alert / nodos propios)."""
    tx_hash:        str
    blockchain:     str                  # "bitcoin", "ethereum", etc.
    from_address:   str
    to_address:     str
    amount:         float                # En unidad nativa del activo
    amount_usd:     float
    flow_type:      str                  # OnChainFlowType value
    from_label:     Optional[str]        # "Binance", "Unknown Whale #42"
    to_label:       Optional[str]
    is_exchange_inflow:  bool = False    # Hacia exchange (distribución)
    is_exchange_outflow: bool = False    # Desde exchange (acumulación)
    timestamp:      datetime
    block_number:   Optional[int] = None


class WalletNetFlow(BaseModel):
    """Flujo neto de una wallet en un período."""
    wallet_address: str
    label:          Optional[str]
    asset:          str
    period_start:   datetime
    period_end:     datetime
    inflow:         float
    outflow:        float
    net_flow:       float                # Positivo = acumulación
    n_transactions: int
    last_activity:  Optional[datetime]


# ─── SEÑALES Y ALERTAS ────────────────────────────────────────────────────────

class ClusterBuy(BaseModel):
    """Compra Agrupada detectada (N insiders ≥ 3, ventana ≤ 7 días)."""
    cluster_id:         str
    ticker:             str
    detection_date:     date
    window_start:       date
    window_end:         date
    n_participants:     int              # ≥ 3 por definición
    participants:       List[str]
    total_value_usd:    float
    avg_price:          float
    transactions:       List[str]        # IDs de InsiderTransaction
    conviction_score:   float = Field(0.0, ge=0.0, le=1.0)
    confirmed_by_vwap:  bool = False
    confirmed_by_onchain: bool = False


class MarketSignal(BaseModel):
    """Señal de alta convicción emitida por el motor."""
    signal_id:      str
    signal_type:    str                  # SignalType value
    strength:       int                  # SignalStrength value (0-4)
    ticker:         Optional[str]
    asset:          Optional[str]
    description:    str
    evidence:       List[str]            # Referencias a IDs de datos fuente
    conviction_score: float = Field(0.0, ge=0.0, le=1.0)
    is_toxic:       bool = False         # Marcado como Window Dressing / manipulativo
    tags:           List[str] = []
    created_at:     datetime = Field(default_factory=datetime.utcnow)
    expires_at:     Optional[datetime]

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


# ─── VWAP Y MICROESTRUCTURA ───────────────────────────────────────────────────

class VWAPAnalysis(BaseModel):
    """Análisis VWAP institucional para un activo."""
    ticker:             str
    date:               date
    vwap:               float
    close_price:        float
    price_vs_vwap:      float            # close - vwap (positivo = sobre VWAP)
    volume_today:       float
    volume_ma20:        float
    volume_sd:          float
    volume_zscore:      float            # (vol_today - ma20) / sd
    is_accumulation:    bool             # Precio > VWAP AND vol_zscore > 2
    dix_score:          Optional[float]  # Dark Index (0-100)


class IcebergDetection(BaseModel):
    """Detección de orden iceberg o flujo de dark pool."""
    ticker:             str
    price_level:        float
    displayed_volume:   float
    executed_volume:    float
    iceberg_ratio:      float            # executed / displayed
    timestamp:          datetime
    is_confirmed:       bool = False
    exchange:           Optional[str]


# ─── CARTERA CLONADA ─────────────────────────────────────────────────────────

class ClonedPortfolioEntry(BaseModel):
    """Posición en cartera clonada de superinversor."""
    guru_name:          str
    ticker:             str
    weight_in_guru:     float            # % de la cartera del gurú
    entry_price:        float
    current_price:      float
    pnl_pct:            float
    n_gurus_holding:    int              # Cuántas ballenas tienen este activo
    convergence_score:  float            # Score de convergencia entre ballenas
    vwap_validated:     bool = False     # ¿La entrada fue validada por VWAP?
    risk_hedge:         bool = False     # ¿Existe cobertura con derivados?
    whale_score:        Optional[float]  # WhaleWisdom score del fondo


# ─── RESUMEN EJECUTIVO ────────────────────────────────────────────────────────

class SystemDashboard(BaseModel):
    """Estado consolidado del sistema para el endpoint /dashboard."""
    timestamp:              datetime
    total_signals_today:    int
    critical_signals:       int
    active_clusters:        List[ClusterBuy]
    top_whales:             List[WhaleEntity]
    top_onchain_flows:      List[OnChainTransaction]
    accumulation_assets:    List[str]
    distribution_assets:    List[str]
    cloned_positions:       List[ClonedPortfolioEntry]
    iratio_current:         float
    iratio_seasonal_adj:    float
    market_regime:          str          # "RISK_ON" | "RISK_OFF" | "NEUTRAL"

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}
