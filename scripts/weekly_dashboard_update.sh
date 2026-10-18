#!/usr/bin/env bash
#
# Woechentlicher Dashboard-Update fuer den hl-vault-bot.
# Mechanisch, kein LLM: rebalance -> export -> deploy.
# Wird per launchd aufgerufen (~/Library/LaunchAgents/com.kollers.hlvault-*.plist).
#
set -euo pipefail

# stderr ins stdout-Log mitnehmen, damit Fehler im selben Log landen.
exec 2>&1

# launchd liefert ein minimales Environment: HOME/PATH explizit setzen, damit
# python3 (Homebrew) und der Vercel-Login (~/Library/Application Support/com.vercel.cli) gefunden werden.
export HOME="/Users/kollers"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO="/Users/kollers/Documents/dev/Johan/hl-vault-bot"
cd "$REPO"

echo "=== hl-vault-bot Dashboard-Update $(date '+%Y-%m-%d %H:%M') ==="

# 1. Papertrading rebalancen (virtuell, keine echten Transaktionen)
echo "-- paper rebalance --"
python3 -m hlvault.cli -q paper rebalance 2>&1 | tail -6

# 2. Frischen Snapshot exportieren (screent live gegen Mainnet)
# Bis zu 3 Versuche: der 14MB-Stats-Endpoint liefert unter Last mal IncompleteRead.
echo "-- export --"
EXPORT_OK=0
for attempt in 1 2 3; do
  if python3 -m hlvault.export_web --screen 2>&1 | tail -3; then
    EXPORT_OK=1
    break
  fi
  echo "  export Versuch $attempt fehlgeschlagen, retry in 10s..."
  sleep 10
done
if [ "$EXPORT_OK" -ne 1 ]; then
  echo "FEHLER: Export 3x fehlgeschlagen, kein Deploy (alte Daten bleiben online)."
  exit 1
fi

# 3. Auf Vercel deployen (Prod). Vercel-CLI ist auf dieser Maschine eingeloggt.
echo "-- deploy --"
cd "$REPO/web"
# --token aus Env falls gesetzt, sonst nutzt vercel die lokale Anmeldung
if command -v vercel >/dev/null 2>&1; then
  # rc separat abfangen: unter set -e wuerde ein Fehler hier sonst still abbrechen.
  DEPLOY_RC=0
  DEPLOY_OUT="$(vercel deploy --prod --yes 2>&1 </dev/null)" || DEPLOY_RC=$?
  if [ "$DEPLOY_RC" -ne 0 ]; then
    echo "FEHLER: vercel deploy exit $DEPLOY_RC (HOME=$HOME). Ausgabe:"
    printf '%s\n' "$DEPLOY_OUT" | tail -20
    exit 1
  fi
  # URL robust extrahieren (neues Vercel-JSON: "url": "https://...vercel.app")
  DEPLOY_URL="$(printf '%s\n' "$DEPLOY_OUT" | grep -Eo 'https://[a-z0-9.-]+\.vercel\.app' | tail -1 || true)"
  if [ -z "$DEPLOY_URL" ]; then
    echo "FEHLER: Deploy-URL nicht gefunden. Vercel-Ausgabe:"
    printf '%s\n' "$DEPLOY_OUT" | tail -20
    exit 1
  fi
  echo "$DEPLOY_URL"
else
  echo "FEHLER: vercel CLI nicht gefunden" >&2
  exit 1
fi

echo "=== fertig: https://hl-vault-dashboard-chi.vercel.app ==="
