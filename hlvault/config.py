"""Zentrale Konfiguration: Filter, Gewichte, Limits.

Alles was du tunen willst steht hier drin - nicht im Code verstreut.
"""
from dataclasses import dataclass, field


# --- API ------------------------------------------------------------------
# Der offizielle info-Endpoint {"type":"vaultSummaries"} liefert aktuell [].
# Die Vault-Liste kommt darum vom stats-data Endpoint, der auch das Web-UI speist.
STATS_URL = "https://stats-data.hyperliquid.xyz/Mainnet/vaults"
INFO_URL = "https://api.hyperliquid.xyz/info"

HTTP_TIMEOUT = 60
DETAIL_SLEEP = 0.15  # Sekunden zwischen vaultDetails-Requests (Rate-Limit schonen)


@dataclass
class Filters:
    """Harte Ausschlusskriterien. Wer hier durchfaellt wird gar nicht erst bewertet."""

    min_tvl: float = 250_000.0        # unter 250k ist es nicht liquide genug
    max_tvl: float = 250_000_000.0    # HLP-Groesse verzerrt das Ranking
    min_age_days: float = 120.0       # weniger als 4 Monate = kein Track Record
    max_drawdown: float = 0.25        # 25% Peak-to-Trough auf Equity
    min_leader_fraction: float = 0.03  # Leader muss >=3% Skin in the Game haben
    max_leader_commission: float = 0.15
    min_history_points: int = 20      # zu wenig Datenpunkte = keine Statistik
    require_open: bool = True         # isClosed / allowDeposits


@dataclass
class Weights:
    """Score-Gewichte, Summe sollte 1.0 sein."""

    sharpe: float = 0.40
    drawdown: float = 0.20
    age: float = 0.15
    tvl_stability: float = 0.15
    apr: float = 0.10

    def total(self) -> float:
        return self.sharpe + self.drawdown + self.age + self.tvl_stability + self.apr


@dataclass
class Portfolio:
    """Allokations-Regeln fuer die spaetere Rebalance-Phase."""

    max_positions: int = 4
    max_weight_per_vault: float = 0.30
    min_deposit_usd: float = 20.0     # Hyperliquid Hard-Limit
    lockup_days: int = 1              # Hyperliquid Hard-Limit nach jedem Deposit
    # Hysterese: neuer Kandidat muss den gehaltenen Vault um X Score-Punkte
    # schlagen, sonst wird nicht rotiert (Rotation kostet Profit-Share-HWM).
    rotation_margin: float = 8.0
    # Reissleine: unabhaengig vom Rebalance-Zyklus sofort raus.
    panic_drawdown: float = 0.20
    panic_tvl_drop: float = 0.40      # TVL faellt 40% ggue. letztem Snapshot


@dataclass
class Config:
    filters: Filters = field(default_factory=Filters)
    weights: Weights = field(default_factory=Weights)
    portfolio: Portfolio = field(default_factory=Portfolio)
    top_n_detail: int = 60  # so viele Kandidaten holen Detaildaten


CONFIG = Config()
