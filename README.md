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
     ERLEDIGT -> backtest.py, siehe unten.
  3. Erst dann Execution: hyperliquid-python-sdk, exchange.vault_transfer().
     Key aus ENV/Keychain, NIE ins Repo. Zuerst Testnet.
  4. Telegram-Notify fuer Alerts.


Walk-Forward-Backtest (backtest.py)
-----------------------------------

Beantwortet die Kernfrage von Phase 2 OHNE auf die 4-6 Wochen DB-Snapshots zu
warten: die vaultDetails liefern pro Vault Monate an accountValueHistory +
pnlHistory. Damit laesst sich rueckwirkend testen.

    python3 -m hlvault.cli fetch-cache     # einmal: 40 Kandidaten-Details cachen
    python3 -m hlvault.cli backtest        # gegen den Cache, reproduzierbar

Verfahren (kein In-Sample):
  1. Fuer mehrere Stichtage T (default 6, alle 60 Tage rueckwaerts):
     jede Serie auf t <= T abschneiden, Metriken + Score NUR daraus, dann ranken.
  2. Realisierte Rendite jedes Vaults im Fenster (T, T+H] messen (H = 60/90 Tage).
  3. Strategien vergleichen: Score-Top-K vs. APR-Top-K (naiv) vs. Gleichgewicht
     aller Qualifizierten vs. Score-Bottom-K vs. ganzes Universum.

Ergebnis des ersten Laufs (Cache 2026-09-01, K=4):

                        60d-Horizont   90d-Horizont
    Score-Top-K            +4.96%         +8.03%
    APR-Top-K (naiv)       +7.03%         -6.60%
    Gleichgewicht          +3.68%         +8.73%
    Score-Bottom-K         +3.34%         +9.23%
    ganzes Universum      -38.24%        -79.91%

Was das ehrlich heisst:
  - Die FILTER sind das Wertvolle. Das ganze Universum verliert brutal
    (-38%/-80% durch einzelne Vaults die um -270% einbrechen); alles was durch
    die harten Ausschluesse kommt, liegt zwischen +3% und +9%. Der Nutzen sitzt
    im Rauswerfen, nicht im Feinranking.
  - Nach APR sortieren ist widerlegt: +7% auf 60d, aber -6.6% auf 90d und stark
    von einem einzelnen +134%-Ausreisser getragen. Genau die Instabilitaet, die
    das Scoring vermeiden soll.
  - Score-Top-K schlaegt APR-Top-K auf 90d klar und ist deutlich stabiler.
  - ABER: Score-Top-K ~= Gleichgewicht ~= Score-Bottom-K. Ursache: nach den
    Filtern bleiben meist nur 2-7 Vaults, K=4 ist dann fast das ganze Feld ->
    das Score-RANKING innerhalb der Qualifizierten trennt (noch) nicht. Belegt
    ist der Wert der Filter, nicht der der Gewichte. Solange das so ist, ist
    Gleichgewicht ueber alle Qualifizierten die ehrlichere Allokation als ein
    Score-gewichtetes Top-4.

Grenzen dieses Backtests (nicht wegzudiskutieren):
  - Survivorship-Bias: geblowte/geschlossene Vaults sind aus der Liste raus.
    Das Universum sah in Wahrheit schlechter aus -> der Filtervorteil ist real,
    aber die absoluten Zahlen sind optimistisch.
  - leaderFraction/-commission gibt es nur als AKTUELLEN Wert, nicht als
    Verlauf. Fuer historische Stichtage wird der heutige Wert benutzt -> ein
    kleiner Lookahead-Rest in den Filtern. Score/Ranking selbst ist sauber
    out-of-sample.
  - accountValueHistory ist gebinnt, nicht taeglich -> kurze Fenster sind grob,
    darum H >= 60 Tage.
  - Nur 6 Stichtage x 40 Vaults. Richtungsweisend, statistisch nicht hart.

Konsequenz fuer Phase 3: die Filter behalten, aber das Scoring nicht als
belegten Edge verkaufen. Erst wenn genug echte DB-Snapshots da sind (Schritt 1)
oder ein groesseres/aelteres Universum die Qualifizierten-Zahl hebt, laesst sich
pruefen ob die Gewichte innerhalb der Qualifizierten wirklich trennen.


Strategie-Verbesserung (experiments/)
-------------------------------------

Aufbauend auf dem Backtest wurde die Strategie datengetrieben ueberarbeitet -
nicht per Bauchgefuehl. Zwei Experimente:

  experiments/signal_power.py
     Spearman-Rangkorrelation jedes Signals zur Forward-Rendite, gepoolt ueber
     300+ Vault-Stichtag-Paare. ERGEBNIS: KEIN Einzelsignal sagt die Zukunft
     vorher, |rho| < 0.1 fuer alle. Sharpe (mit 40% das hoechste Gewicht!) ist
     sogar leicht negativ. -> An den Gewichten zu drehen waere Overfitting auf
     Rauschen. Der Score bleibt Tie-Breaker/Transparenz, kein Renditevorteil.

  experiments/strategy_variants.py
     Vergleicht Portfolio-REGELN walk-forward. Kernzahl ist das SCHLECHTESTE
     Einzelfenster (Tail-Risk), nicht der Schnitt:

                                        90d Mittel   90d Worst
        A alt (DD<25%, Score-Top-4)       +6.6%       -11.0%
        B breit, Gleichgewicht            +7.0%        -2.8%
        C strenger (DD<15%,vol<1.0), EW   +8.6%        +0.6%   <- gewinnt
        D wie C, aber nur 8 Positionen    +5.6%        -4.5%

Was tatsaechlich geaendert wurde (config.py), alles aus C:
  - maxDD-Filter 25% -> 15%   (der eigentliche Tail-Risk-Hebel)
  - NEU: vol-Cap 100% annualisiert  (schliesst Leverage-Casino-Vaults aus)
  - TVL-Fenster 250k-250M -> 50k-1B, Alter 120d -> 90d
    -> Feld waechst von 7 auf ~13 Qualifizierte = echte Auswahl statt "alle 4"
  - Allokation: Score-gewichtetes Top-4 -> GLEICHGEWICHT ueber bis zu 12 Vaults
  - panic_drawdown an 15% angeglichen

Die Verbesserung kommt also aus RISIKODISZIPLIN + DIVERSIFIKATION, nicht aus
kluegerem Ranking. Das ist die ehrliche Lehre: bei diesem Rauschabstand gewinnt
man durch Vermeiden der Verlustfenster, nicht durch Return-Chasing.

Reproduzieren:
    PYTHONPATH=. python3 experiments/signal_power.py
    PYTHONPATH=. python3 experiments/strategy_variants.py


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
