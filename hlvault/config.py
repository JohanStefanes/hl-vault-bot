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
    """Harte Ausschlusskriterien. Wer hier durchfaellt wird gar nicht erst bewertet.

    Backtest mit geschlossenen Vaults (README): strengerer maxDD + vol-Cap senken
    das Verlustrisiko im schlechtesten 90-Tage-Fenster. Ein Renditevorteil ist
    damit nicht belegt.
    """

    min_tvl: float = 50_000.0          # breiter: mehr Kandidaten -> echte Auswahl
    max_tvl: float = 1_000_000_000.0   # HLP-Groesse wird ohnehin separat gekappt
    min_age_days: float = 90.0         # 3 Monate Mindest-Track-Record
    max_drawdown: float = 0.15         # 15%: der Tail-Risk-Hebel laut Backtest
    max_vol_annual: float = 1.0        # >100% annual. Vol = Leverage-Casino, raus
    min_leader_fraction: float = 0.03  # Leader muss >=3% Skin in the Game haben
    max_leader_commission: float = 0.15
    min_history_points: int = 20       # zu wenig Datenpunkte = keine Statistik
    require_open: bool = True          # isClosed / allowDeposits


@dataclass
class Weights:
    """Score-Gewichte, Summe sollte 1.0 sein.

    ACHTUNG (Backtest-Ergebnis): kein Einzelsignal korreliert nennenswert mit der
    Forward-Rendite (|Spearman| <= 0.12 ueber rund 3700 Vault-Stichtag-Paare). Der
    Score taugt darum als TIE-BREAKER und fuer Transparenz, NICHT als belegter
    Renditevorteil. Belegt ist nur weniger Risiko durch Filter + Streuung.
    Gewichte hier nicht auf die paar Stichtage overfitten.
    """

    sharpe: float = 0.40
    drawdown: float = 0.20
    age: float = 0.15
    tvl_stability: float = 0.15
    apr: float = 0.10

    def total(self) -> float:
        return self.sharpe + self.drawdown + self.age + self.tvl_stability + self.apr


@dataclass
class Portfolio:
    """Allokations-Regeln fuer die spaetere Rebalance-Phase.

    Backtest (Strategie C): Gleichgewicht ueber alle Qualifizierten hatte das
    kleinste Verlustfenster (-5.5%, mit 8 Positionen -10.2%). max_positions=4 weicht
    wegen des kleinen Kapitals bewusst davon ab und traegt mehr Einzelrisiko.
    """

    equal_weight: bool = True         # gleichgewichten statt Score-gewichten
    max_positions: int = 4            # bei kleinem Kapital: ~$60/Vault statt ~$20
    max_weight_per_vault: float = 0.30  # bei 4 Positionen faktisch 1/4, hier Hard-Cap
    min_deposit_usd: float = 20.0     # Hyperliquid Hard-Limit
    lockup_days: int = 1              # Hyperliquid Hard-Limit nach jedem Deposit
    # Hysterese: neuer Kandidat muss den gehaltenen Vault um X Score-Punkte
    # schlagen, sonst wird nicht rotiert (Rotation kostet Profit-Share-HWM).
    rotation_margin: float = 8.0
    # Reissleine: unabhaengig vom Rebalance-Zyklus sofort raus.
    panic_drawdown: float = 0.15      # an den strengeren Filter angeglichen
    panic_tvl_drop: float = 0.40      # TVL faellt 40% ggue. letztem Snapshot


@dataclass
class Config:
    filters: Filters = field(default_factory=Filters)
    weights: Weights = field(default_factory=Weights)
    portfolio: Portfolio = field(default_factory=Portfolio)
    top_n_detail: int = 100  # so viele Kandidaten holen Detaildaten


CONFIG = Config()
