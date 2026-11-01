"""Klickbare Links fuer Telegram-Meldungen.

Je Farm/Pool zwei Links:
  📈 Chart     -> DexScreener-Seite des Pools (Kurs, Liquiditaet, Handel)
  ➕ Einsteigen -> Einzahlseite der DEX, wenn eine Vorlage in config.json steht
                  ("einstieg_links"), sonst die Startseite der DEX ("dex_seiten").

Vorlagen koennen {pool}, {t0}, {t1}, {fee} (Gebuehr in Millionstel, 0,25 % = 2500)
und {chain_id} enthalten. Alles, was hier fehlt, faellt still weg - eine Meldung
darf an einem Link nie scheitern.
"""
import html

DEXSCREENER_NETZ = {
    "Base": "base", "OP Mainnet": "optimism", "Arbitrum": "arbitrum",
    "Robinhood": "robinhood", "Avalanche": "avalanche", "BNB Chain": "bsc",
    "HyperEVM": "hyperevm",
}
CHAIN_ID = {"Base": 8453, "OP Mainnet": 10, "Arbitrum": 42161, "Robinhood": 4663,
            "Avalanche": 43114, "BNB Chain": 56, "HyperEVM": 999}

_konfig = {}


def einrichten(konfig):
    """Einmal pro Lauf aus main.py aufrufen."""
    global _konfig
    _konfig = konfig or {}


def _a(url, text):
    return f'<a href="{html.escape(url, quote=True)}">{text}</a>'


def chart_url(chain, pool):
    netz = DEXSCREENER_NETZ.get(chain)
    if not netz or not pool or not str(pool).startswith("0x") or len(pool) != 42:
        return None
    return f"https://dexscreener.com/{netz}/{pool.lower()}"


def einstieg_url(chain, dex, pool=None, t0=None, t1=None, gebuehr_prozent=None):
    """(url, direkt) - direkt=False heisst: nur Startseite der DEX."""
    vorlagen = _konfig.get("einstieg_links", {})
    v = vorlagen.get(f"{chain}|{dex}") or vorlagen.get(dex)
    if v:
        werte = {"pool": pool, "t0": t0, "t1": t1, "chain_id": CHAIN_ID.get(chain),
                 "fee": None if gebuehr_prozent is None else int(round(gebuehr_prozent * 10000))}
        if all(werte.get(k) is not None for k in ("pool", "t0", "t1", "fee", "chain_id") if "{" + k + "}" in v):
            return v.format(**werte), True
    seite = _konfig.get("dex_seiten", {}).get(dex)
    return (seite, False) if seite else (None, False)


def zeile(chain, dex=None, pool=None, t0=None, t1=None, gebuehr_prozent=None, einstieg=None):
    """Linkzeile fuer Telegram (HTML). einstieg: fertige Einzahl-URL (z. B. von Merkl)."""
    teile = []
    c = chart_url(chain, pool)
    if c:
        teile.append(_a(c, "📈 Chart"))
    if einstieg:
        teile.append(_a(einstieg, "➕ Einsteigen"))
    elif dex:
        url, direkt = einstieg_url(chain, dex, pool, t0, t1, gebuehr_prozent)
        if url:
            teile.append(_a(url, "➕ Einsteigen" if direkt else f"🏦 {html.escape(dex)} öffnen"))
    return "  " + " · ".join(teile) if teile else ""


def fuer_farm(f):
    return zeile(f.get("chain"), f.get("dex"), f.get("pool"), f.get("t0"), f.get("t1"),
                 f.get("gebuehr_prozent"))
