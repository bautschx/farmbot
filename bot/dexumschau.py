"""Einmalige Umschau (Stefan 21.11.2026): Welche DEXe auf Base und Robinhood haben Farmen,
die zu den Paar-Regeln der Bot-Wallet passen (Stablecoin + Aktien-Token oder Top-20-Coin)?

Quellen: DefiLlama Yields (alle Pools mit Belohnungs-APY, nach Projekt) und Merkl (Kampagnen).
Ergebnis: docs/data/dexumschau.json. Nur lesen, nichts senden.
"""
import json
import re
import time
import urllib.request

import paarfilter

STABLE = {"USDC", "USDT", "USDG", "USDT0", "USD₮0", "DAI", "USDE", "FRXUSD", "USDS", "PYUSD", "USDBC", "EURC"}


def _hole(url):
    req = urllib.request.Request(url, headers={"user-agent": "farmbot/1.0", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def _art(teile, top, chain=""):
    """'Aktie', 'Top' oder None fuer die Nicht-Stable-Seite eines Paares (nur ueber Kuerzel)."""
    andere = [t for t in teile if t.upper() not in STABLE]
    stable = [t for t in teile if t.upper() in STABLE]
    if len(teile) != 2 or len(stable) != 1:
        return None
    s = andere[0]
    if re.match(r"^[A-Z]{1,5}c$", s) or s.upper().endswith("X") and len(s) <= 6 and s[:-1].isupper():
        return "Aktie?"
    if paarfilter.ALIAS.get(s.lower(), s.lower()) in top:
        return "Top"
    if chain == "Robinhood" and s.isupper() and 1 <= len(s) <= 5:
        return "Aktie?"
    return None


def laufen(cache, top):
    out = {"zeit": time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime()), "defillama": {}, "merkl": {}}
    try:
        pools = _hole("https://yields.llama.fi/pools")["data"]
        for p in pools:
            if p.get("chain") not in ("Base", "Robinhood"):
                continue
            teile = re.split(r"[-/]", p.get("symbol") or "")
            art = _art(teile, top, p.get("chain"))
            apr = (p.get("apyReward") or 0)
            proj = p.get("project")
            z = out["defillama"].setdefault(f"{p['chain']}|{proj}", {"pools": 0, "mit_belohnung": 0, "passend": []})
            z["pools"] += 1
            if apr > 0:
                z["mit_belohnung"] += 1
            if art and apr >= 100 and (p.get("tvlUsd") or 0) >= 10000:
                z["passend"].append({"paar": p.get("symbol"), "apr_belohnung": round(apr), "tvl": round(p.get("tvlUsd") or 0),
                                     "art": art, "pool": p.get("pool"), "meta": p.get("poolMeta")})
        for z in out["defillama"].values():
            z["passend"].sort(key=lambda x: -x["apr_belohnung"])
            z["passend"] = z["passend"][:12]
        out["defillama"] = dict(sorted(out["defillama"].items(), key=lambda kv: -len(kv[1]["passend"])))
    except Exception as ex:
        out["defillama_fehler"] = str(ex)[:300]
    for name, cid in (("Base", 8453), ("Robinhood", 4663)):
        try:
            liste = []
            for seite in range(5):
                d = _hole(f"https://api.merkl.xyz/v4/opportunities?status=LIVE&items=100&page={seite}&chainId={cid}&campaigns=true")
                if not d:
                    break
                liste += d
            for o in liste:
                toks = [t.get("symbol") for t in (o.get("tokens") or [])]
                art = _art(toks, top, name)
                proto = ((o.get("protocol") or {}).get("name")) or o.get("mainProtocol")
                z = out["merkl"].setdefault(f"{name}|{proto}", {"kampagnen": 0, "passend": []})
                z["kampagnen"] += 1
                if art and (o.get("apr") or 0) >= 100:
                    z["passend"].append({"name": o.get("name"), "apr_merkl": round(o.get("apr") or 0),
                                         "tvl": round(o.get("tvl") or 0), "usd_tag": round(o.get("dailyRewards") or 0),
                                         "art": art, "typ": o.get("type"), "ziel": o.get("identifier")})
            for z in out["merkl"].values():
                z["passend"].sort(key=lambda x: -x["apr_merkl"])
                z["passend"] = z["passend"][:12]
        except Exception as ex:
            out[f"merkl_fehler_{name}"] = str(ex)[:300]
    out["merkl"] = dict(sorted(out["merkl"].items(), key=lambda kv: -len(kv[1]["passend"])))
    return out
