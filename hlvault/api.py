"""Duenner Client fuer die Hyperliquid-Endpoints. Nur stdlib."""
from __future__ import annotations

import gzip
import http.client
import json
import time
import urllib.error
import urllib.request

from .config import CONFIG, DETAIL_SLEEP, HTTP_TIMEOUT, INFO_URL, STATS_URL

_UA = "hl-vault-bot/0.1 (research)"

# Fehler, die ein kurzlebiger Netz-/Server-Hickup sind und einen Retry lohnen.
# WICHTIG: http.client.IncompleteRead ist KEIN URLError-Subtyp, sondern eine
# HTTPException. Der grosse 14MB-Stats-Endpoint liefert unter Last gerne mal
# einen abgeschnittenen Body -> ohne diesen Fang stirbt der ganze Lauf.
_RETRYABLE = (
    urllib.error.URLError,
    http.client.IncompleteRead,
    http.client.HTTPException,
    TimeoutError,
    ConnectionError,
    json.JSONDecodeError,
)


def _retry(fn, *, tries: int = 4, base_sleep: float = 1.5):
    """Fuehrt fn() mit exponentiellem Backoff aus. Letzter Fehler fliegt weiter."""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            return fn()
        except _RETRYABLE as e:
            last = e
            if attempt == tries - 1:
                raise
            time.sleep(base_sleep * (2 ** attempt))
    assert last is not None
    raise last


def _get(url: str) -> object:
    def once() -> object:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Accept-Encoding": "gzip"}
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        return json.loads(raw)

    return _retry(once)


def _post(url: str, payload: dict) -> object:
    body = json.dumps(payload).encode()

    def once() -> object:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": _UA},
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())

    return _retry(once)


def fetch_all_vaults() -> list[dict]:
    """Alle ~9500 Vaults als Rohliste (summary + apr + pnls)."""
    data = _get(STATS_URL)
    if not isinstance(data, list):
        raise RuntimeError(f"Unerwartete Antwort von {STATS_URL}: {type(data)}")
    return data


def fetch_vault_details(address: str) -> dict:
    """Detaildaten inkl. accountValueHistory, leaderFraction, followers."""
    return _post(INFO_URL, {"type": "vaultDetails", "vaultAddress": address})


def fetch_details_bulk(addresses: list[str], on_progress=None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i, addr in enumerate(addresses, 1):
        try:
            out[addr] = fetch_vault_details(addr)  # _post retryt bereits intern
        except _RETRYABLE:
            pass  # einzelner Vault faellt raus, Lauf geht weiter
        if on_progress:
            on_progress(i, len(addresses))
        time.sleep(DETAIL_SLEEP)
    return out


def fetch_user_vault_equities(user_address: str) -> list[dict]:
    """Aktuelle Vault-Positionen einer Adresse (fuer spaeteres Tracking)."""
    return _post(INFO_URL, {"type": "userVaultEquities", "user": user_address})


__all__ = [
    "fetch_all_vaults",
    "fetch_vault_details",
    "fetch_details_bulk",
    "fetch_user_vault_equities",
    "CONFIG",
]
