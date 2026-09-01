hl-vault-bot
============

Screener und Ranking-Engine fuer Hyperliquid-Vaults.
Phase 1 ist bewusst READ-ONLY: es werden keine Transaktionen signiert oder
gesendet. Erst Daten sammeln und dem Modell vertrauen lernen, dann Kapital.

Nur Python-stdlib, keine Dependencies.


Quickstart
----------

    cd ~/Documents/dev/Johan/hl-vault-bot

    python3 -m hlvault.cli screen -n 20        # ranken
    python3 -m hlvault.cli screen --show-failed # auch die Durchfaller + Gruende
    python3 -m hlvault.cli alloc 5000          # Allokationsvorschlag fuer $5000
    python3 -m hlvault.cli watch               # Check + Alerts gegen letzten Snapshot
    python3 -m hlvault.cli hist 0x1e37a3...    # Score-Historie eines Vaults

Jeder screen/watch-Lauf schreibt einen Snapshot nach data/vaults.db (SQLite).
Genau diese Historie brauchst du spaeter, um zu pruefen ob das Scoring
ueberhaupt was taugt.


Datenquellen
------------

  GET  https://stats-data.hyperliquid.xyz/Mainnet/vaults
       Liste aller ~9500 Vaults (name, address, leader, tvl, isClosed, apr,
       createTimeMillis, PnL-Serien). ~14 MB. Undokumentiert, speist das Web-UI.

  POST https://api.hyperliquid.xyz/info  {"type":"vaultDetails","vaultAddress":"0x.."}
       accountValueHistory, pnlHistory, leaderFraction, leaderCommission,
       allowDeposits, followers.

WICHTIG: der offizielle {"type":"vaultSummaries"} gibt aktuell [] zurueck.
Deshalb der stats-data-Endpoint. Falls der mal wegfaellt, ist STATS_URL in
config.py die einzige Stelle die angepasst werden muss.


Wie gescored wird
-----------------

Zweistufig, weil 9500 Detail-Requests unnoetig waeren:

  1. Vorfilter auf der Liste (billig)   -> ~40 Kandidaten
     offen, kein Child-Vault, TVL 250k..250M, Alter >= 120 Tage
  2. Detaildaten fuer die Kandidaten, dann Kennzahlen + Score

Kennzahlen (metrics.py) kommen aus pnlHistory RELATIV zu accountValueHistory.
Roher PnL waere irrefuehrend: ein Vault der nur viel Kapital eingesammelt hat
sieht sonst automatisch gut aus.

Harte Ausschluesse (config.py -> Filters):
  MaxDD > 25%, leaderFraction < 3%, Commission > 15%,
  < 20 Datenpunkte, CAGR <= 0

Score 0..100, gewichtet (config.py -> Weights):
  Sharpe        40%   annualisiert
  Drawdown      20%   weniger ist besser
  Alter         15%   log-skaliert, 120d..900d
  TVL-Stabil.   15%   30-Tage-Veraenderung; Abfluss straft
  APR/CAGR      10%   bewusst niedrig und bei 200% gedeckelt

Der letzte Punkt ist der eigentliche Trick. Nach APR sortieren liefert oben
Vaults mit 1000%+ und -35% Drawdown - reines Leverage, das irgendwann
komplett abraeucht. Rendite ist hier bewusst das schwaechste Kriterium.


Hyperliquid-Constraints, die die Strategie bestimmen
----------------------------------------------------

  - min. 20 USDC pro Deposit
  - 1 Tag Lockup nach JEDEM Deposit -> kein Intraday-Hopping
  - Leader nimmt bis 10% Profit Share, mit High-Water-Mark
    -> haeufiges Rotieren resettet effektiv deine HWM-Position. Teuer.
  - isClosed / allowDeposits=false -> Vault nimmt kein Geld an
  - Vaults koennen liquidiert werden. Totalverlust ist moeglich.

Daraus folgt: NICHT "immer im besten Vault". Das ist Performance-Chasing und
der Hauptgrund warum solche Bots verlieren. Stattdessen 3-4 Vaults, Cap 30%,
Rebalance woechentlich mit Hysterese (rotation_margin), plus taeglicher
Reissleinen-Check (watch).


Bekannte Einschraenkungen
-------------------------

  - followers ist API-seitig bei 100 gedeckelt -> kein echtes Signal, nur Anzeige
  - accountValueHistory ist gebinnt, nicht taeglich -> Sharpe ist eine
    Naeherung, gut fuer relatives Ranking, nicht als absolute Zahl lesen
  - Kein Survivorship-Bias-Handling: geschlossene/geblowte Vaults sind aus der
    Liste raus, das Feld sieht dadurch besser aus als es war
  - leaderFraction schwankt mit dem TVL, nicht mit dem Commitment des Leaders


Taeglich laufen lassen
----------------------

    crontab -e
    0 8 * * * cd ~/Documents/dev/Johan/hl-vault-bot && /usr/bin/python3 -m hlvault.cli -q watch >> data/watch.log 2>&1


Naechste Schritte (Phase 2+)
----------------------------

  1. 4-6 Wochen Snapshots sammeln, dann pruefen: haetten die Top-4 vom
     Score tatsaechlich die restlichen geschlagen? Wenn nein -> Gewichte
     in config.py anpassen, nicht Kapital einsetzen.
  2. Backtest gegen die historischen PnL-Serien (Walk-Forward, kein In-Sample).
  3. Erst dann Execution: hyperliquid-python-sdk, exchange.vault_transfer().
     Key aus ENV/Keychain, NIE ins Repo. Zuerst Testnet.
  4. Telegram-Notify fuer Alerts.
