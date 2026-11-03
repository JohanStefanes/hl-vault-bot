# HL Vault Bot: Kurzüberblick und Setup

Der Bot sucht auf Hyperliquid die Vaults mit dem geringsten Absturzrisiko und testet sie mit Spielgeld. Er signiert keine Transaktionen und braucht weder Wallet noch Key, du kannst also nichts verlieren.

Ein Vault ist ein Topf, den ein Trader verwaltet. Anleger zahlen ein, der Trader bekommt bis zu 10 % vom Gewinn. Vaults können auch komplett abstürzen.

## So funktioniert es

Stand 03.11.2026:

1. **Liste holen:** Der Bot lädt alle 9.478 Vaults von Hyperliquid.
2. **Grob aussortieren (90 übrig):** Der Vault muss offen sein, 50.000 $ bis 1 Mrd. $ verwalten und mindestens 90 Tage alt sein.
3. **Hart filtern (14 übrig):** Maximaler Verlust vom Hoch höchstens 15 %, Schwankung höchstens 100 % pro Jahr, Trader mit mindestens 3 % eigenem Geld drin, Gebühr höchstens 15 %, Rendite positiv.
4. **Punkte vergeben (0–100):** Sharpe 40 %, Drawdown 20 %, Alter 15 %, TVL-Stabilität 15 %, Rendite 10 %.
5. **Mit Spielgeld testen:** Die besten 4 bekommen gleich viel virtuelles Geld. Bewertet wird mit den echten Vault-Renditen. Die Hyperliquid-Regeln gelten auch hier: mindestens 20 USDC pro Einzahlung, 1 Tag Sperrfrist, Gewinnanteil des Traders.

Der Screener und das Dashboard laufen getrennt: Der Bot schreibt eine JSON-Datei, und die Webseite zeigt nur diese Datei an.

## Setup

Du brauchst Python 3, git und Zugriff auf das private GitHub-Repo (Johan muss dich einladen). Es gibt nichts zu installieren, der Bot nutzt nur die Python-Standardbibliothek. Getestet auf macOS mit Python 3.14 in einem frischen Klon.

```bash
git clone https://github.com/JohanStefanes/hl-vault-bot.git
cd hl-vault-bot
```

Vaults ranken (lädt live, kann bis zu 2 Minuten dauern):

```bash
python3 -m hlvault.cli screen -n 10
```

Portfolio mit 1.000 $ Spielgeld anlegen und bewerten:

```bash
python3 -m hlvault.cli paper init 1000
python3 -m hlvault.cli paper rebalance
python3 -m hlvault.cli paper status
```

Dashboard lokal starten, danach http://localhost:8765 öffnen:

```bash
python3 -m hlvault.export_web
cd web/public && python3 -m http.server 8765
```

## Ausprobieren

| Befehl (`python3 -m hlvault.cli ...`) | Was passiert |
| --- | --- |
| `screen --show-failed` | Zeigt auch die aussortierten Vaults mit Grund |
| `alloc 1000` | Schlägt eine Aufteilung für 1.000 $ vor |
| `watch` | Prüft auf Warnsignale gegenüber dem letzten Lauf |
| `paper rebalance` | Schichtet das Spielgeld um, am besten einmal pro Woche |
| `paper ledger` | Zeigt alle Spielgeld-Buchungen |
| `fetch-cache`, danach `backtest` | Testet die Regeln an historischen Daten |

Alle Stellschrauben stehen in `hlvault/config.py`:

| Einstellung | Standard | Wirkung |
| --- | --- | --- |
| `max_drawdown` | 0.15 | Maximal erlaubter Verlust vom Hoch |
| `max_vol_annual` | 1.0 | Maximal erlaubte Schwankung pro Jahr |
| `min_age_days` | 90 | Mindestalter des Vaults in Tagen |
| `min_leader_fraction` | 0.03 | Mindestanteil des Traders am eigenen Vault |
| `max_positions` | 4 | Anzahl Vaults im Portfolio |
| `rotation_margin` | 8.0 | Punktevorsprung, ab dem ein Vault ersetzt wird |

Guter erster Versuch: Einen Wert ändern und `backtest` erneut laufen lassen. Mit `max_drawdown = 0.10` sank die Gleichgewicht-Rendite über 90 Tage zum Beispiel von +7,32 % auf +5,82 %.

Damit nichts schiefgeht:

- `paper init` überschreibt dein bisheriges Spielgeld-Portfolio.
- `export_web` ändert die versionierte Datei `web/public/data.json`. Zurücksetzen mit `git checkout web/public/data.json`.
- `scripts/` nicht ausführen: Die Skripte enthalten Johans Pfade und deployen auf Johans Vercel-Projekt.
- `experiments/` läuft im frischen Klon nicht, weil `data/backtest_cache/wide.json` fehlt.

## Was der Test zeigt

Die Filter schützen vor den großen Abstürzen, das Punkte-Ranking bringt dagegen keinen belegbaren Vorteil. Backtest über 90 Tage, 6 Stichtage, Daten vom 03.11.2026, 4 Vaults pro Strategie:

| Strategie | Mittel | Schlechtestes Fenster |
| --- | --- | --- |
| Die 4 mit der höchsten Rendite (APR) | +19,23 % | −3,2 % |
| Die 4 schlechtesten Scores unter den Gefilterten | +7,76 % | +0,5 % |
| Gleich viel in alle Gefilterten | +7,32 % | +2,8 % |
| Die 4 besten Scores | +7,29 % | +0,1 % |
| Alle Vaults ohne Filter | −27,77 % | −110,8 % |

So solltest du das lesen:

- **Nach Rendite sortieren lag hier vorn**, hatte aber als einzige gefilterte Strategie ein Verlustfenster. Das README nennt diese Methode noch „widerlegt“, das geben die aktuellen Daten nicht her.
- **Die Zahlen sind zu optimistisch:** Geschlossene Vaults fehlen in den Daten, und 6 Stichtage sind wenig.
- **Werte unter −100 %** sind ein Rechenartefakt, mehr als den Einsatz kann man nicht verlieren.
- **Die Vergangenheit sagt wenig über die Zukunft.** Echtes Geld kann in Vaults komplett verloren gehen, und das hier ist keine Anlageberatung.
