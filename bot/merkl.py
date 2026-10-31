"""Merkl: Zusatz-Belohnungen (Kampagnen) fuer Liquiditaet auf vielen DEXen.

Merkl verteilt Belohnungen, die Projekte fuer bestimmte Pools hinterlegen, haeufig in
Stablecoins oder im Projekt-Token, mit festem Start und Ende. Neue Kampagnen sind genau die
frischen Farmen, die wir suchen. Der Bot fragt alle 3 h die laufenden Kampagnen auf unseren
Chains ab und meldet:
- neue Kampagnen (Start in den letzten 72 h) mit genug Belohnung pro Tag
- jede Kampagne nur einmal
Merkls APR bezieht sich auf die gesamte TVL des Pools. Bei konzentrierter Liquiditaet und
engem Bereich liegt der eigene Anteil meist deutlich hoeher; nachgerechnet wird am Vertrag.
"""
import json
import time
import urllib.request

URL = "https://api.merkl.xyz/v4/opportunities?status=LIVE&items=100&page={seite}&chainId={chain}&campaigns=true"
STABLE = {"USDC", "USDT", "USDG", "USDT0", "USD₮0", "DAI", "USDE", "FRXUSD", "USDS", "PYUSD"}


def _hole(url):
    req = urllib.request.Request(url, headers={"user-agent": "farmbot/1.0", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def laufen(einstellung, cache, jetzt=None):
    jetzt = jetzt or time.time()
    st = cache.setdefault("merkl", {"ts": 0, "gemeldet": []})
    if jetzt - st.get("ts", 0) < einstellung.get("abstand_h", 3) * 3600:
        return None
    st["ts"] = jetzt
    alle = []
    for name, cid in einstellung["chains"].items():
        for seite in range(5):
            try:
                daten = _hole(URL.format(seite=seite, chain=cid))
            except Exception as e:
                print("Merkl", name, e)
                break
            if not daten:
                break
            for o in daten:
                if not any(k in str(o.get("type", "")).upper() for k in
                           ("UNISWAP", "SUSHI", "PANCAKE", "AERO", "VELO", "ALGEBRA", "QUICKSWAP", "CLAMM",
                            "PROJECT_X", "HYPERSWAP", "KITTEN", "RAMSES", "ZIA", "BROWNFI", "GIGA", "SWAPHOOD")):
                    continue  # nur Liquiditaet, kein Verleihen/Staking
                kamp = o.get("campaigns") or []
                start = min((c.get("startTimestamp") or jetzt) for c in kamp) if kamp else None
                ende = max((c.get("endTimestamp") or 0) for c in kamp) if kamp else None
                belohnung = sorted({(c.get("rewardToken") or {}).get("symbol", "?") for c in kamp})
                alle.append({"chain": name, "typ": o.get("type"), "name": o.get("name", ""), "id": o.get("identifier"),
                             "tvl": round(float(o.get("tvl") or 0)), "apr_gesamt_tvl": round(float(o.get("apr") or 0)),
                             "usd_tag": round(float(o.get("dailyRewards") or 0)), "belohnung": belohnung,
                             "stable": all(b.upper() in STABLE for b in belohnung) if belohnung else False,
                             "start": start, "ende": ende, "link": o.get("depositUrl") or ""})
            if len(daten) < 100:
                break
    alle.sort(key=lambda o: -o["usd_tag"])
    gemeldet = set(st.get("gemeldet", []))
    neu = [o for o in alle if o["id"] not in gemeldet and o["start"] and jetzt - o["start"] <= 72 * 3600
           and o["usd_tag"] >= einstellung.get("min_usd_tag", 300) and o["tvl"] >= einstellung.get("min_tvl_usd", 10000)]
    return {"stand": time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(jetzt)), "kampagnen": alle, "neu": neu}


def als_gemeldet(cache, neu):
    st = cache.setdefault("merkl", {})
    st["gemeldet"] = sorted(set(st.get("gemeldet", [])) | {o["id"] for o in neu})[-3000:]


def nachricht(neu):
    z = []
    for o in neu[:10]:
        tage = round((o["ende"] - o["start"]) / 86400) if o["ende"] and o["start"] else "?"
        ende = time.strftime("%d.%m.", time.gmtime(o["ende"])) if o["ende"] else "?"
        z.append(f"• {o['chain']} · <b>{o['name'][:60]}</b>\n  {o['usd_tag']} $/Tag in {', '.join(o['belohnung'])}"
                 f"{' (Stablecoin)' if o['stable'] else ''}, TVL {round(o['tvl'] / 1000)}k $, "
                 f"{o['apr_gesamt_tvl']} % auf die ganze TVL, läuft bis {ende} ({tage} Tage)")
    return ("🎯 <b>Neue Merkl-Kampagne(n)</b>\n" + "\n".join(z) +
            "\n\nMerkl-APR gilt für die ganze TVL, mit engem Bereich meist deutlich mehr. Nachrechnen vor dem Einstieg.")
