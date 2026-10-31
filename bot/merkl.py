"""Merkl: Zusatz-Belohnungen (Kampagnen) fuer Liquiditaet auf vielen DEXen.

Merkl verteilt Belohnungen, die Projekte fuer bestimmte Pools hinterlegen, haeufig in
Stablecoins oder im Projekt-Token, mit festem Start und Ende. Neue Kampagnen sind genau die
frischen Farmen, die wir suchen. Der Bot fragt alle 3 h die laufenden Kampagnen auf unseren
Chains ab und meldet:
- neue Kampagnen (Start in den letzten 72 h) mit genug Belohnung pro Tag
- jede Kampagne nur einmal
Merkls APR bezieht sich auf die gesamte TVL des Pools. Das hilft bei konzentrierter
Liquiditaet wenig. Deshalb rechnet der Bot fuer jede gemeldete Kampagne am Vertrag nach:
- aktive Liquiditaet L im Pool (V3/Algebra: liquidity(), V4: PoolManager.extsload)
- 100 $ im Bereich +-r um den aktuellen Kurs ergeben die eigene Liquiditaet myL
- Anteil = myL / (L + myL), Ertrag/Tag = Merkl-Belohnung pro Tag * Anteil
Der vorgeschlagene Bereich richtet sich nach der Schwankung des Paares in den letzten 24 h
(GeckoTerminal Stundenkerzen). Naeherung: Merkl gewichtet meist 80 % nach Gebuehren
(entspricht dem L-Anteil) und je 10 % nach gehaltenen Token. Handelsgebuehren kommen noch dazu.
"""
import json
import time
import urllib.request

from keccak import keccak256
from rpc import Chain, aufruf, als_int, als_adr

URL = "https://api.merkl.xyz/v4/opportunities?status=LIVE&items=100&page={seite}&chainId={chain}&campaigns=true"
GT_OHLCV = "https://api.geckoterminal.com/api/v2/networks/{net}/pools/{pool}/ohlcv/hour?limit=24"
GT_NETZ = {"Robinhood": "robinhood", "HyperEVM": "hyperevm", "Base": "base", "Arbitrum": "arbitrum",
           "OP Mainnet": "optimism", "BNB Chain": "bsc", "Avalanche": "avax"}
BEREICHE = (1, 2, 4, 8, 15)
Q96 = 2 ** 96
STABLE = {"USDC", "USDT", "USDG", "USDT0", "USD₮0", "DAI", "USDE", "FRXUSD", "USDS", "PYUSD"}


def _hole(url):
    req = urllib.request.Request(url, headers={"user-agent": "farmbot/1.0", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _wert_je_L(s, r, d0, d1, p0, p1):
    """Dollar-Wert je Einheit Liquiditaet fuer Bereich +-r um den Kurs (s = sqrtPrice roh)."""
    sa, sb = s * (1 - r) ** 0.5, s * (1 + r) ** 0.5
    return (1 / s - 1 / sb) / 10 ** d0 * p0 + (s - sa) / 10 ** d1 * p1


def _pool_lesen(chain, o, kamp):
    """Liefert (sqrtP, L, d0, d1, token0, token1) direkt vom Vertrag."""
    p = (kamp[0].get("params") or {}) if kamp else {}
    if p.get("poolManager") and int(p["poolManager"], 16) and len(o["id"]) == 66:
        basis = int.from_bytes(keccak256(bytes.fromhex(o["id"][2:]) + (6).to_bytes(32, "big")), "big")
        s0 = als_int(chain.call(p["poolManager"], aufruf("1e2eaeaf", basis)))
        L = als_int(chain.call(p["poolManager"], aufruf("1e2eaeaf", basis + 3)))
        return ((s0 & ((1 << 160) - 1)) / Q96, L, int(p["decimalsCurrency0"]), int(p["decimalsCurrency1"]),
                p["currency0"].lower(), p["currency1"].lower())
    pool = o["id"]
    try:
        roh = chain.call(pool, aufruf("3850c7bd"))       # slot0 (Uniswap-Art)
    except Exception:
        roh = chain.call(pool, aufruf("e76c01e4"))       # globalState (Algebra)
    sq = als_int(roh) / Q96
    L = als_int(chain.call(pool, aufruf("1a686502")))
    t0 = als_adr(chain.call(pool, aufruf("0dfe1681")))
    t1 = als_adr(chain.call(pool, aufruf("d21220a7")))
    d0 = als_int(chain.call(t0, aufruf("313ce567")))
    d1 = als_int(chain.call(t1, aufruf("313ce567")))
    return sq, L, d0, d1, t0.lower(), t1.lower()


def _schwankung_24h(chain_name, pool):
    """Spanne Hoch-Tief der letzten 24 h in Prozent vom letzten Kurs (oder None)."""
    net = GT_NETZ.get(chain_name)
    if not net:
        return None
    for versuch in range(2):
        time.sleep(3 + 6 * versuch)  # GeckoTerminal: ca. 30 Abfragen pro Minute
        try:
            d = _hole(GT_OHLCV.format(net=net, pool=pool))
            kerzen = d["data"]["attributes"]["ohlcv_list"]  # [ts, open, high, low, close, vol]
            if not kerzen:
                return None
            hoch, tief = max(k[2] for k in kerzen), min(k[3] for k in kerzen)
            return round((hoch - tief) / kerzen[0][4] * 100, 2)
        except Exception as e:
            print("GeckoTerminal OHLCV", pool[:10], e)
    return None


def eng_rechnen(o, kamp, preise, rpcs):
    """Ergaenzt o um APR fuer 100 $ in engen Bereichen und einen Vorschlag."""
    if o["typ"] in ("ERC20LOGPROCESSOR", "ENCOMPASSING") or not rpcs.get(o["chain"]):
        return
    try:
        chain = Chain(o["chain"], rpcs[o["chain"]], 0)
        sq, L, d0, d1, t0, t1 = _pool_lesen(chain, o, kamp)
    except Exception as e:
        o["eng_fehler"] = str(e)[:100]
        return
    p0, p1 = preise.get(t0), preise.get(t1)
    if not (sq and L and p0 and p1):
        o["eng_fehler"] = "Preis oder Liquiditaet fehlt"
        return
    o["eng"] = {}
    for r in BEREICHE:
        w = _wert_je_L(sq, r / 100, d0, d1, p0, p1)
        mein = 100 / w if w > 0 else 0
        tag = o["usd_tag"] * mein / (L + mein)
        o["eng"][r] = {"usd_tag": round(tag, 2), "apr": round(tag * 365)}
    # aktive Liquiditaet in Dollar (Wert eines +-1-%-Streifens hochgerechnet ist ungenau, daher nur L)
    span = _schwankung_24h(o["chain"], o["id"])
    o["schwankung_24h"] = span
    if span is None:
        vorschlag = 4
    else:
        # Bereich +-r ist 2r breit; die 24-h-Spanne soll mit etwas Reserve hineinpassen
        vorschlag = next((r for r in BEREICHE if 2 * r >= span * 1.2), BEREICHE[-1])
    o["vorschlag"] = vorschlag


def laufen(einstellung, cache, jetzt=None, rpcs=None):
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
                preise = {str(t.get("address", "")).lower(): float(t.get("price") or 0) for t in (o.get("tokens") or [])}
                alle.append({"_kamp": kamp, "_preise": preise, "chain": name, "typ": o.get("type"), "name": o.get("name", ""), "id": o.get("identifier"),
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
    # Enge Bereiche am Vertrag nachrechnen: fuer alle neuen und die 15 groessten Kampagnen
    for o in neu + [x for x in alle[:15] if x not in neu]:
        try:
            eng_rechnen(o, o["_kamp"], o["_preise"], rpcs or {})
        except Exception as e:
            o["eng_fehler"] = str(e)[:100]
    for o in alle:
        o.pop("_kamp", None)
        o.pop("_preise", None)
    return {"stand": time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(jetzt)), "kampagnen": alle, "neu": neu}


def als_gemeldet(cache, neu):
    st = cache.setdefault("merkl", {})
    st["gemeldet"] = sorted(set(st.get("gemeldet", [])) | {o["id"] for o in neu})[-3000:]


def nachricht(neu):
    z = []
    for o in neu[:10]:
        tage = round((o["ende"] - o["start"]) / 86400) if o["ende"] and o["start"] else "?"
        ende = time.strftime("%d.%m.", time.gmtime(o["ende"])) if o["ende"] else "?"
        zeile = (f"• {o['chain']} · <b>{o['name'][:60]}</b>\n  {o['usd_tag']} $/Tag in {', '.join(o['belohnung'])}"
                 f"{' (Stablecoin)' if o['stable'] else ''}, TVL {round(o['tvl'] / 1000)}k $, läuft bis {ende} ({tage} Tage)")
        if o.get("eng"):
            v = o["vorschlag"]
            sp = f"Schwankung 24 h {o['schwankung_24h']} %" if o.get("schwankung_24h") is not None else "Schwankung unbekannt"
            e = o["eng"]
            zeile += (f"\n  👉 <b>±{v} %: {e[v]['apr']} % APR</b> ({e[v]['usd_tag']} $/Tag je 100 $), {sp}"
                      f"\n  ±2 %: {e[2]['apr']} % · ±4 %: {e[4]['apr']} % · ±8 %: {e[8]['apr']} %")
        else:
            zeile += f"\n  Enger Bereich nicht berechenbar ({o.get('eng_fehler', 'kein Pool')}), Merkl ganze TVL: {o['apr_gesamt_tvl']} %"
        z.append(zeile)
    return ("🎯 <b>Neue Merkl-Kampagne(n)</b>\n" + "\n".join(z) +
            "\n\nAPR = nur Merkl-Belohnung für 100 $ im Bereich, am Vertrag gerechnet (aktive Liquidität jetzt). "
            "Gebühren kommen dazu. Kommt mehr Liquidität rein, sinkt der Wert.")
