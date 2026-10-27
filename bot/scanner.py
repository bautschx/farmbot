"""Farm-Jagd direkt aus der Blockchain, ohne vfat oder andere Plattformen.

Ablauf je DEX: Aus einem bekannten Gauge den Voter ermitteln, alle Pools des
Voters durchgehen, lebende Gauges mit laufender Belohnung finden, Preise aus
den Pools selbst ableiten und fuer eine eigene Position (z. B. 100 $ bei ±2 %)
ausrechnen, was sie wirklich bekommen wuerde. Danach Stefans Regeln anwenden.
"""
import math
import time

from rpc import Chain, aufruf, als_int, als_adr, als_text, RPCFehler

S = {  # Funktions-Selektoren (keccak256, erste 4 Bytes), am 27.10.2026 berechnet
    "voter": "46c96aac", "nft": "47ccca02", "pool": "16f0115b",
    "rewardToken": "f7c618c1", "rewardRate": "7b0a47ee", "periodFinish": "ebe2b12b",
    "length": "1f7b6d32", "pools": "ac4afa38", "gauges": "b9a09fd5", "isAlive": "1703e5f9",
    "slot0": "3850c7bd", "token0": "0dfe1681", "token1": "d21220a7",
    "tickSpacing": "d0c93a7c", "liquidity": "1a686502", "stakedLiquidity": "3ab04b20",
    "fee": "ddca3f43", "decimals": "313ce567", "symbol": "95d89b41",
    "balanceOf": "70a08231",
}
Q96 = 2 ** 96
JAHR = 365 * 24 * 3600


def _m(chain, aufrufe):
    return chain.multi(aufrufe) if aufrufe else []


# ---------- Struktur einlesen ----------

def voter_pools(chain, dex, cache, max_neu=4000):
    """Alle Pool-Adressen des Voters, neue Eintraege werden nachgeladen."""
    schl = f"{chain.name}|{dex['name']}"
    c = cache.setdefault("voter", {}).setdefault(schl, {})
    if not c.get("voter"):
        c["voter"] = als_adr(chain.call(dex["seed_gauge"], aufruf(S["voter"])))
    voter = c["voter"]
    anzahl = als_int(chain.call(voter, aufruf(S["length"])))
    bekannt = c.setdefault("pools", [])
    fehlend = list(range(len(bekannt), min(anzahl, len(bekannt) + max_neu)))
    res = _m(chain, [(voter, aufruf(S["pools"], i)) for i in fehlend])
    bekannt.extend(als_adr(r) for r in res)
    return voter, anzahl, [p for p in bekannt if p]


def token_infos(chain, tokens, cache):
    tc = cache.setdefault("tokens", {})
    neu = [t for t in tokens if f"{chain.name}:{t}" not in tc]
    aufr = []
    for t in neu:
        aufr += [(t, aufruf(S["decimals"])), (t, aufruf(S["symbol"]))]
    res = _m(chain, aufr)
    for i, t in enumerate(neu):
        d = als_int(res[2 * i])
        tc[f"{chain.name}:{t}"] = {"d": d if d is not None and d <= 36 else 18,
                                   "s": (als_text(res[2 * i + 1]) or "?").strip()[:20]}
    return {t: tc[f"{chain.name}:{t}"] for t in tokens}


def farmen_lesen(chain, dex, voter, pools):
    """Lebende Gauges mit laufender Belohnung und die Pooldaten dazu."""
    res = _m(chain, [(voter, aufruf(S["gauges"], p)) for p in pools])
    paare = [(p, als_adr(g)) for p, g in zip(pools, res)
             if g and als_adr(g) != "0x" + "0" * 40]
    lebend = _m(chain, [(voter, aufruf(S["isAlive"], g)) for _, g in paare])
    paare = [pg for pg, a in zip(paare, lebend) if als_int(a) == 1]

    felder_g = ["rewardRate", "periodFinish", "rewardToken"]
    felder_p = ["token0", "token1", "slot0", "stakedLiquidity", "liquidity",
                "tickSpacing", "fee"]
    aufr = []
    for p, g in paare:
        aufr += [(g, aufruf(S[f])) for f in felder_g]
        aufr += [(p, aufruf(S[f])) for f in felder_p]
    res = _m(chain, aufr)
    jetzt = time.time()
    farmen = []
    k = len(felder_g) + len(felder_p)
    for i, (p, g) in enumerate(paare):
        r = res[i * k:(i + 1) * k]
        rate, ende, rt = als_int(r[0]), als_int(r[1]), als_adr(r[2])
        t0, t1 = als_adr(r[3]), als_adr(r[4])
        if None in (rate, ende, rt, t0, t1) or r[5] is None:
            continue  # kein konzentrierter Pool oder anderer Vertragstyp
        farmen.append({
            "pool": p, "gauge": g, "dex": dex["name"],
            "belohnung": rt, "rate": rate, "laeuft": ende > jetzt and rate > 0,
            "ende": ende, "t0": t0, "t1": t1,
            "sqrtP": als_int(r[5], 0), "tick": als_int(r[5], 1, signed=True),
            "staked_L": als_int(r[6]) or 0, "L": als_int(r[7]) or 0,
            "tickSpacing": als_int(r[8], signed=True), "fee": als_int(r[9]),
        })
    return farmen


def bestaende(chain, farmen):
    aufr = []
    for f in farmen:
        aufr += [(f["t0"], aufruf(S["balanceOf"], f["pool"])),
                 (f["t1"], aufruf(S["balanceOf"], f["pool"]))]
    res = _m(chain, aufr)
    for i, f in enumerate(farmen):
        f["b0"] = als_int(res[2 * i]) or 0
        f["b1"] = als_int(res[2 * i + 1]) or 0


# ---------- Preise aus den Pools selbst ----------

def preise_ableiten(farmen, tokens, dollar, min_tiefe_usd=5000):
    """Dollar-Token = 1 $. Andere Token bekommen den Preis aus dem Pool, in dem
    die Gegenseite am meisten bekannten Wert hat (Schutz gegen Mini-Pools)."""
    preis = {a: 1.0 for a in dollar}
    for _ in range(4):
        beste = {}
        for f in farmen:
            if not f["sqrtP"]:
                continue
            d0, d1 = tokens[f["t0"]]["d"], tokens[f["t1"]]["d"]
            p1_je_0 = (f["sqrtP"] / Q96) ** 2 * 10 ** (d0 - d1)
            if p1_je_0 <= 0:
                continue
            for bekannt, unbekannt, faktor, bestand, dek in (
                    (f["t1"], f["t0"], p1_je_0, f["b1"], d1),
                    (f["t0"], f["t1"], 1 / p1_je_0, f["b0"], d0)):
                if bekannt in preis and unbekannt not in preis:
                    tiefe = bestand / 10 ** dek * preis[bekannt]
                    if tiefe >= min_tiefe_usd and tiefe > beste.get(unbekannt, (0, 0))[0]:
                        beste[unbekannt] = (tiefe, faktor * preis[bekannt])
        if not beste:
            break
        for t, (_, p) in beste.items():
            preis[t] = p
    return preis


# ---------- Rechnung fuer die eigene Position ----------

def eigene_position(f, tokens, preis, wert_usd, breite_prozent):
    """Was bekommt eine eigene Position mit wert_usd bei ±breite_prozent?"""
    if f["belohnung"] not in preis or f["t0"] not in preis or f["t1"] not in preis:
        return None
    w = breite_prozent / 100
    s = f["sqrtP"] / Q96
    # Grenzen auf das Tick-Raster des Pools legen, so wie eine echte Position sie hat
    ts = f.get("tickSpacing") or 1
    t = f.get("tick")
    if t is not None and ts > 0:
        unten = math.floor((t + math.log(1 - w) / math.log(1.0001)) / ts) * ts
        oben = math.ceil((t + math.log(1 + w) / math.log(1.0001)) / ts) * ts
        sa, sb = math.sqrt(1.0001 ** unten), math.sqrt(1.0001 ** oben)
    else:
        sa, sb = s * math.sqrt(1 - w), s * math.sqrt(1 + w)
    d0, d1 = tokens[f["t0"]]["d"], tokens[f["t1"]]["d"]
    usd_je_L = ((1 / s - 1 / sb) / 10 ** d0 * preis[f["t0"]]
                + (s - sa) / 10 ** d1 * preis[f["t1"]])
    if usd_je_L <= 0:
        return None
    L = wert_usd / usd_je_L
    anteil = L / (f["staked_L"] + L)
    dr = tokens[f["belohnung"]]["d"]
    jahr_usd = f["rate"] / 10 ** dr * preis[f["belohnung"]] * JAHR * anteil if f["laeuft"] else 0
    return {"apr": jahr_usd / wert_usd * 100, "tag_usd": jahr_usd / 365}


# ---------- Ein ganzer Durchlauf ----------

def scannen(konfig, cache, verlauf):
    jetzt = int(time.time())
    regeln = konfig["regeln"]
    wert = konfig["position_usd"]
    ergebnis, status = [], []
    for name, cfg in konfig["chains"].items():
        chain = Chain(name, cfg["rpc"], cfg["id"])
        for dex in cfg["dexe"]:
            eintrag = {"chain": name, "dex": dex["name"], "typ": dex["typ"]}
            if dex["typ"] != "slipstream":
                eintrag["stand"] = "noch nicht angebunden"
                status.append(eintrag)
                continue
            try:
                voter, anzahl, pools = voter_pools(chain, dex, cache)
                farmen = farmen_lesen(chain, dex, voter, pools)
                alle_tokens = sorted({t for f in farmen for t in (f["t0"], f["t1"], f["belohnung"])})
                tokens = token_infos(chain, alle_tokens, cache)
                bestaende(chain, farmen)
                preis = preise_ableiten(farmen, tokens, cfg["dollar"])
            except RPCFehler as e:
                eintrag["stand"] = f"Fehler: {str(e)[:160]}"
                status.append(eintrag)
                continue

            # Regel 2: Liquiditaet des Belohnungs-Tokens gegen seine Tagesausschuettung
            emission_tag, liq = {}, {}
            for f in farmen:
                rt = f["belohnung"]
                if f["laeuft"] and rt in preis:
                    emission_tag[rt] = emission_tag.get(rt, 0) + \
                        f["rate"] * 86400 / 10 ** tokens[rt]["d"] * preis[rt]
                for t, b in ((f["t0"], f["b0"]), (f["t1"], f["b1"])):
                    if t == rt and t in preis:
                        liq[t] = liq.get(t, 0) + b / 10 ** tokens[t]["d"] * preis[t]

            # eigener Preisverlauf fuer Regel 4 (Belohnung −30 % in 24 h)
            for rt in emission_tag:
                schl = f"{name}:{rt}"
                reihe = verlauf.setdefault(schl, [])
                reihe.append([jetzt, round(preis[rt], 10)])
                verlauf[schl] = [x for x in reihe if x[0] > jetzt - 8 * 86400]

            laufende = 0
            for f in farmen:
                if not f["laeuft"]:
                    continue
                laufende += 1
                tvl = sum(b / 10 ** tokens[t]["d"] * preis.get(t, 0)
                          for t, b in ((f["t0"], f["b0"]), (f["t1"], f["b1"])))
                pos = {str(b): eigene_position(f, tokens, preis, wert, b)
                       for b in konfig["bereiche_prozent"]}
                rt = f["belohnung"]
                ver = verlauf.get(f"{name}:{rt}", [])
                alt = [p for t, p in ver if t <= jetzt - 23 * 3600]
                aend24 = (preis[rt] / alt[-1] - 1) * 100 if alt and rt in preis else None
                gebuehr = (f["fee"] or 0) / 10000
                p2 = pos.get("2")
                pruef = {
                    "gebuehr_ok": gebuehr <= regeln["max_poolgebuehr_prozent"],
                    "belohnung_nicht_im_paar": rt not in (f["t0"], f["t1"]),
                    "apr_ok": bool(p2 and p2["apr"] >= regeln["min_apr_prozent"]),
                    "liquiditaet_ok": emission_tag.get(rt, 0) > 0 and liq.get(rt, 0) >=
                        regeln["min_liquiditaet_faktor_emission"] * emission_tag[rt],
                    "tvl_ok": tvl >= regeln["min_pool_tvl_usd"],
                    "preis_ok": aend24 is None or aend24 > -regeln["max_tagesminus_belohnung_prozent"],
                    "preis_bekannt": rt in preis and f["t0"] in preis and f["t1"] in preis,
                }
                ergebnis.append({
                    "chain": name, "dex": f["dex"], "pool": f["pool"], "gauge": f["gauge"],
                    "paar": f"{tokens[f['t0']]['s']}/{tokens[f['t1']]['s']}",
                    "belohnung": tokens[rt]["s"], "belohnung_adr": rt,
                    "belohnung_preis": preis.get(rt),
                    "belohnung_24h_prozent": None if aend24 is None else round(aend24, 1),
                    "gebuehr_prozent": gebuehr, "tvl_usd": round(tvl),
                    "ausschuettung_tag_usd": round(f["rate"] * 86400 / 10 ** tokens[rt]["d"]
                                                   * preis.get(rt, 0), 2),
                    "mein_apr": {b: (None if v is None else round(v["apr"], 1)) for b, v in pos.items()},
                    "mein_ertrag_tag_usd": {b: (None if v is None else round(v["tag_usd"], 3))
                                            for b, v in pos.items()},
                    "pruefung": pruef,
                    "alle_regeln_ok": all(pruef.values()),
                    "belohnung_endet": f["ende"],
                })
            eintrag.update({"stand": "ok", "voter": voter, "pools_gesamt": anzahl,
                            "laufende_farmen": laufende, "abfragen": chain.abfragen})
            status.append(eintrag)
    ergebnis.sort(key=lambda e: -(e["mein_apr"].get("2") or 0))
    return {"zeit": jetzt, "position_usd": wert, "farmen": ergebnis, "dexe": status}
