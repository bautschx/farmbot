"""Paarfilter fuer die Echt-Strategie (18.11.2026, Stefan).

Erlaubt sind nur Paare aus einem Dollar-Token und
  - einem Coinbase-Aktien-Token auf Base (B20-Standard, Kuerzel mit kleinem "c", z. B. NVDAc), oder
  - einem Top-N-Coin nach Marktkapitalisierung (ohne Stablecoins), z. B. WETH, cbBTC, SOL.

Top-N kommt von CoinGecko (frei, ohne Schluessel), hoechstens alle 12 h, im cache gemerkt.
Schutz gegen gleichnamige Fremd-Token: Der Kurs des Tokens im Pool muss nahe am CoinGecko-Kurs liegen.
"""
import json
import re
import time
import urllib.request

# Coinbase-Aktien-Token: Adressen beginnen mit 0xb2 und 20 Nullen, Kuerzel = Boersenkuerzel + "c"
AKTIE_ADR = re.compile(r"^0xb20{20}")
AKTIE_SYM = re.compile(r"^[A-Z]{1,5}c$")

# Kuerzel auf Base -> Kuerzel bei CoinGecko
ALIAS = {"weth": "eth", "cbbtc": "btc", "wbtc": "btc", "cbeth": "eth", "wsteth": "eth", "cbxrp": "xrp",
         "cbdoge": "doge", "cbltc": "ltc", "cbada": "ada"}

STABLE_CG = {"usdt", "usdc", "usde", "dai", "usds", "fdusd", "pyusd", "usd1", "tusd", "rlusd", "usdtb", "bsc-usd"}


def ist_aktie(sym, adr):
    return bool(AKTIE_ADR.match(adr.lower()) and AKTIE_SYM.match(sym or ""))


def top_liste(cache, n):
    """{kuerzel: usd_kurs} der Top-n-Coins ohne Stablecoins. Bei Fehler: letzte bekannte Liste."""
    c = cache.setdefault("top_coins", {})
    if c.get("liste") and time.time() - c.get("zeit", 0) < 12 * 3600:
        return c["liste"]
    try:
        url = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
               "&order=market_cap_desc&per_page=40&page=1")
        req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "farmbot"})
        with urllib.request.urlopen(req, timeout=20) as r:
            daten = json.loads(r.read())
        liste = {}
        for m in daten:
            s = (m.get("symbol") or "").lower()
            if s in STABLE_CG or "usd" in s or s in liste:
                continue
            liste[s] = m.get("current_price")
            if len(liste) >= n:
                break
        if liste:
            c.update({"liste": liste, "zeit": time.time()})
    except Exception as e:
        print("Top-Coins (CoinGecko) fehlgeschlagen:", e)
    return c.get("liste") or {}


def erlaubt(f, dollar, top, max_abweichung):
    """(ok, art) - art ist 'Aktie', 'Top' oder der Grund fuer die Ablehnung."""
    syms = f["paar"].split("/")
    seiten = list(zip(syms, (f["t0"], f["t1"]), ("p0", "p1")))
    stable = [s for s in seiten if s[1] in dollar]
    andere = [s for s in seiten if s[1] not in dollar]
    if not stable:
        return False, "kein Dollar-Token"
    if not andere:
        return False, "nur Dollar-Token"
    sym, adr, pk = andere[0]
    if ist_aktie(sym, adr):
        return True, "Aktie"
    cg = ALIAS.get(sym.lower(), sym.lower())
    if cg not in top:
        return False, "nicht Top-Coin"
    kurs_pool, kurs_cg = (f.get("sim") or {}).get(pk), top[cg]
    if not kurs_pool or not kurs_cg:
        return False, "Kurs fehlt"
    if abs(kurs_pool / kurs_cg - 1) * 100 > max_abweichung:
        return False, "Kurs weicht von CoinGecko ab"
    return True, "Top"
