"""
═══════════════════════════════════════════════════════════════════════════════
SISTEMA UNIFICADO DE RASTREO DE FLUJOS INSTITUCIONALES Y BALLENAS
constants.py — Tabla de Constantes y Umbrales de Actores de Mercado
═══════════════════════════════════════════════════════════════════════════════
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional


# ─── CATEGORÍAS DE ACTORES ───────────────────────────────────────────────────

class ActorCategory(Enum):
    PLANCTON   = "plancton"
    CAMARON    = "camaron"
    DELFIN     = "delfin"
    TIBURON    = "tiburon"
    BALLENA    = "ballena"
    BALLENA_JOROBADA = "ballena_jorobada"


@dataclass(frozen=True)
class ActorProfile:
    """Perfil completo de un actor de mercado."""
    category: ActorCategory
    label_es: str
    usd_min: Optional[float]
    usd_max: Optional[float]
    btc_min: Optional[float]
    btc_max: Optional[float]
    market_impact: str
    price_discovery_weight: float   # 0.0 → 1.0
    detection_priority: int         # 1 = máxima prioridad


ACTOR_PROFILES = {
    ActorCategory.PLANCTON: ActorProfile(
        category=ActorCategory.PLANCTON,
        label_es="Plancton / Camarón",
        usd_min=0,
        usd_max=100_000,
        btc_min=0,
        btc_max=1.0,
        market_impact="Despreciable; ruido de mercado.",
        price_discovery_weight=0.01,
        detection_priority=5,
    ),
    ActorCategory.DELFIN: ActorProfile(
        category=ActorCategory.DELFIN,
        label_es="Delfín / Tiburón",
        usd_min=1_000_000,
        usd_max=100_000_000,
        btc_min=100,
        btc_max=1_000,
        market_impact="Moderado; manipulación en micro-caps.",
        price_discovery_weight=0.30,
        detection_priority=3,
    ),
    ActorCategory.BALLENA: ActorProfile(
        category=ActorCategory.BALLENA,
        label_es="Ballena",
        usd_min=100_000_000,
        usd_max=1_000_000_000,
        btc_min=1_000,
        btc_max=5_000,
        market_impact="Alto; definición de tendencias sectoriales.",
        price_discovery_weight=0.70,
        detection_priority=2,
    ),
    ActorCategory.BALLENA_JOROBADA: ActorProfile(
        category=ActorCategory.BALLENA_JOROBADA,
        label_es="Ballena Jorobada",
        usd_min=1_000_000_000,
        usd_max=None,
        btc_min=5_000,
        btc_max=None,
        market_impact="Sistémico; capacidad de mover mercados globales.",
        price_discovery_weight=1.0,
        detection_priority=1,
    ),
}


# ─── CONSTANTES DE SEÑALES ────────────────────────────────────────────────────

class SignalType(Enum):
    CLUSTER_BUY           = "cluster_buy"
    CLUSTER_SELL          = "cluster_sell"
    WHALE_ACCUMULATION    = "whale_accumulation"
    WHALE_DISTRIBUTION    = "whale_distribution"
    WALL_INJECTION        = "wall_injection"           # "Inyección de Intencionalidad"
    DARK_POOL_ABSORPTION  = "dark_pool_absorption"
    WINDOW_DRESSING       = "window_dressing"
    INSIDER_CONVICTION    = "insider_conviction"
    SECTOR_MIGRATION      = "sector_migration"
    HEDGING_DETECTED      = "hedging_detected"
    DIRECTIONAL_BET       = "directional_bet"


class SignalStrength(Enum):
    NOISE    = 0
    WEAK     = 1
    MODERATE = 2
    STRONG   = 3
    CRITICAL = 4   # Sistémico


# ─── PARÁMETROS DEL MOTOR CLUSTER BUY ────────────────────────────────────────

CLUSTER_CONFIG = {
    "min_participants":      3,          # Variable N ≥ 3
    "time_window_days":      7,          # Ventana temporal ≤ 7 días
    "valid_transaction_codes": ["P"],    # Solo compras abiertas de mercado
    "invalid_codes": ["A", "M", "F"],    # Premios, opciones, impuestos
    "conviction_ratio_threshold": 0.10, # Compra ≥ 10% de compensación anual
}

# Meses de blackout tras earnings (ajuste estacional del I-Ratio)
BLACKOUT_MONTHS = [3, 5, 8, 11]       # Marzo, Mayo, Agosto, Noviembre
BLACKOUT_SENSITIVITY_FACTOR = 0.65    # Reducir sensibilidad al 65%

# ─── UMBRALES DE MICROESTRUCTURA ─────────────────────────────────────────────

MICROSTRUCTURE = {
    "vwap_volume_sd_threshold": 2.0,   # 2 desviaciones estándar
    "vwap_volume_lookback_days": 20,   # Media móvil de 20 días
    "iceberg_detection_ratio": 1.5,    # Volumen ejecutado / mostrado
    "dix_high_threshold": 45.0,        # DIX > 45% = acumulación institucional
}

# ─── UMBRALES REGULATORIOS ────────────────────────────────────────────────────

REGULATORY = {
    # SEC
    "form_13f_aum_threshold":   100_000_000,   # AUM > $100M obligatorio
    "form_13f_lag_days":        45,
    "form_4_filing_days":       2,
    "schedule_13dg_threshold":  0.05,           # Participación > 5%
    "schedule_13dg_filing_days": 5,             # Plazo 2024 actualizado
    "schedule_13d_activist":    True,
    # CNMV España
    "cnmv_participation_general": 0.03,         # > 3% general
    "cnmv_participation_tax_haven": 0.01,       # > 1% paraísos fiscales
}

# ─── CLASIFICACIÓN ON-CHAIN ───────────────────────────────────────────────────

class OnChainFlowType(Enum):
    EXCHANGE_TO_COLD    = "exchange_to_cold"     # Acumulación / HODL
    COLD_TO_EXCHANGE    = "cold_to_exchange"     # Distribución / Venta potencial
    WALLET_TO_WALLET    = "wallet_to_wallet"     # OTC / transferencia
    MINING_REWARD       = "mining_reward"        # Minería
    UNKNOWN             = "unknown"

ONCHAIN_THRESHOLDS = {
    "btc_whale_min":    1_000,
    "eth_whale_min":    10_000,
    "usdt_whale_min":   1_000_000,
}

# ─── SCORE DE CONVICCIÓN ─────────────────────────────────────────────────────

CONVICTION_WEIGHTS = {
    "form_4_open_market":    0.35,    # Insider comprando con dinero propio
    "onchain_accumulation":  0.30,    # Ballena retirando a cold wallet
    "cluster_buy":           0.20,    # Múltiples insiders coincidentes
    "vwap_above_2sd_vol":    0.10,    # Micrestructura confirmando
    "dix_elevated":          0.05,    # Dark pool acumulación
}
