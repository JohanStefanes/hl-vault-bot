"""Duenner Client fuer die Hyperliquid-Endpoints. Nur stdlib."""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request

from .config import CONFIG, DETAIL_SLEEP, HTTP_TIMEOUT, INFO_URL, STATS_URL

_UA = "hl-vault-bot/0.1 (research)"


def _get(url: str) -> object:
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept-Encoding": "gzip"}
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw)


def _post(url: str, payload: dict) -> object:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": _UA},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


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
        for attempt in range(3):
            try:
                out[addr] = fetch_vault_details(addr)
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                if attempt == 2:
                    break
                time.sleep(1.5 * (attempt + 1))
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
