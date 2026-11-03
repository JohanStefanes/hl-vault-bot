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
    python3 -m hlvault.cli fetch-cache         # Detaildaten einmal cachen
    python3 -m hlvault.cli backtest            # Walk-Forward-Backtest

    python3 -m hlvault.cli paper init 5000      # virtuelles Portfolio anlegen
    python3 -m hlvault.cli paper rebalance      # screenen + virtuell allozieren
    python3 -m hlvault.cli paper status         # aktueller Wert (mark-to-market)
    python3 -m hlvault.cli paper ledger         # Transaktions-/Ereignis-Log

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

  1. Vorfilter auf der Liste (billig)   -> ~90 Kandidaten
     offen, kein Child-Vault, TVL 50k..1B, Alter >= 90 Tage
  2. Detaildaten fuer die Kandidaten, dann Kennzahlen + Score

Kennzahlen (metrics.py) kommen aus pnlHistory RELATIV zu accountValueHistory.
Roher PnL waere irrefuehrend: ein Vault der nur viel Kapital eingesammelt hat
sieht sonst automatisch gut aus. Zufluesse im selben Intervall zaehlen halb zur
Basis (Modified Dietz), sonst entstehen Renditen wie -12115%.

Harte Ausschluesse (config.py -> Filters):
  MaxDD > 15%, Vol > 100%, leaderFraction < 3%, Commission > 15%,
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
der Hauptgrund warum solche Bots verlieren. Stattdessen 4 Vaults, Cap 30%,
Rebalance woechentlich mit Hysterese (rotation_margin), plus taeglicher
Reissleinen-Check (watch).


Bekannte Einschraenkungen
-------------------------

  - followers ist API-seitig bei 100 gedeckelt -> kein echtes Signal, nur Anzeige
  - accountValueHistory ist gebinnt, nicht taeglich -> Sharpe ist eine
    Naeherung, gut fuer relatives Ranking, nicht als absolute Zahl lesen
  - Survivorship-Bias nur teilweise behoben: 'fetch-cache --all' holt
    geschlossene Vaults mit |PnL| >= $5000. Kleinere fehlen, und die Vault-Liste
    gibt es erst seit 2026-11-03 als taeglichen Snapshot (db.save_vault_list)
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
     ERLEDIGT -> backtest.py, siehe unten.
  3. Erst dann Execution: hyperliquid-python-sdk, exchange.vault_transfer().
     Key aus ENV/Keychain, NIE ins Repo. Zuerst Testnet.
  4. Telegram-Notify fuer Alerts.


Walk-Forward-Backtest (backtest.py)
-----------------------------------

Testet rueckwirkend, was die Regeln gebracht haetten. Fuer jeden Stichtag zaehlen
nur Daten bis dahin, gemessen wird die Rendite danach.

    python3 -m hlvault.cli fetch-cache --all --out data/backtest_cache/full.json
    python3 -m hlvault.cli backtest --cache data/backtest_cache/full.json
    python3 -m unittest discover tests     # Selbstcheck der Logik

Der volle Abruf dauert rund 40 Minuten und laesst sich fortsetzen. Ohne --all
fehlen geschlossene Vaults, die Zahlen fallen dann viel zu gut aus.

Verfahren:
  1. Stichtage im Abstand des Horizonts, die Fenster ueberlappen also nicht.
  2. Vorfilter, Filter und Score nur mit dem Stand am Stichtag.
  3. Rendite nach Leader-Profit-Share. Jedes Fenster zaehlt als neuer Einstieg.
  4. Ausgabe: Mittel, Median, schlechtestes Fenster, groesster Drawdown im
     Fenster und Bootstrap-90%-Intervall des Mittels.

Ergebnis (Cache 2026-11-03: 1711 Vaults, davon 919 geschlossen; K=4; 6 Fenster
zu je 90 Tagen, nach Fee):

                             Mittel  Median   Worst   MaxDD   90%-Intervall
    Score-Bottom-K            +5.0%   +2.4%   -1.6%   -5.2%   +0.6..+11.3%
    CAGR-Top-K naiv           +3.4%   +9.1%  -48.0%  -52.5%  -15.3..+19.0%
    Gleichgew. o. Leader      +0.6%   +2.1%   -4.6%  -13.3%   -1.7..+2.7%
    Gleichgewicht             +0.4%   +2.2%   -5.7%  -14.1%   -2.2..+2.9%
    Universum                 -0.5%   -2.0%   -9.6%  -25.3%   -5.7..+4.9%
    CAGR-Top-K gefiltert      -1.2%   +0.6%  -13.3%  -15.3%   -5.9..+2.7%
    Score-Top-K               -1.8%   +2.7%  -26.4%  -32.7%  -10.7..+4.2%

Was das ehrlich heisst:
  - Kein Renditevorteil belegt. Die Intervalle sind breit und ueberlappen sich
    fast alle, sechs Fenster reichen nicht fuer mehr.
  - Der Score trennt nicht: die schlechtesten vier lagen vor den besten vier.
    Das ist vermutlich Zufall, aber sicher kein Beleg fuer das Ranking.
  - Belegt ist nur weniger Risiko: Gleichgewicht ueber alle Gefilterten hatte
    als schlechtestes Fenster -5.7%, die naive Rendite-Auswahl -48.0%.
  - Vier Positionen sind riskant: Score-Top-K verlor -26.4% in einem Fenster,
    weil zwei der vier ausgewaehlten Vaults -93% und -51% machten.

Fruehere Fassungen dieses Abschnitts nannten +7% bis +9%. Diese Zahlen waren
falsch: heutiges APR als Auswahlkriterium, keine geschlossenen Vaults, keine
Fees, ueberlappende Fenster und Renditen von unter -100%.

Grenzen:
  - Survivorship-Rest: Vaults mit |PnL| < $5000 fehlen.
  - leaderFraction/-commission gibt es nur als aktuellen Wert. Die Gegenprobe
    ohne Leader-Filter weicht aber kaum ab (+0.6% statt +0.4%).
  - 367 von 66166 Intervallen ergeben rechnerisch -100% oder weniger und zaehlen
    als Totalverlust. Rund die Haelfte der Vaults hat danach wieder Kapital, das
    ist teils ein Binning-Artefakt und macht die Zahlen eher zu pessimistisch.
  - Nur 6 Fenster. Richtungsweisend, statistisch nicht hart.


Strategie-Experimente (experiments/)
------------------------------------

    PYTHONPATH=. python3 experiments/signal_power.py data/backtest_cache/full.json
    PYTHONPATH=. python3 experiments/strategy_variants.py data/backtest_cache/full.json

signal_power: Spearman-Korrelation jedes Signals zur Rendite danach. Auch mit dem
vollen Cache bleibt jedes Signal schwach (|rho| <= 0.12 ueber rund 3700 Paare).
Am staerksten: niedrige Volatilitaet und hohes Alter. Sharpe, mit 40% das hoechste
Gewicht im Score, liegt bei +0.02 auf 90 Tage. Der Score bleibt darum Tie-Breaker.

strategy_variants: Portfolio-Regeln im Vergleich (Stichtage alle 60 Tage, 90 Tage
Horizont, die Fenster ueberlappen also; vor Fee):

                                        Mittel   Worst   Positionen
    A alt (DD<25%, 4 Positionen)         -4.6%  -24.1%       4
    B breit, Gleichgewicht               -2.7%  -16.9%      26
    C streng (DD<15%, vol<1.0), EW       +0.2%   -5.5%      16
    D wie C, aber 8 Positionen           -2.5%  -10.2%       8

Strenge Filter und breite Streuung senken das Verlustrisiko. Der Live-Bot haelt
aber nur 4 Positionen (config.py -> max_positions), weil das Spielkapital klein
ist und pro Deposit 20 USDC noetig sind. Das ist die riskanteste Variante.


Papertrading (paper.py)
-----------------------

Der Schritt zwischen Backtest und echtem Geld: ein virtuelles Portfolio, das
mit der aktuellen Strategie gegen die REALEN, laufenden Vault-Renditen
mitlaeuft. Kein Key, keine Transaktionen, kein Risiko.

    python3 -m hlvault.cli paper init 5000     # virtuelles Startkapital
    python3 -m hlvault.cli paper rebalance     # screenen + virtuell allozieren
    python3 -m hlvault.cli paper status        # mark-to-market gegen Live-Daten
    python3 -m hlvault.cli paper ledger        # alle Aktionen nachvollziehbar

Was es echt macht:
  - Positionswert = usd_in * (1 + reale Vault-Rendite seit Einstieg). Die
    Rendite kommt aus denselben accountValueHistory/pnlHistory wie Backtest
    und metrics - kein erfundener Wert.
  - Es erzwingt die harten Hyperliquid-Regeln: min. 20 USDC pro Deposit,
    1 Tag Lockup (kein Rebalance-Exit vorher), und Leader-Profit-Share auf
    Gewinn ueber der High-Water-Mark wird als Fee abgezogen.
  - Zustand liegt in data/vaults.db (paper_portfolio / paper_positions /
    paper_ledger) und ueberlebt Neustarts.

So nutzt man es: einmal 'paper init', dann woechentlich (z.B. per Cron zum
watch dazu) 'paper rebalance' laufen lassen. Nach ein paar Wochen zeigt
'paper status' die tatsaechlich erzielte Rendite der Strategie - DAS ist die
Zahl, die ueber den Uebergang zu Testnet/Execution entscheidet, nicht der
Backtest.

Bewusst ehrlich:
  - accountValueHistory ist gebinnt, nicht sekundengenau -> kurzfristige Werte
    sind grob. Ueber Wochen mittelt sich das raus.
  - Simuliert wird die Strategie-Rendite, nicht die Marktbedingungen: in echt
    koennte ein Vault gerade keine Deposits annehmen. Das faengt erst die
    echte Execution ab.

Reset: die drei paper_-Tabellen in data/vaults.db leeren (oder 'paper init'
erneut - das ueberschreibt das alte virtuelle Portfolio).


Web-Dashboard (web/ + Vercel)
-----------------------------

Live: https://hl-vault-dashboard-chi.vercel.app

Vercel ist serverless und kann NICHT auf die lokale SQLite oder den 14MB-Screen
zugreifen. Deshalb ist das Dashboard bewusst STATISCH: der Bot exportiert einen
JSON-Snapshot, die Seite liest nur diesen. Aktualisieren = neu exportieren +
neu deployen.

Aufbau:
  hlvault/export_web.py   dumpt Screener-Ranking + Paper-Portfolio + Config
                          nach web/public/data.json
  web/public/index.html   Single-Page-Dashboard (dark, kein Build-Step),
                          laedt data.json und rendert Portfolio + Positionen
                          + Screener-Tabelle
  web/vercel.json         outputDirectory=public, rein statisch

Aktualisieren (der ganze Workflow):
    python3 -m hlvault.export_web --screen     # frisch screenen (Netz, ~15s)
    cd web && vercel deploy --prod --yes       # neu deployen (~5s)

Ohne --screen nimmt der Export den letzten DB-Snapshot (schnell, kein Netz).

Automatisieren (optional): einen Cron bauen, der woechentlich
'paper rebalance', dann 'export_web --screen', dann 'vercel deploy --prod'
laeuft - dann ist das Dashboard immer aktuell, ohne Handarbeit.

Kein echtes Geld, nichts Sensibles im JSON (nur oeffentliche Vault-Adressen und
virtuelle Betraege) - die Seite darf oeffentlich sein.
